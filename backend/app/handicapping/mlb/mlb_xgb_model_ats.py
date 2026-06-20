"""
MLB XGBoost Backtester — ATS (against-the-spread) model.

Trains XGBoost regressors to predict run differential using shared feature
engineering from ``data_loader``.  Backtesting, inference, and model persistence.

Usage:
    python -m app.handicapping.mlb.mlb_xgb_model_ats --test-year 2025
    python -m app.handicapping.mlb.mlb_xgb_model_ats --mode all --train-from 2021
"""

import asyncio
import logging
import warnings
import json
import os
import pickle
import shutil
from datetime import datetime, timezone, date
from typing import Optional, Any
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

from app.handicapping.mlb.data_loader import (
    get_data_loader,
    build_features as mlb_build_features,
    FEATURE_SETS,
    MLBDataLoader,
)

warnings.filterwarnings("ignore")

# ── Training DB persistence (safe import) ──
try:
    from app.handicapping.db_training import (
        save_training_run,
        update_pkl_filename,
        get_current_training_run,
        get_model_pkl_path,
    )
    _DB_HELPERS_AVAILABLE = True
except ImportError:
    _DB_HELPERS_AVAILABLE = False

log = logging.getLogger(__name__).info

# ── Sync DB URL for inference path ──
DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

# ── Model globals ──
ATS_MODEL_PATH = os.path.join(os.path.dirname(__file__), "ats_model.pkl")
_ats_model = None
_ats_feature_cache: Optional[pd.DataFrame] = None
CURRENT_YEAR = datetime.now().year


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2023,
    feature_set: str = "full",
    train_years: list[int] | None = None,
) -> dict:
    """Run a single backtest year."""
    import time

    t0 = time.time()

    if train_years is None:
        train_years = [y for y in [2020, 2021, 2022] if y != test_year]

    log(f"=== Backtest {test_year} ===")
    log(f"  Train: {train_years}  Test: {test_year}  Features: {feature_set}")

    fcols = FEATURE_SETS.get(feature_set, FEATURE_SETS["full"])

    # Fix column name aliasing — map old feature names
    col_map = {
        "ha_tz": "tz_diff",
        "aa_tz": "tz_diff",
    }
    fcols = [col_map.get(c, c) for c in fcols]

    present = [c for c in fcols if c in feats.columns]
    missing = [c for c in fcols if c not in feats.columns]
    if missing:
        log(f"  WARNING: missing features: {missing}")
    log(f"  Features: {len(present)} / {len(fcols)}")

    # Split — use feats for both features AND targets
    train_feats = feats[feats["season_year"].isin(train_years)].copy()
    test_feats = feats[feats["season_year"] == test_year].copy()

    present = [c for c in fcols if c in train_feats.columns]

    log(f"  Train: {len(train_feats)} rows  Test: {len(test_feats)} rows")

    if len(train_feats) < 50 or len(test_feats) < 10:
        log(f"  SKIP: insufficient data")
        return {}

    # Train
    X_train = train_feats[present].values
    y_train = train_feats["actual_margin"].values

    model = xgb.XGBRegressor(
        n_estimators=300,
        max_depth=5,
        learning_rate=0.06,
        subsample=0.7,
        colsample_bytree=0.5,
        random_state=42,
        verbosity=0,
        eval_metric="mae",
    )
    model.fit(X_train, y_train)

    # Predict
    X_test = test_feats[present].values
    y_test = test_feats["actual_margin"].values
    y_pred = model.predict(X_test)

    # Evaluation
    mae = mean_absolute_error(y_test, y_pred)
    y_test_sign = np.sign(y_test)
    y_pred_sign = np.sign(y_pred)
    acc = np.mean(y_test_sign == y_pred_sign)

    # ATS: use spread to check if predicted margin > spread
    spread = test_feats["spread"].values
    ats_correct = np.sign(y_pred - spread) == np.sign(y_test - spread)
    ats_acc = np.mean(ats_correct) if len(ats_correct) > 0 else 0.5

    # OU
    actual_total = test_feats["home_score"].values + test_feats["away_score"].values
    ou_line = test_feats["over_under"].fillna(8.5).values
    implied_total = np.clip(
        test_feats.get("implied_total", pd.Series(8.5)).values, 3, 16
    )
    ou_correct = np.sign(implied_total - ou_line) == np.sign(actual_total - ou_line)
    ou_acc = np.mean(ou_correct) if len(ou_correct) > 0 else 0.5

    # ML
    home_ml = test_feats["home_moneyline"].values
    implied_ml = test_feats.get("home_implied_probability", pd.Series(0.5)).values
    ml_pred_home = implied_ml > 0.5
    ml_actual_home = test_feats["home_score"].values > test_feats["away_score"].values
    ml_acc = np.mean(ml_pred_home == ml_actual_home) if len(ml_actual_home) > 0 else 0.5

    results = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": feature_set,
        "rows": {
            "train": len(train_feats),
            "test": len(test_feats),
        },
        "metrics": {
            "mae": round(float(mae), 3),
            "direction_accuracy": round(float(acc), 4),
            "ats_accuracy": round(float(ats_acc), 4),
            "ou_accuracy": round(float(ou_acc), 4),
            "ml_accuracy": round(float(ml_acc), 4),
        },
        "feature_importance": [
            {"feature": f, "importance": round(float(imp), 6)}
            for f, imp in zip(present, model.feature_importances_)
        ],
        "model_params": model.get_params(),
        "duration_seconds": round(time.time() - t0, 1),
    }

    log(f"  MAE: {results['metrics']['mae']}  ATS: {results['metrics']['ats_accuracy']:.3f}  OU: {results['metrics']['ou_accuracy']:.3f}  ML: {results['metrics']['ml_accuracy']:.3f}  Dir: {results['metrics']['direction_accuracy']:.3f}")
    log(f"  Duration: {results['duration_seconds']}s")
    print(f"\n  Top 10 features by importance:")
    imp_sorted = sorted(results["feature_importance"], key=lambda x: -x["importance"])
    for feat in imp_sorted[:10]:
        print(f"    {feat['feature']:35s} {feat['importance']:.4f}")

    return results


