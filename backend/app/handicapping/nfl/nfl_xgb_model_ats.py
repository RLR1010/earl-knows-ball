"""
NFL XGBoost ATS/OU model — train, backtest, and predict.

Mirrors ``mlb/mlb_xgb_model_ats.py`` but adapted for the NFL schema
and NFL data loader.
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
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, mean_squared_error

from app.handicapping.db_training import save_training_run, update_pkl_filename
from app.handicapping.nfl.data_loader import (
    FEATURES_CATALOG,
    NFLDataLoader,
    get_data_loader,
    get_model_features,
)

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
# PKL directory for NFL models (matches MLB pattern: data/models/<sport>/)
NFL_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl")
ATS_MODEL_PATH = NFL_PKL_DIR / "nfl_ats_best.pkl"
OU_MODEL_PATH = NFL_PKL_DIR / "nfl_ou_best.pkl"
NFL_PKL_DIR.mkdir(parents=True, exist_ok=True)

# ── Training constants ──────────────────────────────────────────────────────────
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_MAX_DEPTH = 6
DEFAULT_N_ESTIMATORS = 800
DEFAULT_EARLY_STOPPING = 50
DEFAULT_SUBSAMPLE = 0.8
DEFAULT_COL_SAMPLE = 0.8

CURRENT_YEAR = datetime.now().year
NFL_SCHEMA = "nfl"
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)


# ── Helper: ensure ATS feature columns exist ────────────────────────────────────
def _ensure_ats_features(df: pd.DataFrame) -> pd.DataFrame:
    """Ensure all NFL ATS feature columns exist in the DataFrame.

    Relies on the NFL ``nfl.features`` table via ``get_model_features()``.
    """
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            ats_features = get_model_features(cur, ats_only=True)
    finally:
        conn.close()

    for feat in ats_features:
        if feat not in df.columns:
            # Fill missing with 0 (neutral for tree models) instead of NaN.
            # NaN would trigger dropna() and drop rows — or erase the entire
            # dataset if the column is entirely missing.  The engine's
            # _extract_feature_vector fills NaN with 0.0, so we must match.
            df[feat] = 0.0
        elif df[feat].isna().all():
            df[feat] = df[feat].fillna(0.0)

    return df


# ── Helper: load ATS model ───────────────────────────────────────────────────────
def _load_ats_model(model_path: Optional[Path] = None) -> Optional[xgb.Booster]:
    """Load a pickled XGBoost model."""
    path = model_path or ATS_MODEL_PATH
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

    Returns dict with: year, rmse, mae, train_rmse, train_mae,
    feature_importance, n_train, n_test, model (optional).
    """
    t0 = time.time()

    train_df = df[df["season_year"] < test_year].copy()
    test_df = df[df["season_year"] == test_year].copy()

    if train_df.empty or test_df.empty:
        logger.warning("Empty train (%d) or test (%d) for year %d", len(train_df), len(test_df), test_year)
        return {"year": test_year, "error": "insufficient data"}

    target = "home_score_margin"
    if target not in train_df.columns:
        logger.error("Target column '%s' not found — skipping year %d", target, test_year)
        return {"year": test_year, "error": f"missing target '{target}'"}

    train_df = train_df.dropna(subset=[target])
    test_df = test_df.dropna(subset=[target])
    if train_df.empty or test_df.empty:
        return {"year": test_year, "error": "no labeled data"}

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            if ats_only:
                feature_cols = get_model_features(cur, ats_only=True)
            elif ou_only:
                feature_cols = get_model_features(cur, ou_only=True)
            else:
                feature_cols = get_model_features(cur)
    finally:
        conn.close()

    feature_cols = [c for c in feature_cols if c in train_df.columns]
    if not feature_cols:
        return {"year": test_year, "error": "no feature columns available"}

    # Fill NaN with 0 to match engine's _extract_feature_vector behavior
    for feat in feature_cols:
        if feat not in test_df.columns:
            test_df[feat] = 0.0
        test_df[feat] = test_df[feat].fillna(0.0)
        if feat in train_df.columns:
            train_df[feat] = train_df[feat].fillna(0.0)

    # Only drop rows where target is NaN (features already filled)
    train_df = train_df.dropna(subset=[target])
    test_df = test_df.dropna(subset=[target])

    X_train, y_train = train_df[feature_cols].values, train_df[target].values
    X_test, y_test = test_df[feature_cols].values, test_df[target].values

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
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

    y_pred_margin = model.predict(dtest)

    try:
        rmse_val = float(np.sqrt(mean_squared_error(y_test, y_pred_margin)))
        mae_val = float(mean_absolute_error(y_test, y_pred_margin))
    except Exception:
        rmse_val = mae_val = 0.0

    y_train_margins = model.predict(dtrain)
    train_rmse = float(np.sqrt(mean_squared_error(y_train, y_train_margins)))
    train_mae = float(mean_absolute_error(y_train, y_train_margins))

    importance = model.get_score(importance_type="gain")
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v, 4)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    result: Dict[str, Any] = {
        "year": test_year,
        "rmse": round(float(rmse_val), 4),
        "mae": round(float(mae_val), 4),
        "train_rmse": round(float(train_rmse), 4),
        "train_mae": round(float(train_mae), 4),
        "feature_importance": fi_sorted,
        "feature_set": feature_cols,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "elapsed_seconds": round(time.time() - t0, 2),
        "target": target,
    }

    if return_model:
        result["model"] = model

    logger.info(
        "Year %d | rmse=%.4f mae=%.4f | train=%d test=%d %.1fs",
        test_year, result["rmse"], result["mae"], len(X_train), len(X_test), result["elapsed_seconds"],
    )

    return result


