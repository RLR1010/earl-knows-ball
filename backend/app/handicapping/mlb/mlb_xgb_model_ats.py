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
import uuid
from datetime import datetime, timezone, date
from typing import Optional, Any
from pathlib import Path

import math

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error
import xgboost as xgb

# Optional DB helpers for saving training runs
try:
    from app.handicapping.db_training import save_training_run, update_pkl_filename
    _DB_HELPERS_AVAILABLE = True
except ImportError:
    save_training_run = None
    update_pkl_filename = None
    _DB_HELPERS_AVAILABLE = False

from app.handicapping.mlb.data_loader import (
    get_data_loader,
    build_features as mlb_build_features,
    get_model_features,
    MLBDataLoader,
)

# Feature list for the ATS model, sourced from the most recent
# (is_current) training run's feature_importance.  Must stay in sync
# with the model that was actually trained.
# Lazy-loaded to avoid DB query at import time (prevents Granian startup crash).
ATS_FEATURES: list[str] = []
_ats_features_loaded = False


def _ensure_ats_features() -> list[str]:
    global ATS_FEATURES, _ats_features_loaded
    if not _ats_features_loaded:
        try:
            ATS_FEATURES = get_model_features("ats")
        except RuntimeError:
            ATS_FEATURES = []
        _ats_features_loaded = True
    return ATS_FEATURES

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

# PKL directory for MLB models
MLB_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
MLB_PKL_DIR.mkdir(parents=True, exist_ok=True)

_ats_model = None
_ats_feature_cache: Optional[pd.DataFrame] = None
CURRENT_YEAR = datetime.now().year


async def run_backtest(
    df: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int = 2023,
    feature_set: list[str] | None = None,
    train_years: list[int] | None = None,
    training_id: str | None = None,
) -> dict:
    """Run a single backtest year."""
    import time

    t0 = time.time()

    if train_years is None:
        train_years = [y for y in [2020, 2021, 2022] if y != test_year]

    log(f"=== Backtest {test_year} ===")
    log(f"  Train: {train_years}  Test: {test_year}")

    # Resolve "full" string shorthand to ATS_FEATURES list
    if isinstance(feature_set, str):
        feature_set = _ensure_ats_features()

    fcols = feature_set if feature_set is not None else _ensure_ats_features()

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

    # Filter out games without betting lines — they can't be used for ATS training
    train_mask = train_feats["spread"].notna() & train_feats["home_moneyline"].notna()
    test_mask = test_feats["spread"].notna() & test_feats["home_moneyline"].notna()
    train_feats = train_feats[train_mask].copy()
    test_feats = test_feats[test_mask].copy()

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
    # ATS: home team covers if margin + spread > 0 (i.e., actual result beats the spread).
    # The spread is from the home team's perspective: negative = home favored, positive = home underdog.
    ats_correct = np.sign(y_pred + spread) == np.sign(y_test + spread)
    ats_acc = np.mean(ats_correct) if len(ats_correct) > 0 else 0.5

    # ML: model predicts margin — positive margin = model picks home team to win
    ml_pred_home = y_pred > 0
    ml_actual_home = test_feats["home_score"].values > test_feats["away_score"].values
    ml_acc = np.mean(ml_pred_home == ml_actual_home) if len(ml_actual_home) > 0 else 0.5

    n_test = len(test_feats)
    n_correct_ats = int(np.sum(ats_correct))
    n_correct_ml = int(np.sum(ml_pred_home == ml_actual_home))

    results = {
        "test_year": test_year,
        "train_years": train_years,
        "feature_set": feature_set,
        "rows": {
            "train": len(train_feats),
            "test": n_test,
        },
        "total_games": n_test,
        "mae": round(float(mae), 3),
        "ats": {
            "total": n_test,
            "correct": n_correct_ats,
            "incorrect": n_test - n_correct_ats,
            "pct": round(float(ats_acc * 100), 2),
        },
        "ml": {
            "total": n_test,
            "correct": n_correct_ml,
            "incorrect": n_test - n_correct_ml,
            "pct": round(float(ml_acc * 100), 2),
        },
        "feature_importance": [
            {"feature": f, "importance": round(float(imp), 6)}
            for f, imp in zip(present, model.feature_importances_)
        ],
        "model_params": model.get_params(),
        "duration_seconds": round(time.time() - t0, 1),
    }

    log(f"  MAE: {results['mae']}  ATS: {results['ats']['pct']:.3f}  ML: {results['ml']['pct']:.3f}")
    log(f"  Duration: {results['duration_seconds']}s")
    print(f"\n  Top 10 features by importance:")
    imp_sorted = sorted(results["feature_importance"], key=lambda x: -x["importance"])
    for feat in imp_sorted[:10]:
        print(f"    {feat['feature']:35s} {feat['importance']:.4f}")

    # Save model to pkl
    # Use training_id if provided, otherwise use a temp UUID
    pkl_stem = training_id if training_id else str(uuid.uuid4())
    pkl_path = MLB_PKL_DIR / f"{pkl_stem}-{test_year}.pkl"
    try:
        pickle.dump(model, open(pkl_path, "wb"))
        log(f"  Saved model to {pkl_path}")
    except Exception as e:
        log(f"  WARNING: failed to save pkl: {e}")

    return results