async def run_all_years(
    hide_progress: bool = True,
    feature_sets: list[str] | None = None,
    train_from: int = 2020,
    test_until: int | None = None,
) -> list[dict]:
    """Run backtests for all available years."""
    from sqlalchemy.ext.asyncio import create_async_engine

    if test_until is None:
        test_until = CURRENT_YEAR

    if feature_sets is None:
        feature_sets = ["full"]

    total_results: list[dict] = []

    raw = get_data_loader().load_games(status="FINAL")
    feats = mlb_build_features(raw)
    log(f"Loaded {len(raw)} games, {len(feats.columns)} features")

    for feature_set in feature_sets:
        for year in range(train_from + 1, test_until + 1):
            train_years = list(range(train_from, year))
            result = await run_backtest(raw, feats, year, feature_set, train_years)
            if result:
                total_results.append(result)
    return total_results


def _enrich_df(df: pd.DataFrame) -> pd.DataFrame:
    """Enrich a raw game DataFrame with full features for inference."""
    return mlb_build_features(df)


async def run_single(
    test_year: int = 2025,
    feature_set: str = "full",
) -> dict:
    """Load data, build features, run backtest for one year."""
    # Load data for ALL seasons so we can train on earlier years
    df = get_data_loader().load_games(
        status="FINAL",
        include_upcoming=False,
    )
    if df.empty:
        log(f"No games found")
        return {}
    all_seasons = sorted(df["season_year"].dropna().unique().tolist())
    train_years = [y for y in all_seasons if y < test_year]

    feats = mlb_build_features(df)
    result = await run_backtest(df, feats, test_year, feature_set, train_years)
    return result or {}


def set_model_path(path: str):
    global ATS_MODEL_PATH, _ats_model
    ATS_MODEL_PATH = path
    _ats_model = None


def _load_ats_model():
    global _ats_model
    if _ats_model is not None:
        return _ats_model
    path = ATS_MODEL_PATH
    if not os.path.exists(path):
        log(f"ATS model not found at {path}")
        return None
    with open(path, "rb") as f:
        model = pickle.load(f)
    _ats_model = model
    log(f"ATS model loaded from {path}")
    return _ats_model


