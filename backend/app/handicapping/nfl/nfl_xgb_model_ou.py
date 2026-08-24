"""
NFL XGBoost Over/Under model - regression predicting total points.

Mirrors ``mlb/mlb_xgb_model_ou.py`` but adapted for the NFL schema
and NFL data loader. Predicts total game points (home + away) rather
than a binary over/under classification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from app.db_urls import PSYCOPG2_DATABASE_URL
import pickle
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from app.handicapping.db_training import save_training_run, update_pkl_filename
from app.handicapping.nfl.engine import _impute_feature
from app.handicapping.nfl.data_loader import (
    NFLDataLoader,
    get_data_loader,
    get_model_features,
)

logger = logging.getLogger(__name__)

# ── Model paths ─────────────────────────────────────────────────────────────────
# PKL directory for NFL models (matches MLB pattern: data/models/<sport>/)
NFL_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl")
OU_MODEL_PATH = NFL_PKL_DIR / "nfl_ou_best.pkl"
NFL_PKL_DIR.mkdir(parents=True, exist_ok=True)

# ── Training defaults ───────────────────────────────────────────────────────────
DEFAULT_N_ESTIMATORS = 600
DEFAULT_LEARNING_RATE = 0.04
DEFAULT_MAX_DEPTH = 5
DEFAULT_SUBSAMPLE = 0.8
DEFAULT_COL_SAMPLE = 0.6
DEFAULT_EARLY_STOPPING = 50
DEFAULT_TIME_DECAY = 0.96

CURRENT_YEAR = datetime.now().year
# PSYCOPG2_DATABASE_URL already reflects .env DATABASE_URL (asyncpg suffix stripped)
DB_DSN: str = PSYCOPG2_DATABASE_URL

# Module-level model cache for inference
_MODEL: Optional[xgb.Booster] = None


# ── Helper: ensure OU feature columns exist ─────────────────────────────────────
def _ensure_ou_features(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all NFL OU feature columns exist in the DataFrame."""
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            ou_features = get_model_features(cur, ou_only=True)
    finally:
        conn.close()

    for feat in ou_features:
        if feat not in df.columns:
            df[feat] = float("nan")
    return df


# ── Train model (async full pipeline) ────────────────────────────────────────────
TEST_YEARS = [2021, 2022, 2023, 2024, 2025]


def _train_years_for_test_year(test_year: int) -> List[int]:
    """Return the training years for the given test year.

    2024: trains on 2021, 2022, 2023
    2025: trains on 2021, 2022, 2023, 2024
    """
    return list(range(2016, test_year))


def _compute_decay_weights(df: pd.DataFrame, decay: float) -> np.ndarray:
    """Compute per-game sample weights via exponential time decay.

    More recent seasons get higher weight. Formula:
        weight = decay ^ (max_train_year - season_year)

    With decay=0.96 and max=2024:
        2024 -> 1.0,   2023 -> 0.96,   2022 -> 0.92,
        2021 -> 0.88,  2020 -> 0.85,   2019 -> 0.82,  ...
    """
    max_year = df["season_year"].max()
    return (decay ** (max_year - df["season_year"].values)).astype(np.float32)