async def run_all_years(
    hide_progress: bool = True,
    feature_sets: list[str] | None = None,
    train_from: int = 2022,
    test_until: int | None = None,
    skip_db: bool = False,
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

    # Test years are the final 2 seasons; train_years is everything before each test year
    test_years = [2025, 2026]

    for feature_set in feature_sets:
        for year in test_years:
            train_years = list(range(train_from, year))
            result = await run_backtest(raw, feats, year, feature_set, train_years)
            if result:
                total_results.append(result)

    # Save ONE training run with all years as a list in results_json
    # (matches the admin frontend which expects a single row with a list of year results)
    if _DB_HELPERS_AVAILABLE and save_training_run and not skip_db and total_results:
        try:
            def _sanitize(obj):
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_sanitize(v) for v in obj]
                elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                    return None
                return obj

            # Build the list-of-years format the admin page expects
            # First save to DB to get the training_id, then rename pkls
            results_list = [_sanitize(r) for r in total_results]
            for r, flat_entry in zip(total_results, results_list):
                year = r["test_year"]
                flat_entry["name"] = f"{year} MLB ATS"
                flat_entry["ats_pct"] = r["ats"]["pct"]
                flat_entry["ats_correct"] = r["ats"]["correct"]
                flat_entry["ats_total"] = r["ats"]["total"]

            # Store the most recent (last) test year and its training years in the DB row
            last_test_year = test_years[-1]
            last_train_years = list(range(train_from, last_test_year))
            db_run_id = save_training_run(
                sport="mlb",
                model_type="ats",
                test_year=last_test_year,
                train_years=last_train_years,
                results_json=results_list,
                pkl_filename="",  # placeholder, updated below
                algorithm="xgboost",
                description=f"ATS backtest {test_years[0]}-{test_years[-1]}",
            )

            # Save PKL files for each test year — only 2025 and 2026.
            # Each training session generates temp UUID-named PKLs in run_backtest,
            # then we permanently rename them here. Do NOT delete other runs' PKLs.
            pkl_names = []
            for r in total_results:
                year = r["test_year"]
                stable_name = f"{db_run_id}-{year}.pkl"
                # Find the temp PKL for this session/year (most recent file matching this session)
                temp_pkls = sorted(MLB_PKL_DIR.glob(f"*-{year}.pkl"),
                                   key=lambda p: p.stat().st_mtime, reverse=True)
                if temp_pkls:
                    try:
                        temp_pkls[0].rename(MLB_PKL_DIR / stable_name)
                        pkl_names.append(stable_name)
                        log(f"  Pkl saved: {stable_name}")
                    except FileNotFoundError:
                        log(f"  WARNING: temp pkl for {year} not found")

            if pkl_names:
                update_pkl_filename("mlb", db_run_id, ",".join(pkl_names))

            log(f"  Saved training run {db_run_id}: {len(total_results)} years")
        except Exception as e:
            log(f"  WARNING: failed to save training run: {e}")

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

    fcols = _ensure_ats_features()
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
        "ats_pick": "home" if (pred_margin + spread) > 0 else "away",
        "ou_pick": "over" if pred_margin > 4.5 else "under",
        "confidence": min(abs(pred_margin + spread) / 3, 0.95),
    }


async def train_model(
    year: int,
    train_years: list[int],
    feature_set: list[str] | None = None,
) -> dict:
    """Train and persist the ATS model for season ``year`` using ``train_years`` data."""
    df = get_data_loader().load_games(seasons=train_years, status="FINAL")
    feats = mlb_build_features(df)

    fcols = feature_set if feature_set is not None else _ensure_ats_features()
    present = [c for c in fcols if c in feats.columns]

    # Filter out games without betting lines
    train_mask = feats["spread"].notna() & feats["home_moneyline"].notna()
    feats = feats[train_mask].copy()

    target = feats["actual_margin"].values
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
    parser.add_argument("--test-year", type=int, default=None, help="Test year (default: CURRENT_YEAR)")
    parser.add_argument("--features", type=str, default="ats")
    parser.add_argument("--mode", type=str, default="one",
                        choices=["one", "all"])
    parser.add_argument("--train-from", type=int, default=2022, help="First training year")
    parser.add_argument("--test-until", type=int, default=None, help="Last test year (default: CURRENT_YEAR)")
    parser.add_argument("--skip-db", action="store_true", help="Skip saving to database")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    test_until = args.test_until or CURRENT_YEAR
    test_year = args.test_year or test_until

    if args.mode == "all":
        results = asyncio.run(run_all_years(
            feature_sets=[args.features],
            train_from=args.train_from,
            test_until=test_until,
            skip_db=args.skip_db,
        ))
        print(f"\n{'='*60}")
        print(f"Summary: {len(results)} backtests")
        for r in results:
            print(f"  {r['test_year']}: MAE={r['mae']:.3f}  ATS={r['ats']['pct']:.3f}  ML={r['ml']['pct']:.3f}")
    else:
        result = asyncio.run(run_single(args.test_year or test_year, args.features))
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("No results (check data)")