async def predict_ats(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
) -> dict[str, Any] | None:
    """Inference for a single game. Loads data, builds features, returns prediction."""
    global _ats_feature_cache

    model = _load_ats_model()
    if model is None:
        return None

    if _ats_feature_cache is None:
        log("ATS: building feature cache from all recent games...")
        raw_df = get_data_loader().load_games(
            seasons=[CURRENT_YEAR],
            status=None,
            include_upcoming=True,
        )
        _ats_feature_cache = mlb_build_features(raw_df)
        log(f"ATS: {len(_ats_feature_cache)} features built")

    feats_df = _ats_feature_cache
    game_feats = feats_df[feats_df["game_id"] == game_id]

    if game_feats.empty:
        log(f"ATS: no features for game_id={game_id}, trying by teams...")
        game_feats = feats_df[
            (feats_df["ha"] == home_abbr) & (feats_df["aa"] == away_abbr)
        ].sort_values("game_date", ascending=False)
        if game_feats.empty:
            log(f"ATS: no features for {home_abbr} vs {away_abbr}")
            return None
        game_feats = game_feats.iloc[:1]

    fcols = FEATURE_SETS.get("full", FEATURE_SETS["full"])
    present = [c for c in fcols if c in game_feats.columns]
    if not present:
        log(f"ATS: no features available in cache")
        return None

    X = game_feats[present].values
    pred_margin = float(model.predict(X)[0])

    spread = float(game_feats["spread"].iloc[0]) if "spread" in game_feats.columns else 0.0
    ou = float(game_feats["over_under"].iloc[0]) if "over_under" in game_feats.columns else 8.5
    home_ml = float(game_feats["home_moneyline"].iloc[0]) if "home_moneyline" in game_feats.columns else 0
    away_ml = float(game_feats["away_moneyline"].iloc[0]) if "away_moneyline" in game_feats.columns else 0

    return {
        "game_id": game_id,
        "home_team": home_abbr,
        "away_team": away_abbr,
        "predicted_margin": round(pred_margin, 2),
        "spread": spread,
        "over_under": ou,
        "home_moneyline": home_ml,
        "away_moneyline": away_ml,
        "ats_pick": "home" if pred_margin > spread else "away",
        "ou_pick": "over" if pred_margin > 4.5 else "under",
        "confidence": min(abs(pred_margin - spread) / 3, 0.95),
    }


async def train_model(
    year: int,
    train_years: list[int],
    feature_set: str = "full",
) -> dict:
    """Train and persist the ATS model for season ``year`` using ``train_years`` data."""
    df = get_data_loader().load_games(seasons=train_years, status="FINAL")
    feats = mlb_build_features(df)

    fcols = FEATURE_SETS.get(feature_set, FEATURE_SETS["full"])
    present = [c for c in fcols if c in feats.columns]

    target = df["actual_margin"].values
    X = feats[present].values

    model = xgb.XGBRegressor(
        n_estimators=400,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.75,
        colsample_bytree=0.5,
        random_state=42,
        verbosity=0,
        eval_metric="mae",
    )
    model.fit(X, target)

    os.makedirs(os.path.dirname(ATS_MODEL_PATH), exist_ok=True)
    with open(ATS_MODEL_PATH, "wb") as f:
        pickle.dump(model, f)

    global _ats_model
    _ats_model = model

    feature_importance = [
        {"feature": f, "importance": round(float(imp), 6)}
        for f, imp in zip(present, model.feature_importances_)
    ]
    feature_importance.sort(key=lambda x: -x["importance"])
    log(f"Model saved to {ATS_MODEL_PATH}")
    for fi in feature_importance[:15]:
        log(f"  {fi['feature']:35s} {fi['importance']:.4f}")

    return {
        "model_type": "ats",
        "test_year": year,
        "train_years": train_years,
        "feature_set": feature_set,
        "num_features": len(present),
        "features": present,
        "feature_importance": feature_importance,
        "model_params": model.get_params(),
        "model_path": ATS_MODEL_PATH,
    }


# ── CLI ──
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser("MLB ATS Backtest")
    parser.add_argument("--test-year", type=int, default=2025)
    parser.add_argument("--features", type=str, default="full",
                        choices=list(FEATURE_SETS.keys()))
    parser.add_argument("--mode", type=str, default="one",
                        choices=["one", "all"])
    parser.add_argument("--train-from", type=int, default=2020)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.mode == "all":
        results = asyncio.run(run_all_years(
            feature_sets=[args.features],
            train_from=args.train_from,
            test_until=args.test_year,
        ))
        print(f"\n{'='*60}")
        print(f"Summary: {len(results)} backtests")
        for r in results:
            m = r["metrics"]
            print(f"  {r['test_year']}: MAE={m['mae']}  ATS={m['ats_accuracy']:.3f}  OU={m['ou_accuracy']:.3f}  ML={m['ml_accuracy']:.3f}  Dir={m['direction_accuracy']:.3f}")
    else:
        result = asyncio.run(run_single(args.test_year, args.features))
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("No results (check data)")
