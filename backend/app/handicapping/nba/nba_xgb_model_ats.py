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
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
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


# ── Helper: load ATS model ───────────────────────────────────────────────────────
def _load_ats_model(model_path: Optional[Path] = None) -> Optional[xgb.Booster]:
    """Load a pickled XGBoost model."""
    path = model_path
    if path.exists():
        with open(path, "rb") as f:
            model = pickle.load(f)
        logger.info("Loaded model from %s", path)
        return model
    logger.warning("Model not found at %s", path)
    return None


# ── Backtest ─────────────────────────────────────────────────────────────────────
def run_backtest(
    df: pd.DataFrame,
    test_year: int,
    ats_only: bool = True,
    ou_only: bool = False,
    hyperparams: Optional[Dict[str, Any]] = None,
    return_model: bool = False,
) -> Dict[str, Any]:
    """Train on seasons before ``test_year``, evaluate on ``test_year``.

    Parameters
    ----------
    df : Full feature DataFrame (must contain ``season_year`` column).
    test_year : Calendar year to hold out.
    ats_only : Train ATS model vs OU.
    ou_only : Train OU model.
    hyperparams : XGBoost param overrides.
    return_model : Include trained model in result.

    Returns dict with: year, accuracy, auc, log_loss, precision, recall,
    feature_importance, n_train, n_test, train_accuracy, model (optional).
    """
    t0 = time.time()

    train_df = df[df["season_year"] < test_year].copy()
    test_df = df[df["season_year"] == test_year].copy()

    # Drop games without spread — needed for ATS evaluation
    train_df = train_df[train_df["spread"].notna()].copy()
    test_df = test_df[test_df["spread"].notna()].copy()

    if train_df.empty or test_df.empty:
        logger.warning("Empty train (%d) or test (%d) for year %d", len(train_df), len(test_df), test_year)
        return {"year": test_year, "error": "insufficient data"}

    target = "home_actual_margin" if not ou_only else "home_actual_margin"
    if target not in train_df.columns and "home_score" in train_df.columns and "away_score" in train_df.columns:
        for df_t in [train_df, test_df]:
            df_t[target] = df_t["home_score"] - df_t["away_score"]
    if target not in train_df.columns:
        logger.error("Target column '%s' not found — skipping year %d", target, test_year)
        return {"year": test_year, "error": f"missing target '{target}'"}

    train_df = train_df.dropna(subset=[target])
    test_df = test_df.dropna(subset=[target])
    if train_df.empty or test_df.empty:
        return {"year": test_year, "error": "no labeled data"}

    if ats_only:
        feature_cols = get_model_features(target="ats")
    elif ou_only:
        feature_cols = get_model_features(target="ou")
    else:
        feature_cols = get_model_features()

    feature_cols = [c for c in feature_cols if c in train_df.columns]
    if not feature_cols:
        return {"year": test_year, "error": "no feature columns available"}

    train_df = train_df.dropna(subset=feature_cols)
    test_df = test_df.dropna(subset=feature_cols)

    X_train, y_train = train_df[feature_cols].values, train_df[target].values
    X_test, y_test = test_df[feature_cols].values, test_df[target].values

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

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=feature_cols)
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=feature_cols)

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)
    early_stop = hp.get("early_stopping_rounds", DEFAULT_EARLY_STOPPING)

    model = xgb.train(
        params, dtrain,
        num_boost_round=n_estimators,
        evals=[(dtest, "test")],
        early_stopping_rounds=early_stop,
        verbose_eval=False,
    )

    y_pred = model.predict(dtest)

    try:
        mae = float(mean_absolute_error(y_test, y_pred))
        rmse = float(np.sqrt(mean_squared_error(y_test, y_pred)))
        r2 = float(r2_score(y_test, y_pred))
    except Exception:
        mae = rmse = r2 = 0.0

    y_train_pred = model.predict(dtrain)
    train_mae = float(mean_absolute_error(y_train, y_train_pred))

    importance = model.get_score(importance_type="gain")
    total_gain = sum(importance.values()) or 1.0
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v / total_gain, 6)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    # Compute ATS and ML directional accuracy from margin predictions
    pred_margin = y_pred
    actual_margin = y_test

    # ML accuracy: did we pick the winner correctly?
    pred_home_wins = (pred_margin > 0).astype(int)
    actual_home_wins = (actual_margin > 0).astype(int)
    ml_correct = int((pred_home_wins == actual_home_wins).sum())
    ml_total = len(y_pred)
    ml_incorrect = ml_total - ml_correct

    # ATS accuracy: did we pick the spread cover correctly?
    # Note: DataFrame uses "spread" column (closing_spread renamed in build_features)
    if "spread" in test_df.columns:
        spread = test_df["spread"].values
        pred_home_covers = (pred_margin > -spread).astype(int)
        if "home_ats_cover" in test_df.columns:
            actual_home_covers = test_df["home_ats_cover"].values.astype(int)
        else:
            actual_home_covers = actual_home_wins
        ats_correct = int((pred_home_covers == actual_home_covers).sum())
        ats_total = ml_total
        ats_incorrect = ats_total - ats_correct
    else:
        ats_correct = ml_correct
        ats_total = ml_total
        ats_incorrect = ml_incorrect

    elapsed = round(time.time() - t0, 2)

    result: Dict[str, Any] = {
        "year": int(test_year),
        "test_year": int(test_year),
        "mae": round(mae, 4),
        "rmse": round(rmse, 4),
        "r2": round(r2, 4),
        "feature_importance": fi_sorted,
        "feature_set": feature_cols,
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "train_mae": round(train_mae, 4),
        "elapsed_seconds": elapsed,
        "duration_seconds": elapsed,
        "input_features": int(len(feature_cols)),
        "total_games": int(len(y_pred)),
        "model_params": {
            "seed": params["seed"],
            "max_depth": params["max_depth"],
            "objective": params["objective"],
            "subsample": params["subsample"],
            "verbosity": params["verbosity"],
            "eval_metric": params["eval_metric"],
            "n_estimators": hp.get("n_estimators", n_estimators),
            "learning_rate": params["learning_rate"],
            "colsample_bytree": params["colsample_bytree"],
        },
        "target": target,
        "ats": {
            "pct": round(ats_correct / ats_total * 100, 2),
            "total": ats_total,
            "correct": ats_correct,
            "incorrect": ats_incorrect,
        },
        "ml": {
            "pct": round(ml_correct / ml_total * 100, 2),
            "total": ml_total,
            "correct": ml_correct,
            "incorrect": ml_incorrect,
        },
        "ats_pct": round(ats_correct / ats_total * 100, 2),
        "ats_total": ats_total,
        "ats_correct": ats_correct,
    }

    if return_model:
        result["model"] = model

    logger.info(
        "Year %d | ats=%.1f%% ml=%.1f%% mae=%.4f | train=%d test=%d %.1fs",
        int(test_year),
        result["ats_pct"],
        result["ml"]["pct"],
        mae,
        len(X_train), len(X_test),
        elapsed,
    )

    return result


