"""
NFL XGBoost Over/Under model — regression predicting total points.

Mirrors ``mlb/mlb_xgb_model_ou.py`` but adapted for the NFL schema
and NFL data loader. Predicts total game points (home + away) rather
than a binary over/under classification.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
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
from app.handicapping.nfl.data_loader import (
    FEATURES_CATALOG,
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
DEFAULT_N_ESTIMATORS = 300
DEFAULT_LEARNING_RATE = 0.03
DEFAULT_MAX_DEPTH = 5
DEFAULT_SUBSAMPLE = 0.8
DEFAULT_COL_SAMPLE = 0.8
DEFAULT_EARLY_STOPPING = 30

CURRENT_YEAR = datetime.now().year
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)

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


# ── Core training logic ─────────────────────────────────────────────────────────

def run_backtest(
    df: pd.DataFrame,
    test_year: int,
    train_years: Optional[List[int]] = None,
    hyperparams: Optional[Dict[str, Any]] = None,
    return_model: bool = False,
) -> Dict[str, Any]:
    """Train OU regression on ``train_years``, evaluate on ``test_year``.

    The target is actual total game points (``home_score + away_score``).
    The model predicts the total and is scored via MAE / RMSE / R².
    """
    t0 = time.time()

    if train_years is None:
        train_years = sorted(df["season_year"].unique())
        train_years = [y for y in train_years if y < test_year]

    train_feats = df[df["season_year"].isin(train_years)].copy()
    test_feats = df[df["season_year"] == test_year].copy()

    if train_feats.empty or test_feats.empty:
        logger.warning("Empty train (%d) or test (%d) for year %d", len(train_feats), len(test_feats), test_year)
        return {"year": test_year, "error": "insufficient data"}

    # Target: total game points
    target = "total_points"
    if target not in train_feats.columns:
        logger.warning("Computing target '%s' from scores", target)
        train_feats[target] = train_feats["home_score"] + train_feats["away_score"]
        test_feats[target] = test_feats["home_score"] + test_feats["away_score"]

    train_feats = train_feats.dropna(subset=[target])
    test_feats = test_feats.dropna(subset=[target])

    if train_feats.empty or test_feats.empty:
        return {"year": test_year, "error": "no labeled data"}

    # Feature columns (OU-only from DB)
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            feature_cols = get_model_features(cur, ou_only=True)
    finally:
        conn.close()

    # Keep only features that exist in the DataFrame
    available = [c for c in feature_cols if c in train_feats.columns]
    if not available:
        return {"year": test_year, "error": "no feature columns available"}
    # Remove columns that are entirely NaN (never computed by build_features)
    available = [c for c in available if train_feats[c].notna().any()]

    train_feats = train_feats.dropna(subset=available)
    test_feats = test_feats.dropna(subset=available)

    if train_feats.empty or test_feats.empty:
        return {"year": test_year, "error": "all rows dropped by NaN"}

    X_train = train_feats[available].values
    y_train = train_feats[target].values
    X_test = test_feats[available].values
    y_test = test_feats[target].values

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

    dtrain = xgb.DMatrix(X_train, label=y_train, feature_names=available)
    dtest = xgb.DMatrix(X_test, label=y_test, feature_names=available)

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)
    early_stop = hp.get("early_stopping_rounds", DEFAULT_EARLY_STOPPING)

    model = xgb.train(
        params, dtrain,
        num_boost_round=n_estimators,
        evals=[(dtest, "test")],
        early_stopping_rounds=early_stop,
        verbose_eval=False,
    )

    # Predict
    y_pred = model.predict(dtest)

    # Regression metrics
    try:
        mae = mean_absolute_error(y_test, y_pred)
        mse = mean_squared_error(y_test, y_pred)
        rmse = np.sqrt(mse)
        r2 = r2_score(y_test, y_pred)
    except Exception:
        mae = mse = rmse = r2 = 0.0

    y_train_pred = model.predict(dtrain)
    train_mae = mean_absolute_error(y_train, y_train_pred)

    # Feature importance
    importance = model.get_score(importance_type="gain")
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v, 4)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    elapsed = time.time() - t0

    # Over/under accuracy: did model correctly predict over/under vs closing OU?
    # Use test_data (full DataFrame) — test_feats is only [features + target], lacks closing_ou
    ou_line = test_data.get("closing_ou", test_data.get("opening_ou", None))
    ou_acc = None
    if ou_line is not None:
        over_correct = ((y_pred > ou_line) == (y_test > ou_line)).mean()
        ou_acc = round(float(over_correct), 4)

    result: Dict[str, Any] = {
        "year": test_year,
        "mae": round(float(mae), 4),
        "rmse": round(float(rmse), 4),
        "r2": round(float(r2), 4),
        "mean_actual": round(float(y_test.mean()), 2),
        "mean_predicted": round(float(y_pred.mean()), 2),
        "ou_accuracy": ou_acc,
        "feature_importance": fi_sorted,
        "feature_set": available,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "train_mae": round(float(train_mae), 4),
        "elapsed_seconds": round(elapsed, 2),
        "target": target,
        "n_features": len(available),
    }

    if return_model:
        result["model"] = model

    logger.info(
        "Year %d | MAE=%.2f RMSE=%.2f R²=%.4f OU_acc=%s | train=%d test=%d %.1fs",
        test_year, mae, rmse, r2, ou_acc, len(X_train), len(X_test), elapsed,
    )

    return result


# ── Run all years ────────────────────────────────────────────────────────────────
async def run_all_years(
    train_from: int = 2021,
    limit: Optional[int] = None,
    hyperparams: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Backtest OU model every year from ``train_from`` to current."""
    dl = get_data_loader(ou_only=True)
    df = dl.load_data(limit=limit)

    if df.empty:
        logger.error("No data loaded")
        return []

    df["total_points"] = df["home_score"] + df["away_score"]
    df = _ensure_ou_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    test_years = sorted(df["season_year"].unique())
    logger.info("Test years (OU): %s", test_years)

    results: List[Dict[str, Any]] = []
    for test_year in test_years:
        if test_year == min(test_years):
            logger.info("Skipping first year %d (no train data before it)", test_year)
            continue

        result = run_backtest(df, test_year, hyperparams=hyperparams, return_model=False)
        results.append(result)

    return results


