"""
NBA XGBoost Over/Under model — regression predicting total points.

Mirrors ``nfl/nfl_xgb_model_ou.py`` but adapted for the NBA schema
and NBA data loader. Predicts total game points (home + away) rather
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
from psycopg2.extras import Json as PgJson
import xgboost as xgb
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

from app.handicapping.db_training import save_training_run, update_pkl_filename
from app.handicapping.nba.data_loader import (
    FEATURES_CATALOG,
    NBADataLoader,
    get_data_loader,
    get_model_features,
)

logger = logging.getLogger(__name__)

# ── Model paths ─────────────────────────────────────────────────────────────────
# PKL directory for nba models (matches MLB pattern: data/models/<sport>/)
NBA_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nba")
NBA_PKL_DIR.mkdir(parents=True, exist_ok=True)

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
    """Ensure all NBA OU feature columns exist in the DataFrame.

    Fills missing features with 0 (neutral for tree models) instead of NaN,
    so ``dropna()`` later does not erase every row.
    """
    ou_features = get_model_features(target="ou")
    for feat in ou_features:
        if feat not in df.columns:
            df[feat] = 0.0
        elif df[feat].isna().all():
            df[feat] = df[feat].fillna(0.0)
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
    feature_cols = get_model_features(target="ou")

    # Keep only features that exist in the DataFrame
    available = [c for c in feature_cols if c in train_feats.columns]
    if not available:
        return {"year": test_year, "error": "no feature columns available"}

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

    # Feature importance (normalized to 0-1)
    importance = model.get_score(importance_type="gain")
    total_gain = sum(importance.values()) or 1.0
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v / total_gain, 6)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    elapsed = time.time() - t0

    # Over/under accuracy: did model correctly predict over/under vs closing OU?
    # Compute with pushes (games where total == ou_line)
    ou_line_vals = test_feats["closing_ou"] if "closing_ou" in test_feats else test_feats.get("opening_ou", None)
    if ou_line_vals is not None:
        ou_line_vals = ou_line_vals.values if hasattr(ou_line_vals, "values") else np.full(len(y_pred), ou_line_vals)
        ou_total = len(y_pred)
        ou_correct = 0
        ou_push = 0
        for i in range(ou_total):
            pred_over = y_pred[i] > ou_line_vals[i]
            actual_over = y_test[i] > ou_line_vals[i]
            if abs(y_test[i] - ou_line_vals[i]) < 0.05:
                ou_push += 1
            elif pred_over == actual_over:
                ou_correct += 1
        ou_non_push = ou_total - ou_push
        ou_incorrect = ou_total - ou_correct - ou_push
        ou_pct = round(100 * ou_correct / max(ou_non_push, 1), 2)
    else:
        ou_total = len(y_pred)
        ou_correct = 0
        ou_push = 0
        ou_non_push = ou_total
        ou_incorrect = ou_total
        ou_pct = 0.0

    elapsed_sec = round(elapsed, 2)

    result: Dict[str, Any] = {
        "year": int(test_year),
        "test_year": int(test_year),
        "mae": round(float(mae), 4),
        "rmse": round(float(rmse), 4),
        "r2": round(float(r2), 4),
        "mean_actual": round(float(y_test.mean()), 2),
        "mean_predicted": round(float(y_pred.mean()), 2),
        "feature_importance": fi_sorted,
        "feature_set": available,
        "n_train": int(len(X_train)),
        "n_test": int(len(X_test)),
        "train_mae": round(float(train_mae), 4),
        "elapsed_seconds": elapsed_sec,
        "duration_seconds": elapsed_sec,
        "target": target,
        "n_features": int(len(available)),
        "input_features": int(len(available)),
        "total_games": ou_total,
        "ou": {
            "pct": ou_pct,
            "push": ou_push,
            "total": ou_total,
            "correct": ou_correct,
            "non_push": ou_non_push,
            "incorrect": ou_incorrect,
        },
        "ou_pct": ou_pct,
        "ou_total": ou_total,
        "ou_correct": ou_correct,
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
    }

    if return_model:
        result["model"] = model

    logger.info(
        "Year %d | ou=%.1f%% mae=%.2f rmse=%.2f r2=%.4f | train=%d test=%d %.1fs",
        int(test_year), ou_pct, mae, rmse, r2, len(X_train), len(X_test), elapsed,
    )

    return result


# ── Run all years ────────────────────────────────────────────────────────────────
async def run_all_years(
    train_from: int = 2021,
    limit: Optional[int] = None,
    hyperparams: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Backtest OU model on last 2 seasons, saving to ``nba.training_runs``."""
    dl = get_data_loader(ou_only=True)
    df = dl.load_data(limit=limit)

    if df.empty:
        logger.error("No data loaded")
        return []

    df["total_points"] = df["home_score"] + df["away_score"]
    df = _ensure_ou_features(df)
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

    unique_years = sorted(df["season_year"].unique())
    test_years = unique_years[-2:]
    logger.info("Test years (OU): %s (all unique years: %s)", test_years, unique_years)

    import math

    total_results: List[Dict[str, Any]] = []
    for test_year in test_years:
        result = run_backtest(df, test_year, hyperparams=hyperparams, return_model=True)
        if result.get("error"):
            logger.warning("Error for year %d: %s", test_year, result["error"])
            continue
        total_results.append(result)

    # Save training runs
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
                entry["name"] = f"{year} NBA OU"
                entry.pop("model", None)
                entry.pop("feature_set", None)

            last_result = results_list[-1]
            train_years = list(range(train_from, last_result["test_year"]))

            db_run_id = save_training_run(
                sport="nba",
                model_type="ou",
                test_year=last_result["test_year"],
                train_years=train_years,
                results_json=results_list,
                pkl_filename="",
            )
            logger.info("Saved training run %s: %d years", db_run_id, len(total_results))

            # Save PKL files
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
                # Update results_json with pkl_filename per year
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
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

    test_years = sorted(df["season_year"].unique())
    train_years = [y for y in test_years if y < CURRENT_YEAR]

    result = run_backtest(
        df, CURRENT_YEAR,
        train_years=train_years,
        hyperparams=hyperparams,
        return_model=True,
    )

    if "model" in result:
        path = model_path
        with open(path, "wb") as f:
            pickle.dump(result["model"], f)
        logger.info("Saved OU model to %s", path)

        test_year = CURRENT_YEAR
        train_seasons = list(range(2015, test_year))
        training_id = save_training_run(
            sport="nba",
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

    path = model_path
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
                FROM nba.games g
                LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
                JOIN nba.teams ht ON ht.id = g.home_team_id
                JOIN nba.teams at ON at.id = g.away_team_id
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
    label: str = "nba_ou_training",
) -> Dict[str, Any]:
    """Full OU training pipeline: trains one OU model per test year (2024, 2025),
    saves each model & its training run to the database."""
    t0 = time.time()

    model_type = "ou"
    model_dir = model_path if model_path else NBA_PKL_DIR
    model_dir.mkdir(parents=True, exist_ok=True)

    dl = get_data_loader(ou_only=True)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df["total_points"] = df["home_score"] + df["away_score"]
    df = _ensure_ou_features(df)
    df = df.sort_values(["season_year", "date"]).reset_index(drop=True)

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

    feature_cols = get_model_features(target="ou")

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)

    # Create the training run FIRST to get the training_id
    training_id = save_training_run(
        sport="nba",
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
        df_train = df_train.dropna(subset=available)

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
            "name": f"{test_year} NBA OU",
            "test_year": test_year,
            "total_games": ou_total,
            "mae": round(float(train_mae), 4),
            "r2": round(float(train_r2), 4),
            "input_features": len(available),
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
    update_pkl_filename("nba", training_id, all_pkl_names)

    # Update results_json via SQL
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
        print("\n=== NBA OU Backtest Results ===")
        for r in results:
            if "error" in r:
                print(f"  {r['year']}: ERROR — {r['error']}")
            else:
                print(f"  {r['year']}: ou={r['ou_pct']:.1f}% mae={r['mae']:.2f} rmse={r['rmse']:.2f} r2={r['r2']:.4f}  n={r['n_train']}+{r['n_test']}")

    elif mode == "train":
        result = asyncio.run(train_model(label="nba_ou_cli"))
        print("\n=== NBA OU Model Training ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            elif k == "results_json":
                print(f"  {k}: (json, {len(v)} chars)")
            else:
                print(f"  {k}: {v}")

    elif mode == "single":
        result = run_single()
        print("\n=== NBA OU Single Model ===")
        for k, v in result.items():
            if k == "feature_importance":
                print(f"  {k}: {len(v)} features")
            else:
                print(f"  {k}: {v}")

    elif mode == "predict":
        if len(sys.argv) < 3:
            print("Usage: python nba_xgb_model_ou.py predict <game_id>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        result = predict_ou(game_id)
        print(json.dumps(result, indent=2))

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python nba_xgb_model_ou.py [backtest|train|single|predict]")
        sys.exit(1)
