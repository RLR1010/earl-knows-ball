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
from app.handicapping.nba.nba_engine import _impute_feature
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
# Hyperparameters aligned with the MLB trainers (mlb_xgb_model_ats/ou) so the
# NBA models share the same boosting settings across sports.
DEFAULT_LEARNING_RATE = 0.04
DEFAULT_MAX_DEPTH = 5
DEFAULT_N_ESTIMATORS = 600
DEFAULT_EARLY_STOPPING = 50
DEFAULT_SUBSAMPLE = 0.8
DEFAULT_COL_SAMPLE = 0.6
DEFAULT_REG_LAMBDA = 1.0
DEFAULT_GAMMA = 0.1
DEFAULT_MIN_CHILD_WEIGHT = 3
DEFAULT_TIME_DECAY = 0.96

CURRENT_YEAR = datetime.now().year
NBA_SCHEMA = "nba"
# PSYCOPG2_DATABASE_URL already reflects .env DATABASE_URL (asyncpg suffix stripped)
DB_DSN: str = PSYCOPG2_DATABASE_URL


# ── Helper: decay sample weights (mirrors MLB trainers) ────────────────────────
def _compute_decay_weights(
    df: pd.DataFrame, last_year: int, decay: float = DEFAULT_TIME_DECAY
) -> np.ndarray:
    """Assign higher weight to more recent seasons (same as mlb_xgb_model_ats)."""
    years_ago = last_year - df["season_year"]
    return np.power(decay, years_ago)


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
    # Train from 2016 up through the latest test year; earlier seasons are never
    # used by the train/test split (train starts at 2016) and prior-season stats
    # come from nba.prior_team_stats, so 2007-2015 game rows are pure dead cost.
    load_seasons = list(range(2016, max(TEST_YEARS) + 1))
    # Train ONLY on regular-season games. Playoff/play-in games distort the
    # ATS/OU margin targets (different pace, intensity, and rest context), so
    # the margin regression model is fit on REG wins only. Inference still
    # scores playoff/play-in games (via load_inference_data, unfiltered).
    df = dl.load_data(seasons=load_seasons, game_types=['REG'])

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
        "eval_metric": "rmse",
        "learning_rate": hp.get("learning_rate", DEFAULT_LEARNING_RATE),
        "max_depth": hp.get("max_depth", DEFAULT_MAX_DEPTH),
        "subsample": hp.get("subsample", DEFAULT_SUBSAMPLE),
        "colsample_bytree": hp.get("colsample_bytree", DEFAULT_COL_SAMPLE),
        "reg_lambda": hp.get("reg_lambda", DEFAULT_REG_LAMBDA),
        "gamma": hp.get("gamma", DEFAULT_GAMMA),
        "min_child_weight": hp.get("min_child_weight", DEFAULT_MIN_CHILD_WEIGHT),
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
        available = [c for c in available if df_train[c].notna().any()]

        # Impute-first (NOT blind dropna): NBA loader leaves week-1 / early-season
        # games legitimately NaN on a few features. nba_engine._impute_feature fills
        # a reasoned prior (season-cumulative avg, carry, league-avg) exactly as for
        # live inference, so we don't silently throw away real games while training.
        _mask = df_train[available].isna()
        if _mask.any().any():
            for feat in available:
                na = df_train[feat].isna()
                if na.any():
                    df_train.loc[na, feat] = df_train.loc[na, :].apply(
                        lambda row: _impute_feature(row, feat), axis=1
                    )
            logger.info("imputed NaN features across %d training games (test_year=%s)",
                        int(_mask.any(axis=1).sum()), test_year)
        df_train = df_train.dropna(subset=available)

        X_train = df_train[available].values
        y_train = df_train[target].values

        # Time-decay sample weights — more recent seasons matter more (same as MLB).
        if "season_year" in df_train.columns:
            sample_weights = _compute_decay_weights(df_train, train_seasons[-1])
        else:
            sample_weights = np.ones(len(df_train))

        # Early stopping (matches MLB): hold out the MOST RECENT ~15% of the
        # training period as a time-ordered eval set (no leakage — the model
        # predicts a later period). xgb.train (native API) still accepts
        # early_stopping_rounds + evals even in XGB 3.x.
        gd = "date" if "date" in df_train.columns else None
        if gd is not None and len(df_train) >= 200:
            _sort_idx = df_train[gd].argsort().to_numpy()
            _tf = df_train.iloc[_sort_idx]
            _X = _tf[available].values
            _y = _tf[target].values
            if "season_year" in _tf.columns:
                _w = _compute_decay_weights(_tf, train_seasons[-1])
            else:
                _w = np.ones(len(_tf))
            _n_eval = max(int(len(_X) * 0.15), 50)
            dtrain = xgb.DMatrix(
                _X[:-_n_eval], label=_y[:-_n_eval], weight=_w[:-_n_eval],
                feature_names=available,
            )
            dvalid = xgb.DMatrix(
                _X[-_n_eval:], label=_y[-_n_eval:], weight=_w[-_n_eval:],
                feature_names=available,
            )
            model = xgb.train(
                params, dtrain, num_boost_round=n_estimators, verbose_eval=False,
                evals=[(dvalid, "valid")],
                early_stopping_rounds=DEFAULT_EARLY_STOPPING,
            )
        else:
            dtrain = xgb.DMatrix(
                X_train, label=y_train, weight=sample_weights, feature_names=available
            )
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
            available_test = [c for c in available_test if df_test[c].notna().any()]
            # Impute-first for the holdout year too, so early-season games are scored
            # instead of silently dropped (mirrors live inference).
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
