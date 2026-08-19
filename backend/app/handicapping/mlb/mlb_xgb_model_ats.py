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

from app.db_urls import PSYCOPG2_DATABASE_URL

log = logging.getLogger(__name__).info

# ── Sync DB URL for inference path ──
DSN = os.environ.get(
    "DATABASE_URL",
    PSYCOPG2_DATABASE_URL,
)

# ── Model globals ──
ATS_MODEL_PATH = os.path.join(os.path.dirname(__file__), "ats_model.pkl")

# PKL directory for MLB models
MLB_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
MLB_PKL_DIR.mkdir(parents=True, exist_ok=True)

_ats_model = None
_ats_feature_cache: Optional[pd.DataFrame] = None
CURRENT_YEAR = datetime.now().year
DEFAULT_TIME_DECAY = 0.96


def _compute_decay_weights(df: pd.DataFrame, last_year: int, decay: float = DEFAULT_TIME_DECAY) -> np.ndarray:
    """Assign higher weight to more recent seasons."""
    years_ago = last_year - df["season_year"]
    return np.power(decay, years_ago)


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

    # --- Early stopping ---
    # Hold out the MOST RECENT ~15% of the training period as an eval set
    # (time-ordered, so no leakage — the model must predict a later period than
    # it trains on). Stop when rmse plateaus, which avoids burning all 600 trees
    # on small/early train sets (those were ~90s wasted, and 600 trees on a few
    # thousand rows overfits). XGBoost with early_stopping_rounds auto-selects
    # best_iteration.
    X_train_full = train_feats[present].values
    y_train_full = train_feats["actual_margin"].values
    ew_full = _compute_decay_weights(train_feats, max(train_years))

    model = xgb.XGBRegressor(
        n_estimators=600,
        max_depth=5,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.6,
        reg_lambda=1.0,
        gamma=0.1,
        min_child_weight=3,
        random_state=42,
        verbosity=0,
        eval_metric="rmse",
        # XGB 3.x: early_stopping_rounds is a CONSTRUCTOR arg (removed from
        # fit()). Runs with eval_set stop when eval rmse plateaus -> avoids
        # burning all 600 trees on small/early train sets (overfit + slow).
        early_stopping_rounds=30,
    )

    if "game_date" in train_feats.columns and len(train_feats) >= 200:
        idx = train_feats["game_date"].argsort().to_numpy()
        train_feats_sorted = train_feats.iloc[idx]
        X_f = train_feats_sorted[present].values
        y_f = train_feats_sorted["actual_margin"].values
        ew_f = _compute_decay_weights(train_feats_sorted, max(train_years))
        n_eval = max(int(len(X_f) * 0.15), 50)
        X_eval = X_f[-n_eval:]
        y_eval = y_f[-n_eval:]
        ew_eval = ew_f[-n_eval:]
        X_train = X_f[:-n_eval]
        y_train = y_f[:-n_eval]
        ew_train = ew_f[:-n_eval]
        model.fit(
            X_train, y_train,
            sample_weight=ew_train,
            eval_set=[(X_eval, y_eval)],
            verbose=False,
        )
    else:
        # Fallback: no date column or too few rows -> plain fit
        model.fit(X_train_full, y_train_full, sample_weight=ew_full)

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
    # Drop games with NaN spread (no betting data available) — NaN comparisons
    # always return False, deflating accuracy.
    valid_spread = ~np.isnan(spread)
    y_pred_ats = y_pred[valid_spread]
    y_test_ats = y_test[valid_spread]
    spread_ats = spread[valid_spread]
    n_ats_with_data = int(np.sum(valid_spread))

    # ATS: home team covers if margin + spread > 0 (i.e., actual result beats the spread).
    # The spread is from the home team's perspective: negative = home favored, positive = home underdog.
    ats_correct = np.sign(y_pred_ats + spread_ats) == np.sign(y_test_ats + spread_ats)
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
        "n_ats_with_data": n_ats_with_data,
        "mae": round(float(mae), 3),
        "ats": {
            "total": n_ats_with_data,
            "correct": n_correct_ats,
            "incorrect": n_ats_with_data - n_correct_ats,
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
    train_from: int = 2016,
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
    test_years = [2021, 2022, 2023, 2024, 2025, 2026]

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



# ── CLI ──
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser("MLB ATS Backtest")
    parser.add_argument("--test-year", type=int, default=None, help="Test year (default: CURRENT_YEAR)")
    parser.add_argument("--features", type=str, default="ats")
    parser.add_argument("--mode", type=str, default="all",
                        choices=["one", "all"])
    parser.add_argument("--train-from", type=int, default=2016, help="First training year")
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
        print("No results (check data)")