# ── Run all years ────────────────────────────────────────────────────────────────
async def run_all_years(
    train_from: int = 2021,
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
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    df = _ensure_ats_features(df)

    test_years = sorted(df["season_year"].unique())
    logger.info("Test years: %s", test_years)

    results: List[Dict[str, Any]] = []
    for test_year in test_years:
        if test_year == min(test_years):
            logger.info("Skipping first year %d (no train data before it)", test_year)
            continue

        result = run_backtest(
            df,
            test_year,
            ats_only=ats_only,
            ou_only=ou_only,
            hyperparams=hyperparams,
            return_model=False,
        )
        results.append(result)

    return results


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
    ``model_path`` (default ``ATS_MODEL_PATH``).
    """
    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df = _ensure_ats_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

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
        path = model_path or (OU_MODEL_PATH if ou_only else ATS_MODEL_PATH)
        with open(path, "wb") as f:
            pickle.dump(result["model"], f)
        logger.info("Saved model to %s", path)

        test_year = CURRENT_YEAR
        train_seasons = list(range(2015, test_year))
        training_id = save_training_run(
            sport="nfl",
            model_type="ats",
            test_year=test_year,
            train_years=train_seasons,
            results_json={
                "n_train": result.get("n_train", 0),
                "rmse": result.get("rmse", 0),
                "mae": result.get("mae", 0),
                "train_rmse": result.get("train_rmse", 0),
                "train_mae": result.get("train_mae", 0),
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
                FROM nfl.games g
                LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
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
        "model_path": str(model_path or ATS_MODEL_PATH),
    }


# ── Train model (async, full pipeline) ───────────────────────────────────────────
TEST_YEARS = [2024, 2025]


def _train_years_for_test_year(test_year: int) -> List[int]:
    """Return the training years for the given test year.

    2024: trains on 2021, 2022, 2023
    2025: trains on 2021, 2022, 2023, 2024
    """
    return list(range(2021, test_year))


async def train_model(
    model_path: Optional[Path] = None,
    ats_only: bool = True,
    ou_only: bool = False,
    hyperparams: Optional[Dict[str, Any]] = None,
    label: str = "nfl_ats_training",
) -> Dict[str, Any]:
    """Full training pipeline: trains ATS model for each test year (2024, 2025),
    saves models and a single training run to the database.

    `results_json` format matches the MLB pattern: a list of per-test-year results,
    each containing ats/ml accuracy, rmse/mae, feature importance, and model params.
    """
    overall_t0 = time.time()

    model_type = "ats"
    model_dir = model_path if model_path else NFL_PKL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df = _ensure_ats_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    target = "home_score_margin"
    spread_col = "closing_spread"
    df_all = df.dropna(subset=[target]).copy()

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "reg:squarederror",
        "eval_metric": "rmse",
        "learning_rate": hp.get("learning_rate", DEFAULT_LEARNING_RATE),
        "max_depth": hp.get("max_depth", DEFAULT_MAX_DEPTH),
        "subsample": hp.get("subsample", DEFAULT_SUBSAMPLE),
        "colsample_bytree": hp.get("colsample_bytree", DEFAULT_COL_SAMPLE),
        "seed": 42,
        "verbosity": 0,
    }

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            if ats_only:
                feature_cols = get_model_features(cur, ats_only=True)
            elif ou_only:
                feature_cols = get_model_features(cur, ou_only=True)
            else:
                feature_cols = get_model_features(cur)
    finally:
        conn.close()

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)

    total_results = []
    pkl_filenames = []
    last_train_years = None
    last_test_year = None

    # Save the training run FIRST to get the training_id (generated by DB)
    training_id = save_training_run(
        sport="nfl",
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

        if df_train.empty:
            logger.warning("No training data for test_year=%d, skipping", test_year)
            continue

        available = [c for c in feature_cols if c in df_train.columns]
        # Remove columns that are entirely NaN (never computed)
        available = [c for c in available if df_train[c].notna().any()]
        df_train = df_train.dropna(subset=available)

        if df_train.empty:
            logger.warning("No training data after NaN filtering for test_year=%d, skipping", test_year)
            continue

        X_train = df_train[available].values
        y_train = df_train[target].values

        dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=available)

        model = xgb.train(params, dtrain, num_boost_round=n_estimators, verbose_eval=False)

        # Training metrics
        train_preds = model.predict(dtrain)
        train_rmse = float(np.sqrt(mean_squared_error(y_train, train_preds)))
        train_mae = float(mean_absolute_error(y_train, train_preds))

        importance = model.get_score(importance_type="gain")
        total_gain = sum(importance.values()) or 1.0
        fi_sorted = sorted(
            [{"feature": k, "importance": round(v / total_gain, 6)} for k, v in importance.items()],
            key=lambda x: -x["importance"],
        )

        # Test accuracy (ATS) – evaluate on test year data
        ats_total = 0
        ats_correct = 0
        ml_total = 0
        ml_correct = 0

        if not df_test.empty and len(df_test) > 0:
            # Match engine inference: use trained features, fill NaN with 0.0
            # (engine's _extract_feature_vector does this; dropping NaN rows would
            #  produce different accuracy than the engine's backtest on same model)
            test_features = list(available)
            for feat in test_features:
                if feat not in df_test.columns:
                    df_test[feat] = 0.0
                df_test[feat] = df_test[feat].fillna(0.0)

            X_test = df_test[test_features].values
            y_test = df_test[target].values
            dtest = xgb.DMatrix(X_test, feature_names=test_features)
            pred_margins = model.predict(dtest)
            rmse_val = float(np.sqrt(mean_squared_error(y_test, pred_margins)))
            mae_val = float(mean_absolute_error(y_test, pred_margins))

            ats_total = len(y_test)
            spread_vals = df_test[spread_col].values if spread_col in df_test.columns else np.zeros(len(df_test))
            pred_ats = ((pred_margins + spread_vals) > 0).astype(int)
            # Recompute home_ats_cover inline to match engine's margin_vs_spread logic
            # (same formula as data_loader: home_score - away_score + closing_spread > 0)
            actual_ats = ((y_test + spread_vals) > 0).astype(int) if spread_col in df_test.columns else (y_test > 0).astype(int)
            ats_correct = int((pred_ats == actual_ats).sum())

            # Moneyline: positive margin = home win (exclude ties)
            pred_ml = (pred_margins > 0).astype(int)
            actual_home_won = (y_test > 0).astype(int)
            tie_mask = (y_test != 0)
            ml_total = int(tie_mask.sum())
            ml_correct = int(((pred_ml == actual_home_won) & tie_mask).sum())

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
            "name": f"{test_year} NFL ATS",
            "test_year": test_year,
            "total_games": ats_total,
            "rmse": round(rmse_val, 4),
            "mae": round(mae_val, 4),
            "train_rmse": round(train_rmse, 4),
            "train_mae": round(train_mae, 4),
            "input_features": list(available),
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
    update_pkl_filename("nfl", training_id, all_pkl_names)

    # Also update results_json on the row (save_training_run was called with empty json)
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
        results = asyncio.run(run_all_years(train_from=2021))
        print("\n=== NFL ATS Backtest Results ===")
        for r in results:
            if "error" in r:
                print(f"  {r['year']}: ERROR — {r['error']}")
            else:
                print(f"  {r['year']}: rmse={r['rmse']:.4f} mae={r['mae']:.4f}  n={r['n_train']}+{r['n_test']}")

    elif mode == "train":
        result = asyncio.run(train_model(label="nfl_cli_training"))
        print("\n=== NFL Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    elif mode == "single":
        result = run_single()
        print("\n=== NFL Single Model ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            else:
                print(f"  {k}: {v}")

    elif mode == "predict":
        if len(sys.argv) < 4:
            print("Usage: python nfl_xgb_model_ats.py predict <game_id> <home_abbr> <away_abbr>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        home_abbr = sys.argv[3].upper()
        away_abbr = sys.argv[4].upper()
        result = predict_ats(game_id, home_abbr, away_abbr)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nfl_xgb_model_ats.py [backtest|train|single|predict]")
        sys.exit(1)