async def train_model(
    model_path: Optional[Path] = None,
    hyperparams: Optional[Dict[str, Any]] = None,
    label: str = "nfl_ou_training",
) -> Dict[str, Any]:
    """Full OU training pipeline: trains one OU model per test year (2024, 2025),
    saves each model & its training run to the database."""
    t0 = time.time()

    model_type = "ou"
    model_dir = model_path if model_path else NFL_PKL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    dl = get_data_loader(ou_only=True)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df["total_points"] = df["home_score"] + df["away_score"]
    df = _ensure_ou_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    df_all = df.dropna(subset=["total_points"]).copy()

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "learning_rate": hp.get("learning_rate", DEFAULT_LEARNING_RATE),
        "max_depth": hp.get("max_depth", DEFAULT_MAX_DEPTH),
        "subsample": hp.get("subsample", DEFAULT_SUBSAMPLE),
        "colsample_bytree": hp.get("colsample_bytree", DEFAULT_COL_SAMPLE),
        "lambda": hp.get("lambda", 1.0),
        "alpha": hp.get("alpha", 0.0),
        "gamma": hp.get("gamma", 0.1),
        "min_child_weight": hp.get("min_child_weight", 3),
        "seed": 42,
        "verbosity": 0,
    }
    
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            feature_cols = get_model_features(cur, ou_only=True)
    finally:
        conn.close()

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)

    # Create the training run FIRST to get the training_id
    training_id = save_training_run(
        sport="nfl",
        model_type=model_type,
        test_year=TEST_YEARS[-1],
        train_years=_train_years_for_test_year(TEST_YEARS[-1]),
        results_json=[],
        pkl_filename="",
    )

    total_results = []

    for test_year in TEST_YEARS:
        ty_t0 = time.time()

        train_seasons = _train_years_for_test_year(test_year)
        logger.info("Training OU model for test_year=%d using train_years=%s", test_year, train_seasons)

        df_train = df_all[df_all["season_year"].isin(train_seasons)].copy()
        df_test = df_all[df_all["season_year"] == test_year].copy()

        # Drop games without closing OU - needed for OU evaluation
        df_train = df_train[df_train["closing_ou"].notna()].copy()
        df_test = df_test[df_test["closing_ou"].notna()].copy()

        if df_train.empty:
            logger.warning("No training data for test_year=%d, skipping", test_year)
            continue

        available = [c for c in feature_cols if c in df_train.columns]
        # Remove columns that are entirely NaN (never computed)
        available = [c for c in available if df_train[c].notna().any()]

        # Impute-first (NOT dropna): the loader leaves some features NaN at the
        # boundary of a team's season (week-1 season_ats_pct, a rookie/backup QB
        # with no last-5 window, implied probs, etc.). The engine's _impute_feature
        # fills a reasoned prior exactly as the live inference path does. Without
        # it, dropna() silently throws out real games and we under-train.
        _mask = df_train[available].isna()
        if _mask.any().any():
            _n_imputed = 0
            for feat in available:
                na = df_train[feat].isna()
                if na.any():
                    df_train.loc[na, feat] = df_train.loc[na, :].apply(
                        lambda row: _impute_feature(row, feat), axis=1
                    )
                    _n_imputed += int(na.sum())
            logger.info(
                "imputed %d NaN feature values across %d games (test_year=%s)",
                _n_imputed, int(_mask.any(axis=1).sum()), test_year,
            )

        # After reasoned imputation, drop only what is still unusable.
        df_train = df_train.dropna(subset=available)

        if df_train.empty:
            logger.warning("No training data after NaN filtering for test_year=%d, skipping", test_year)
            continue

        X = df_train[available].values
        y = df_train["total_points"].values

        # Time-decay sample weights - recent seasons weighted more
        decay_weights = _compute_decay_weights(df_train, decay=hp.get("time_decay", DEFAULT_TIME_DECAY))
        dtrain = xgb.DMatrix(X, label=y, weight=decay_weights, feature_names=available)

        model = xgb.train(params, dtrain, num_boost_round=n_estimators, verbose_eval=False)

        y_pred = model.predict(dtrain)
        train_mae = mean_absolute_error(y, y_pred)
        train_r2 = r2_score(y, y_pred)

        importance = model.get_score(importance_type="gain")
        total_gain = sum(importance.values()) or 1.0
        fi_sorted = sorted(
            [{"feature": k, "importance": round(v / total_gain, 6)} for k, v in importance.items()],
            key=lambda x: -x["importance"],
        )

        # OU accuracy: evaluate on test year
        ou_total = 0
        ou_correct = 0
        ou_push = 0

        if not df_test.empty and len(df_test) > 0:
            available_test = [c for c in feature_cols if c in df_test.columns]
            available_test = [c for c in available_test if df_test[c].notna().any()]
            # Impute-first for the holdout year too, so week-1/rookie-QB games are
            # scored instead of silently dropped (mirrors train + live inference).
            _mask_t = df_test[available_test].isna()
            if _mask_t.any().any():
                for feat in available_test:
                    na = df_test[feat].isna()
                    if na.any():
                        df_test.loc[na, feat] = df_test.loc[na, :].apply(
                            lambda row: _impute_feature(row, feat), axis=1
                        )
            df_test_clean = df_test.dropna(subset=available_test)

            if len(df_test_clean) > 0:
                X_test = df_test_clean[available].values
                y_test = df_test_clean["total_points"].values
                dtest = xgb.DMatrix(X_test, feature_names=available)
                pred_totals = model.predict(dtest)

                ou_total = len(y_test)
                if "closing_ou" in df_test_clean.columns:
                    closing_ou_values = df_test_clean["closing_ou"].values
                    for i in range(ou_total):
                        line = closing_ou_values[i]
                        total = y_test[i]
                        # A real over/under push requires the line to be a WHOLE
                        # number (e.g. 45.0) AND the (integer) final total to land
                        # EXACTLY on it. Final scores are always integers and the
                        # total is home+away, so it can never be a fraction; a .5
                        # line (45.5) can therefore never push. The <0.05 fudge
                        # this replaces was wrong logic that could only hold by
                        # accident. Determine over/under only for non-push lines.
                        is_push = (line % 1 == 0) and (int(total) == int(line))
                        if is_push:
                            ou_push += 1
                            continue
                        actual_over = total > line
                        pred_over = pred_totals[i] > line
                        if pred_over == actual_over:
                            ou_correct += 1

        ou_incorrect = ou_total - ou_correct - ou_push
        ou_non_push = ou_total - ou_push
        ou_pct = round(100 * ou_correct / max(ou_non_push, 1), 2)

        ty_elapsed = time.time() - ty_t0

        # Save .pkl with training_id in filename
        pkl_path = model_dir / f"{training_id}-{test_year}.pkl"
        with open(pkl_path, "wb") as f:
            pickle.dump(model, f)
        logger.info("OU pkl saved to %s for test_year=%d", pkl_path, test_year)

        ty_result = {
            "name": f"{test_year} NFL OU",
            "test_year": test_year,
            "total_games": ou_total,
            "mae": round(float(train_mae), 4),
            "r2": round(float(train_r2), 4),
            "input_features": list(available),
            "feature_importance": fi_sorted,
            "model_params": {**params, "n_estimators": n_estimators},
            "duration_seconds": round(ty_elapsed, 2),
            "ou": {
                "total": ou_total,
                "non_push": ou_non_push,
                "correct": ou_correct,
                "incorrect": ou_incorrect,
                "push": ou_push,
                "pct": ou_pct,
            },
            "ou_total": ou_total,
            "ou_correct": ou_correct,
            "ou_pct": ou_pct,
            "pkl_filename": pkl_path.name,
        }
        total_results.append(ty_result)

    if not total_results:
        return {"error": "no test years trained"}

    # Update the training_run with comma-separated pkl filenames and results_json
    all_pkl_names = ",".join(f"{training_id}-{ty}.pkl" for ty in TEST_YEARS)
    update_pkl_filename("nfl", training_id, all_pkl_names)

    # Update results_json via SQL
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE nfl.training_runs SET results_json = %s WHERE training_id = %s',
                (json.dumps(total_results, default=str), training_id)
            )
            conn.commit()
            logger.info("Updated results_json on training_run %s", training_id)
    except Exception as e:
        logger.error("Failed to update results_json: %s", e)
    finally:
        conn.close()

    elapsed = time.time() - t0

    return {
        "training_id": training_id,
        "label": label,
        "model_type": model_type,
        "test_years": TEST_YEARS,
        "n_results": len(total_results),
        "total_results": total_results,
        "elapsed_seconds": round(elapsed, 2),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "train"

    if mode == "train":
        result = asyncio.run(train_model(label="nfl_ou_cli"))
        print("\n=== NFL OU Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nfl_xgb_model_ou.py [backtest|train|single|predict]")
        sys.exit(1)
