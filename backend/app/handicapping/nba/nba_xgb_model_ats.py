"""
NBA XGBoost ATS/OU model — train, backtest, and predict.

Mirrors ``nfl/nfl_xgb_model_ats.py`` but adapted for the NBA schema
and NBA data loader.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from app.db_urls import PSYCOPG2_DATABASE_URL
import pickle
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import Json as PgJson
import xgboost as xgb
from sklearn.metrics import (
    mean_absolute_error,
    mean_squared_error,
    r2_score,
)

from app.handicapping.db_training import save_training_run, update_pkl_filename
from app.handicapping.nba.data_loader import (
    FEATURES_CATALOG,
    NBADataLoader,
    get_data_loader,
    get_model_features,
)

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
# PKL directory for NBA models (matches MLB pattern: data/models/<sport>/)
NBA_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nba")
NBA_PKL_DIR.mkdir(parents=True, exist_ok=True)

# ── Training constants ──────────────────────────────────────────────────────────
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_MAX_DEPTH = 6
DEFAULT_N_ESTIMATORS = 800
DEFAULT_EARLY_STOPPING = 50
DEFAULT_SUBSAMPLE = 0.8
DEFAULT_COL_SAMPLE = 0.8

CURRENT_YEAR = datetime.now().year
NBA_SCHEMA = "nba"
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    PSYCOPG2_DATABASE_URL,
)


# ── Helper: ensure ATS feature columns exist ────────────────────────────────────
def _ensure_ats_features(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all NBA ATS feature columns exist in the DataFrame.

    Only adds missing features that are actually computed by the data loader
    (present in ``df`` after ``load_data``).  NaN-only columns would cause
    ``dropna()`` to erase every row in the trainer.
    """
    ats_features = get_model_features(target="ats")

    # Fill missing features with 0 (neutral for tree models) instead of NaN,
    # so ``dropna()`` later does not erase every row.
    for feat in ats_features:
        if feat not in df.columns:
            df[feat] = 0.0
        elif df[feat].isna().all():
            df[feat] = df[feat].fillna(0.0)

    return df




# ── Train model (async, full pipeline) ───────────────────────────────────────────
TEST_YEARS = [2021, 2022, 2023, 2024, 2025]


def _train_years_for_test_year(test_year: int) -> List[int]:
    """Return the training years for the given test year.

    2024: trains on 2021, 2022, 2023
    2025: trains on 2021, 2022, 2023, 2024
    """
    return list(range(2016, test_year))