# ── Run all years ────────────────────────────────────────────────────────────────
async def run_all_years(
    train_from: int = 2016,
    ats_only: bool = True,
    ou_only: bool = False,
    limit: Optional[int] = None,
    hyperparams: Optional[Dict[str, Any]] = None,
    no_cache: bool = False,
) -> List[Dict[str, Any]]:
    """Backtest every year from ``train_from`` to current year.

    Parameters
    ----------
    train_from : Earliest calendar year for training data.
    ats_only : Train ATS model vs OU.
    ou_only : Train OU model.
    limit : Max games to load (for testing).
    hyperparams : XGBoost param overrides.
    no_cache : If True, skip model caching.

    Returns list of result dicts.
    """
    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)

    # Load all data from train_from onward
    df = dl.load_data(limit=limit)
    if df.empty:
        logger.error("No data loaded — check DB connection and season IDs")
        return []

    # Filter to train_from+ and sort
    df = df[df["season_year"] >= train_from].copy()
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

    df = _ensure_ats_features(df)

    unique_years = sorted(df["season_year"].unique())
    # Test only the last 2 years (standard default)
    test_years = [2021, 2022, 2023, 2024, 2025, 2026]
    logger.info("Test years: %s (all unique years: %s)", test_years, unique_years)

    import math

    total_results: List[Dict[str, Any]] = []
    for test_year in test_years:
        result = run_backtest(
            df,
            test_year,
            ats_only=ats_only,
            ou_only=ou_only,
            hyperparams=hyperparams,
            return_model=True,
        )
        if result.get("error"):
            logger.warning("Error for year %d: %s", test_year, result["error"])
            continue
        total_results.append(result)

    # Save training runs (matches expected results_json format)
    if total_results:
        def _sanitize(obj):
            if isinstance(obj, dict):
                return {k: _sanitize(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [_sanitize(v) for v in obj]
            elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                return None
            elif hasattr(obj, "item"):
                return obj.item()
            return obj

        try:
            results_list = [_sanitize(r) for r in total_results]
            for r, entry in zip(total_results, results_list):
                year = int(r["test_year"])
                entry["name"] = f"{year} NBA ATS"
                entry.pop("model", None)
                entry.pop("feature_set", None)

            last_result = results_list[-1]
            train_years = list(range(train_from, last_result["test_year"]))

            # Save training run first to get the training_id
            db_run_id = save_training_run(
                sport="nba",
                model_type="ats",
                test_year=last_result["test_year"],
                train_years=train_years,
                results_json=results_list,
                pkl_filename="",
            )
            logger.info("Saved training run %s: %d years", db_run_id, len(total_results))

            # Save PKL files and update pkl_filename on training run + each year entry
            pkl_names: list[str] = []
            for r, entry in zip(total_results, results_list):
                model = r.get("model")
                if model is not None:
                    year = int(r["test_year"])
                    pkl_name = f"{db_run_id}-{year}.pkl"
                    pkl_path = NBA_PKL_DIR / pkl_name
                    with open(pkl_path, "wb") as f:
                        pickle.dump(model, f)
                    entry["pkl_filename"] = pkl_name
                    pkl_names.append(pkl_name)
                    logger.info("  Saved PKL: %s", pkl_name)

            if pkl_names:
                update_pkl_filename("nba", db_run_id, ",".join(pkl_names))
                # Update results_json in the DB with pkl_filename per year
                import psycopg2
                conn = psycopg2.connect("postgresql://earl:earl2025@localhost:5432/earl_knows_football")
                cur = conn.cursor()
                cur.execute(
                    "UPDATE nba.training_runs SET results_json = %s WHERE training_id = %s",
                    (PgJson(results_list), db_run_id),
                )
                conn.commit()
                cur.close()
                conn.close()

        except Exception as e:
            import traceback
            logger.warning("Failed to save training run: %s", e)
            traceback.print_exc()

    return total_results


# ── Run single ───────────────────────────────────────────────────────────────────
def run_single(
    ats_only: bool = True,
    ou_only: bool = False,
    hyperparams: Optional[Dict[str, Any]] = None,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Train on all available data, save model, return metrics.

    Uses all years before ``CURRENT_YEAR`` for training and the
    current year as a test set.  The trained model is saved to
    ``model_path`` .
    """
    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df = _ensure_ats_features(df)
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

    # Train: all years < current; test: current year
    result = run_backtest(
        df,
        CURRENT_YEAR,
        ats_only=ats_only,
        ou_only=ou_only,
        hyperparams=hyperparams,
        return_model=True,
    )

    if "model" in result:
        path = model_path
        with open(path, "wb") as f:
            pickle.dump(result["model"], f)
        logger.info("Saved model to %s", path)

        test_year = CURRENT_YEAR
        train_seasons = list(range(2016, test_year))
        training_id = save_training_run(
            sport="nba",
            model_type="ats",
            test_year=test_year,
            train_years=train_seasons,
            results_json={
                "n_train": result.get("n_train", 0),
                "accuracy": result.get("accuracy", 0),
                "precision": result.get("precision", 0),
                "recall": result.get("recall", 0),
                "f1": result.get("f1", 0),
            },
            pkl_filename="",
        )
        result["training_id"] = training_id

        del result["model"]

    return result


# ── Predict single game ──────────────────────────────────────────────────────────
def predict_ats(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Predict ATS cover probability for a single game.

    Parameters
    ----------
    game_id : Game primary key.
    home_abbr : Home team abbreviation.
    away_abbr : Away team abbreviation.
    model_path : Custom model path.

    Returns dict with home_cover_prob, away_cover_prob, spread, ou, etc.
    """
    model = _load_ats_model(model_path)
    if model is None:
        return {"error": "no model loaded"}

    dl = get_data_loader()
    df = dl.load_inference_data(game_ids=[game_id])

    if df.empty:
        return {"error": "no features for game"}

    df = _ensure_ats_features(df)

    feature_cols = [f for f in df.columns if f != "season_year"]
    feature_cols = [c for c in feature_cols if c in model.feature_names]

    if not feature_cols:
        return {"error": "no matching feature columns"}

    features = df[feature_cols].values
    dmat = xgb.DMatrix(features, feature_names=feature_cols)
    prob = float(model.predict(dmat)[0])

    # Fetch game info from DB for display
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT g.home_score, g.away_score, blc.closing_spread, blc.closing_ou
                FROM nba.games g
                LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
                WHERE g.id = %s
                """,
                (game_id,),
            )
            row = cur.fetchone()
            home_score, away_score, spread, ou = row or (None, None, None, None)
    finally:
        conn.close()

    return {
        "game_id": game_id,
        "home_abbr": home_abbr,
        "away_abbr": away_abbr,
        "home_cover_prob": round(prob, 4),
        "away_cover_prob": round(1 - prob, 4),
        "spread": spread,
        "over_under": ou,
        "home_score": home_score,
        "away_score": away_score,
        "model_path": str(model_path),
    }


# ── Train model (async, full pipeline) ───────────────────────────────────────────
TEST_YEARS = [2021, 2022, 2023, 2024, 2025, 2026]


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

    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        results = asyncio.run(run_all_years(train_from=2016))
        print("\n=== NBA ATS Backtest Results ===")
        for r in results:
            if "error" in r:
                print(f"  {r['year']}: ERROR — {r['error']}")
            else:
                print(f"  {r['year']}: mae={r['mae']:.4f} ats={r['ats_pct']:.1f}% ml={r['ml']['pct']:.1f}%  n={r['n_train']}+{r['n_test']}")

    elif mode == "train":
        result = asyncio.run(train_model(label="nba_cli_training"))
        print("\n=== NBA Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    elif mode == "single":
        result = run_single()
        print("\n=== NBA Single Model ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            else:
                print(f"  {k}: {v}")

    elif mode == "predict":
        if len(sys.argv) < 4:
            print("Usage: python nba_xgb_model_ats.py predict <game_id> <home_abbr> <away_abbr>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        home_abbr = sys.argv[3].upper()
        away_abbr = sys.argv[4].upper()
        result = predict_ats(game_id, home_abbr, away_abbr)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nba_xgb_model_ats.py [backtest|train|single|predict]")
        sys.exit(1)
