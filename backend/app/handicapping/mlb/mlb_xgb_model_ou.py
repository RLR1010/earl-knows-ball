"""
mlb_xgb_model_ou.py — Over/Under XGBoost model for MLB

Refactored to use data_loader.py as the single source of truth for all
game and feature data. Structure matches mlb_xgb_model_ats.py.

Usage:
    python -m app.handicapping.mlb.mlb_xgb_model_ou --mode all
    python -m app.handicapping.mlb.mlb_xgb_model_ou --mode single --test-year 2026 --train-from 2016
"""

import json
import math
import os
import sys
import argparse
import asyncio
import uuid
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from app.handicapping.mlb.data_loader import get_data_loader, build_features as mlb_build_features, get_model_features

# ── Config ──
CURRENT_YEAR = 2026
DEFAULT_TIME_DECAY = 0.96


def _compute_decay_weights(df: pd.DataFrame, last_year: int, decay: float = DEFAULT_TIME_DECAY) -> np.ndarray:
    """Assign higher weight to more recent seasons."""
    years_ago = last_year - df["season_year"]
    return np.power(decay, years_ago)

# ── OU Feature Set (lazy, loaded on first use) ──
OU_FEATURES: list[str] = []
_ou_features_loaded = False


def _ensure_ou_features() -> list[str]:
    global OU_FEATURES, _ou_features_loaded
    if not _ou_features_loaded:
        try:
            OU_FEATURES = get_model_features("ou")
        except RuntimeError:
            OU_FEATURES = []
        _ou_features_loaded = True
    return OU_FEATURES


# ── DB helpers (lazy) ──
_DB_HELPERS_AVAILABLE = False
try:
    from app.handicapping.db_training import save_training_run, update_pkl_filename
    _DB_HELPERS_AVAILABLE = True
except ImportError:
    pass


# ── Model paths ──
MLB_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/mlb")
MLB_PKL_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    print(msg, flush=True)


# ── Core Training Logic ──