async def train_model(
    model_path: Optional[Path] = None,
    ats_only: bool = True,
    ou_only: bool = False,
    hyperparams: Optional[Dict[str, Any]] = None,
    label: str = "nba_ats_training",
) -> Dict[str, Any]:
    """Full training pipeline: trains ATS model for each test year (2024, 2025),
    saves models and a single training run to the database.

    `results_json` format matches the MLB pattern: a list of per-test-year results,
    each containing ats and ml accuracy, feature importance, and model params.
    """
    overall_t0 = time.time()

    model_type = "ats"
    model_dir = model_path if model_path else NBA_PKL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df = _ensure_ats_features(df)
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

    # Regression target: actual margin (home_score - away_score). Compute from db columns.
    if ou_only:
        target = "home_actual_margin"
        if target not in df.columns and "home_score" in df.columns and "away_score" in df.columns:
            df["home_actual_margin"] = df["home_score"] - df["away_score"]
    else:
        target = "home_actual_margin"
        if target not in df.columns and "home_score" in df.columns and "away_score" in df.columns:
            df["home_actual_margin"] = df["home_score"] - df["away_score"]
    df_all = df.dropna(subset=[target]).copy()

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "reg:squarederror",
        "eval_metric": "mae",
        "learning_rate": hp.get("learning_rate", DEFAULT_LEARNING_RATE),
        "max_depth": hp.get("max_depth", DEFAULT_MAX_DEPTH),
        "subsample": hp.get("subsample", DEFAULT_SUBSAMPLE),
        "colsample_bytree": hp.get("colsample_bytree", DEFAULT_COL_SAMPLE),
        "seed": 42,
        "verbosity": 0,
    }

    if ats_only:
        feature_cols = get_model_features(target="ats")
    elif ou_only:
        feature_cols = get_model_features(target="ou")
    else:
        feature_cols = get_model_features()

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)

    total_results = []
    pkl_filenames = []
    last_train_years = None
    last_test_year = None

    # Save the training run FIRST to get the training_id (generated by DB)
    training_id = save_training_run(
        sport="nba",
        model_type=model_type,
        results_json=[],
        pkl_filename="",
        test_year=TEST_YEARS[-1],
        train_years=_train_years_for_test_year(TEST_YEARS[-1]),
    )

    for test_year in TEST_YEARS:
        ty_t0 = time.time()

        train_seasons = _train_years_for_test_year(test_year)
        logger.info("Training ATS model for test_year=%d using train_years=%s", test_year, train_seasons)

        df_train = df_all[df_all["season_year"].isin(train_seasons)].copy()
        df_test = df_all[df_all["season_year"] == test_year].copy()

        # Drop games without spread — needed for ATS evaluation
        df_train = df_train[df_train["spread"].notna()].copy()
        df_test = df_test[df_test["spread"].notna()].copy()

        if df_train.empty:
            logger.warning("No training data for test_year=%d, skipping", test_year)
            continue

        available = [c for c in feature_cols if c in df_train.columns]
        df_train = df_train.dropna(subset=available)

        X_train = df_train[available].values
        y_train = df_train[target].values

        dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=available)

        model = xgb.train(params, dtrain, num_boost_round=n_estimators, verbose_eval=False)

        # Training MAE
        y_pred_train = model.predict(dtrain)
        train_mae = float(mean_absolute_error(y_train, y_pred_train))

        importance = model.get_score(importance_type="gain")
        total_gain = sum(importance.values()) or 1.0
        fi_sorted = sorted(
            [{"feature": k, "importance": round(v / total_gain, 6)} for k, v in importance.items()],
            key=lambda x: -x["importance"],
        )

        # Test evaluation – predict margin, then compute ATS/ML accuracy from margin against spread
        ats_total = 0
        ats_correct = 0
        ml_total = 0
        ml_correct = 0
        test_mae = 0.0

        if not df_test.empty and len(df_test) > 0:
            available_test = [c for c in feature_cols if c in df_test.columns]
            df_test_clean = df_test.dropna(subset=available_test)

            if len(df_test_clean) > 0:
                X_test = df_test_clean[available_test].values
                y_test = df_test_clean[target].values
                dtest = xgb.DMatrix(X_test, feature_names=available_test)
                pred_margins = model.predict(dtest)
                test_mae = float(mean_absolute_error(y_test, pred_margins))

                # ATS: model picks home if predicted margin > -(spread), away otherwise
                if "spread" in df_test_clean.columns and "home_actual_margin" in df_test_clean.columns:
                    spreads = df_test_clean["spread"].values
                    actual_margins = df_test_clean["home_actual_margin"].values
                    ats_pick_home = pred_margins > -(spreads)
                    ats_cover = (actual_margins > -(spreads)).astype(int)
                    ats_pred = ats_pick_home.astype(int)
                    ats_total = len(ats_pred)
                    ats_correct = int((ats_pred == ats_cover).sum())

                    # ML: model picks home if margin > 0
                    home_won = (actual_margins > 0).astype(int)
                    ml_pred = (pred_margins > 0).astype(int)
                    ml_total = len(ml_pred)
                    ml_correct = int((ml_pred == home_won).sum())

        ats_incorrect = ats_total - ats_correct
        ats_pct = round(100 * ats_correct / ats_total, 2) if ats_total > 0 else 0.0
        ml_incorrect = ml_total - ml_correct
        ml_pct = round(100 * ml_correct / ml_total, 2) if ml_total > 0 else 0.0

        ty_elapsed = time.time() - ty_t0

        # Save model pkl with training_id in filename
        pkl_path = model_dir / f"{training_id}-{test_year}.pkl"
        with open(pkl_path, "wb") as f:
            pickle.dump(model, f)
        logger.info("ATS model saved to %s for test_year=%d", pkl_path, test_year)

        ty_result = {
            "name": f"{test_year} NBA ATS",
            "test_year": test_year,
            "total_games": ats_total,
            "mae": round(float(test_mae), 4),
            "input_features": len(available),
            "feature_importance": fi_sorted,
            "model_params": {**params, "n_estimators": n_estimators},
            "duration_seconds": round(ty_elapsed, 2),
            "ats": {
                "total": ats_total,
                "correct": ats_correct,
                "incorrect": ats_incorrect,
                "pct": ats_pct,
            },
            "ml": {
                "total": ml_total,
                "correct": ml_correct,
                "incorrect": ml_incorrect,
                "pct": ml_pct,
            },
            "ats_total": ats_total,
            "ats_correct": ats_correct,
            "ats_pct": ats_pct,
            "pkl_filename": pkl_path.name,
        }
        total_results.append(ty_result)

    if not total_results:
        return {"error": "no test years trained"}

    # Update the training_run row with results_json and comma-separated pkl filenames
    all_pkl_names = ",".join(f"{training_id}-{ty}.pkl" for ty in TEST_YEARS)
    update_pkl_filename("nba", training_id, all_pkl_names)

    # Also update results_json on the row (save_training_run was called with empty json)
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f'UPDATE nba.training_runs SET results_json = %s WHERE training_id = %s',
                (json.dumps(total_results, default=str), training_id)
            )
            conn.commit()
            logger.info("Updated results_json on training_run %s", training_id)
    except Exception as e:
        logger.error("Failed to update results_json: %s", e)
    finally:
        conn.close()

    overall_elapsed = time.time() - overall_t0

    return {
        "training_id": training_id,
        "label": label,
        "model_type": model_type,
        "test_years": TEST_YEARS,
        "n_results": len(total_results),
        "total_results": total_results,
        "elapsed_seconds": round(overall_elapsed, 2),
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
        result = asyncio.run(train_model(label="nba_cli_training"))
        print("\n=== NBA Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nba_xgb_model_ats.py [backtest|train|single|predict]")
        sys.exit(1)