# ── Run single (train all, save model) ───────────────────────────────────────────
def run_single(
    hyperparams: Optional[Dict[str, Any]] = None,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Train OU model on all available data, save, return metrics."""
    dl = get_data_loader(ou_only=True)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df["total_points"] = df["home_score"] + df["away_score"]
    df = _ensure_ou_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    test_years = sorted(df["season_year"].unique())
    train_years = [y for y in test_years if y < CURRENT_YEAR]

    result = run_backtest(
        df, CURRENT_YEAR,
        train_years=train_years,
        hyperparams=hyperparams,
        return_model=True,
    )

    if "model" in result:
        path = model_path or OU_MODEL_PATH
        with open(path, "wb") as f:
            pickle.dump(result["model"], f)
        logger.info("Saved OU model to %s", path)

        test_year = CURRENT_YEAR
        train_seasons = list(range(2015, test_year))
        training_id = save_training_run(
            sport="nfl",
            model_type="ou",
            test_year=test_year,
            train_years=train_seasons,
            results_json={
                "n_train": result.get("n_train", 0),
                "mae": result.get("mae", 0),
                "r2": result.get("r2", 0),
            },
            pkl_filename="",
        )
        result["training_id"] = training_id

        del result["model"]

    return result


# ── Predict total points ────────────────────────────────────────────────────────
def predict_ou(
    game_id: int,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Predict total points for a game using the trained OU model.

    Parameters
    ----------
    game_id : Game primary key.
    model_path : Custom model path.

    Returns dict with predicted_total, confidence, spread, ou, etc.
    """
    global _MODEL

    path = model_path or OU_MODEL_PATH
    if _MODEL is None or model_path:
        if path.exists():
            with open(path, "rb") as f:
                _MODEL = pickle.load(f)
            logger.info("Loaded OU model from %s", path)
        else:
            return {"error": f"no model at {path}"}

    dl = get_data_loader(ou_only=True)
    df = dl.load_inference_data(game_ids=[game_id])

    if df.empty:
        return {"error": "no features for game"}

    df = _ensure_ou_features(df)

    # Feature columns that the model was trained on
    feature_cols = [c for c in df.columns if c in _MODEL.feature_names]

    if not feature_cols:
        return {"error": "no matching feature columns"}

    features = df[feature_cols].values
    dmat = xgb.DMatrix(features, feature_names=feature_cols)
    pred_total = float(_MODEL.predict(dmat)[0])

    # Compute confidence based on proximity to closing OU line
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT blc.closing_ou, blc.closing_spread,
                       ht.abbreviation, at.abbreviation
                FROM nfl.games g
                LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
                JOIN nfl.teams ht ON ht.id = g.home_team_id
                JOIN nfl.teams at ON at.id = g.away_team_id
                WHERE g.id = %s
                """,
                (game_id,),
            )
            row = cur.fetchone()
            ou_line, spread, home_abbr, away_abbr = row or (None, None, None, None)
    finally:
        conn.close()

    # Confidence: difference between predicted and line
    if ou_line:
        diff = abs(pred_total - float(ou_line))
        confidence = round(min(diff / 10.0, 1.0), 4)
        direction = "over" if pred_total > float(ou_line) else "under"
    else:
        confidence = 0.5
        direction = "unknown"

    return {
        "game_id": game_id,
        "predicted_total": round(pred_total, 2),
        "confidence": confidence,
        "direction": direction,
        "closing_ou": float(ou_line) if ou_line else None,
        "closing_spread": float(spread) if spread else None,
        "home_abbr": home_abbr,
        "away_abbr": away_abbr,
        "model_path": str(path),
    }


# ── Predict OU for a game (convenience wrapper) ──────────────────────────────────
def predict_ou_game(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
    spread: float,
    ou_line: float,
    model_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Convenience wrapper around ``predict_ou()``."""
    result = predict_ou(game_id, model_path=model_path)
    if "error" in result:
        return result
    return {
        **result,
        "home_abbr": home_abbr,
        "away_abbr": away_abbr,
        "spread": spread,
        "ou_line": ou_line,
    }


# ── Train model (async full pipeline) ────────────────────────────────────────────
TEST_YEARS = [2024, 2025]


def _train_years_for_test_year(test_year: int) -> List[int]:
    """Return the training years for the given test year.

    2024: trains on 2021, 2022, 2023
    2025: trains on 2021, 2022, 2023, 2024
    """
    return list(range(2021, test_year))


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
        "eval_metric": "mae",
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

        if df_train.empty:
            logger.warning("No training data for test_year=%d, skipping", test_year)
            continue

        available = [c for c in feature_cols if c in df_train.columns]
        # Remove columns that are entirely NaN (never computed by build_features)
        available = [c for c in available if df_train[c].notna().any()]
        df_train = df_train.dropna(subset=available)

        if df_train.empty:
            logger.warning("No training data after NaN filtering for test_year=%d, skipping", test_year)
            continue

        X = df_train[available].values
        y = df_train["total_points"].values

        dtrain = xgb.DMatrix(X, label=y, feature_names=available)

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
                        actual_over = y_test[i] > closing_ou_values[i]
                        pred_over = pred_totals[i] > closing_ou_values[i]
                        if abs(y_test[i] - closing_ou_values[i]) < 0.05:
                            ou_push += 1
                        elif pred_over == actual_over:
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

    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        results = asyncio.run(run_all_years(train_from=2021))
        print("\n=== NFL OU Backtest Results ===")
        for r in results:
            if "error" in r:
                print(f"  {r['year']}: ERROR — {r['error']}")
            else:
                print(f"  {r['year']}: MAE={r['mae']:.2f} RMSE={r['rmse']:.2f} R²={r['r2']:.4f} OU_acc={r['ou_accuracy']}  n={r['n_train']}+{r['n_test']}")

    elif mode == "train":
        result = asyncio.run(train_model(label="nfl_ou_cli"))
        print("\n=== NFL OU Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    elif mode == "single":
        result = run_single()
        print("\n=== NFL OU Single Model ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            else:
                print(f"  {k}: {v}")

    elif mode == "predict":
        if len(sys.argv) < 3:
            print("Usage: python nfl_xgb_model_ou.py predict <game_id>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        result = predict_ou(game_id)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nfl_xgb_model_ou.py [backtest|train|single|predict]")
        sys.exit(1)