async def run_backtest(
    raw: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int,
    feature_set: list,
    train_years: list,
    training_id: str = None,
) -> dict | None:
    """Train on train_years, test on test_year, return result dict.

    Refactored to work with data_loader output.  features are pre-built
    by mlb_build_features() so that rolling windows are computed once
    across all years.
    """
    if training_id is None:
        training_id = uuid.uuid4().hex[:8]

    # Split by season_year
    train_feats = feats[feats["season_year"].isin(train_years)].copy()
    test_feats = feats[feats["season_year"] == test_year].copy()

    # Ensure all feature columns exist
    available = [c for c in feature_set if c in feats.columns]
    missing = [c for c in feature_set if c not in feats.columns]
    if missing:
        log(f"  Missing features: {missing}")

    # Filter out games without betting lines — they can't be used for OU training
    train_mask = train_feats["over_under"].notna()
    test_mask = test_feats["over_under"].notna()
    train_feats = train_feats[train_mask].copy()
    test_feats = test_feats[test_mask].copy()

    # Skip if too little data (protects the early-stopping eval split + fit)
    if len(train_feats) < 100 or len(test_feats) < 10:
        log(f"    Skipping {test_year}: train={len(train_feats)}, test={len(test_feats)}")
        return None

    X_train_full = train_feats[available].fillna(0).values
    y_train_full = train_feats["actual_total"].values
    ew_full = _compute_decay_weights(train_feats, max(train_years))

    # Train XGBoost with early stopping (same approach as ATS): hold out the
    # MOST RECENT ~15% of the training period as a time-ordered eval set (no
    # leakage — the model predicts a later period than it trains on), and stop
    # when rmse plateaus. XGB 3.x requires early_stopping_rounds in the
    # CONSTRUCTOR (removed from fit()).
    # NOTE (2026-08-19): hyperparameters aligned with the ATS model per Rich —
    # the OU model previously used different settings (n_estimators=500,
    # colsample_bytree=0.7, reg_lambda=1.5, reg_alpha=0.5) and ATS was
    # outperforming it. Same params + same early-stopping pattern now.
    model = xgb.XGBRegressor(
        n_estimators=600,
        max_depth=5,
        learning_rate=0.04,
        subsample=0.8,
        colsample_bytree=0.6,
        reg_lambda=1.0,
        gamma=0.1,
        min_child_weight=3,
        eval_metric="rmse",
        random_state=42,
        verbosity=0,
        early_stopping_rounds=30,
    )

    if "game_date" in train_feats.columns and len(train_feats) >= 200:
        idx = train_feats["game_date"].argsort().to_numpy()
        tf = train_feats.iloc[idx]
        X_f = tf[available].fillna(0).values
        y_f = tf["actual_total"].values
        ew_f = _compute_decay_weights(tf, max(train_years))
        n_eval = max(int(len(X_f) * 0.15), 50)
        model.fit(
            X_f[:-n_eval], y_f[:-n_eval],
            sample_weight=ew_f[:-n_eval],
            eval_set=[(X_f[-n_eval:], y_f[-n_eval:])],
            verbose=False,
        )
    else:
        # Fallback: no game_date / too few rows -> plain fit
        model.fit(X_train_full, y_train_full, sample_weight=ew_full)

    # --- Prediction / metrics (unchanged from original, restored after early-stopping fit) ---
    X_test = test_feats[available].fillna(0).values
    y_test = test_feats["actual_total"].values
    ous = test_feats["over_under"].values
    n_train = len(train_feats)
    n_test = len(test_feats)

    y_pred = model.predict(X_test)
    mae = float(np.mean(np.abs(y_pred - y_test)))
    rmse = float(np.sqrt(np.mean((y_pred - y_test) ** 2)))

    # Over/Under accuracy (excluding push games and games with no betting data)
    # ⚠ NaN closing_ou (NULL in DB) must be dropped — NaN comparisons always
    # return False, which erroneously inflates accuracy (e.g. 78.7% for 2021
    # when 58% of games had no betting line data).
    valid_ou = ~np.isnan(ous)
    y_pred_ou = y_pred[valid_ou]
    y_test_ou = y_test[valid_ou]
    ous_ou = ous[valid_ou]
    n_with_data = int(np.sum(valid_ou))

    pushes = y_test_ou == ous_ou
    non_push_mask = ~pushes
    n_non_push = int(np.sum(non_push_mask))
    n_pushes = int(np.sum(pushes))
    if n_non_push > 0:
        ou_correct = (y_pred_ou[non_push_mask] > ous_ou[non_push_mask]) == (y_test_ou[non_push_mask] > ous_ou[non_push_mask])
        ou_acc = float(np.mean(ou_correct))
        ou_count = int(np.sum(ou_correct))
    else:
        ou_acc = 0.0
        ou_count = 0

    # Feature importance (list of dicts, matching admin page format)
    feature_importance = [
        {"feature": f, "importance": round(float(imp), 6)}
        for f, imp in zip(available, model.feature_importances_)
    ]
    feature_importance.sort(key=lambda x: -x["importance"])

    # Save model PKL
    pkl_stem = f"{training_id}-{test_year}"
    pkl_path = MLB_PKL_DIR / f"{pkl_stem}.pkl"
    try:
        import joblib
        joblib.dump(model, pkl_path)
        log(f"  Model saved: {pkl_path.name}")
    except Exception as e:
        log(f"  WARNING: failed to save model: {e}")

    return {
        "test_year": test_year,
        "train_years": train_years,
        "n_train": n_train,
        "n_test": n_test,
        "total_games": n_test,
        "n_with_data": int(n_with_data),
        "mae": mae,
        "rmse": rmse,
        "ou": {"total": int(n_with_data), "non_push": n_non_push, "correct": ou_count, "incorrect": int(n_non_push - ou_count), "push": n_pushes, "pct": round(ou_acc * 100, 1)},
        "model_file": pkl_path.name,
        "feature_importance": feature_importance,
    }


async def run_all_years(
    train_from: int = 2016,
    feature_sets: list[list[str]] = None,
    skip_db: bool = False,
    do_save_training_run: bool = True,
) -> list[dict]:
    """Run OU backtest for 2025 and 2026 using walk-forward training.

    train_from = first train year (default 2016).
    test_years = [2021, 2022, 2023, 2024, 2025, 2026].
    """
    if feature_sets is None:
        feature_sets = [_ensure_ou_features()]

    test_until = CURRENT_YEAR
    test_years = [2021, 2022, 2023, 2024, 2025, 2026]

    # Load ONLY the years in the training window (train_from..max test year) so
    # changing train_from / the year range changes what is actually loaded instead
    # of loading all seasons and slicing in-memory. run_backtest slices feats by
    # season_year, so every usable year (train up to max_test-1 + max_test as test)
    # must be present.
    max_test_year = max(test_years) if test_years else CURRENT_YEAR
    load_seasons = list(range(train_from, max_test_year + 1))
    log(f"Loading MLB data ({len(load_seasons)} seasons: {load_seasons[0]}-{load_seasons[-1]})...")
    raw = get_data_loader().load_games(seasons=load_seasons, status="FINAL")
    feats = mlb_build_features(raw)
    log(f"  Loaded {len(raw)} games, {len(feats.columns)} columns")

    total_results = []

    for feature_set in feature_sets:
        for year in test_years:
            train_years = list(range(train_from, year))
            log(f"\n--- Testing {year} | Train {train_years[0]}-{train_years[-1]} "
                f"| {len(feature_set)} features ---")
            result = await run_backtest(raw, feats, year, feature_set, train_years)
            if result:
                total_results.append(result)
                ou = result["ou"]
                log(f"  Year {year}: MAE={result['mae']:.3f}, RMSE={result['rmse']:.3f}, "
                    f"OU={ou['pct']}% ({ou['correct']}/{ou['non_push']}{' +' + str(ou['push']) + ' push' if ou['push'] else ''})")

    # Save ONE training run with all years as a list in results_json
    if _DB_HELPERS_AVAILABLE and do_save_training_run and not skip_db and total_results:
        try:
            def _sanitize(obj):
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_sanitize(v) for v in obj]
                elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                    return None
                return obj

            results_list = [_sanitize(r) for r in total_results]
            for r, flat_entry in zip(total_results, results_list):
                year = r["test_year"]
                flat_entry["name"] = f"{year} MLB OU"
                flat_entry["ou_pct"] = r["ou"]["pct"]

            # Store the most recent (last) test year and its training years in the DB row
            last_test_year = test_years[-1]
            last_train_years = list(range(train_from, last_test_year))
            db_run_id = save_training_run(
                sport="mlb",
                model_type="ou",
                test_year=last_test_year,
                train_years=last_train_years,
                results_json=results_list,
                pkl_filename="",  # placeholder, updated below
                algorithm="xgboost",
                description=f"OU backtest {test_years[0]}-{test_years[-1]}",
            )

            # Rename temp pkls using DB id for stable naming.
            pkl_names = []
            for r in total_results:
                year = r["test_year"]
                stable_name = f"{db_run_id}-{year}.pkl"
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


# ── CLI ──

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MLB OU XGBoost Backtest")
    parser.add_argument("--mode", type=str, choices=["all", "single"], default="all",
                        help="'all' runs 2025+2026, 'single' runs one year")
    parser.add_argument("--test-year", type=int, default=2026,
                        help="Test year (default 2026; only used for --mode single)")
    parser.add_argument("--train-from", type=int, default=2016,
                        help="First training year (default 2016)")
    parser.add_argument("--skip-db", action="store_true",
                        help="Skip saving to database")


    args = parser.parse_args()

    if args.mode == "all":
        results = asyncio.run(run_all_years(
            train_from=args.train_from,
            skip_db=args.skip_db,
        ))
        if results:
            log("\n=== FINAL RESULTS ===")
            for r in results:
                ou = r["ou"]
                log(f"  {r['test_year']}: MAE={r['mae']:.3f}, OU={ou['pct']}% ({ou['correct']}/{ou['non_push']}{' +' + str(ou['push']) + ' push' if ou['push'] else ''})")
        else:
            log("No results generated.")
    else:
        log("No result generated.")



