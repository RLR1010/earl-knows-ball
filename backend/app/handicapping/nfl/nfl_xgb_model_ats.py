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
from sklearn.metrics import (
    accuracy_score,
    log_loss,
    precision_score,
    recall_score,
    roc_auc_score,
)

from app.handicapping.nfl.data_loader import (
    FEATURES_CATALOG,
    NFLDataLoader,
    get_data_loader,
    get_model_features,
)

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
NFL_PKL_DIR = Path(__file__).parent / "models" / "xgboost"
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
            df[feat] = float("nan")

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

    Returns dict with: year, accuracy, auc, log_loss, precision, recall,
    feature_importance, n_train, n_test, train_accuracy, model (optional).
    """
    t0 = time.time()

    train_df = df[df["season_year"] < test_year].copy()
    test_df = df[df["season_year"] == test_year].copy()

    if train_df.empty or test_df.empty:
        logger.warning("Empty train (%d) or test (%d) for year %d", len(train_df), len(test_df), test_year)
        return {"year": test_year, "error": "insufficient data"}

    target = "over_result" if ou_only else "home_ats_cover"
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

    train_df = train_df.dropna(subset=feature_cols)
    test_df = test_df.dropna(subset=feature_cols)

    X_train, y_train = train_df[feature_cols].values, train_df[target].values
    X_test, y_test = test_df[feature_cols].values, test_df[target].values

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
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

    y_pred_proba = model.predict(dtest)
    y_pred = (y_pred_proba >= 0.5).astype(int)

    try:
        acc = accuracy_score(y_test, y_pred)
        auc = roc_auc_score(y_test, y_pred_proba)
        ll = log_loss(y_test, y_pred_proba)
        prec = precision_score(y_test, y_pred, zero_division=0)
        rec = recall_score(y_test, y_pred, zero_division=0)
    except Exception:
        acc = auc = ll = prec = rec = 0.0

    y_train_pred = (model.predict(dtrain) >= 0.5).astype(int)
    train_acc = accuracy_score(y_train, y_train_pred)

    importance = model.get_score(importance_type="gain")
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v, 4)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    result: Dict[str, Any] = {
        "year": test_year,
        "accuracy": round(float(acc), 4),
        "auc": round(float(auc), 4),
        "log_loss": round(float(ll), 4),
        "precision": round(float(prec), 4),
        "recall": round(float(rec), 4),
        "feature_importance": fi_sorted,
        "n_train": len(X_train),
        "n_test": len(X_test),
        "train_accuracy": round(float(train_acc), 4),
        "elapsed_seconds": round(time.time() - t0, 2),
        "target": target,
    }

    if return_model:
        result["model"] = model

    logger.info(
        "Year %d | acc=%.4f auc=%.4f log_loss=%.4f | train=%d test=%d %.1fs",
        test_year, acc, auc, ll, len(X_train), len(X_test), result["elapsed_seconds"],
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
async def train_model(
    seasons: Optional[List[int]] = None,
    model_path: Optional[Path] = None,
    ats_only: bool = True,
    ou_only: bool = False,
    hyperparams: Optional[Dict[str, Any]] = None,
    label: str = "nfl_ats_training",
) -> Dict[str, Any]:
    """Full training pipeline: load data, train XGBoost, save model,
    save training run metadata to the database.

    Parameters
    ----------
    seasons : List of season_id values for training.
    model_path : Output path (default NFL_PKL_DIR / label.pkl).
    ats_only : Train ATS vs OU.
    ou_only : Train OU model.
    hyperparams : XGBoost param overrides.
    label : Label for the training run.

    Returns dict with run_id, metrics, feature_importance.
    """
    t0 = time.time()

    dl = get_data_loader(ats_only=ats_only, ou_only=ou_only)
    df = dl.load_data()

    if df.empty:
        return {"error": "no data loaded"}

    df = _ensure_ats_features(df)
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    target = "over_result" if ou_only else "home_ats_cover"
    df_train = df.dropna(subset=[target]).copy()

    hp = hyperparams or {}
    params: Dict[str, Any] = {
        "objective": "binary:logistic",
        "eval_metric": "logloss",
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

    feature_cols = [c for c in feature_cols if c in df_train.columns]
    df_train = df_train.dropna(subset=feature_cols)

    X = df_train[feature_cols].values
    y = df_train[target].values

    dtrain = xgb.DMatrix(X, label=y, feature_names=feature_cols)

    n_estimators = hp.get("n_estimators", DEFAULT_N_ESTIMATORS)
    model = xgb.train(params, dtrain, num_boost_round=n_estimators, verbose_eval=False)

    y_pred = (model.predict(dtrain) >= 0.5).astype(int)
    train_acc = accuracy_score(y, y_pred)

    importance = model.get_score(importance_type="gain")
    fi_sorted = sorted(
        [{"feature": k, "importance": round(v, 4)} for k, v in importance.items()],
        key=lambda x: -x["importance"],
    )

    path = model_path or (OU_MODEL_PATH if ou_only else ATS_MODEL_PATH)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    logger.info("Model saved to %s", path)

    results_json = json.dumps({
        "feature_importance": fi_sorted,
        "params": params,
        "n_train": len(X),
        "train_accuracy": round(float(train_acc), 4),
    })

    elapsed = time.time() - t0

    return {
        "label": label,
        "train_accuracy": round(float(train_acc), 4),
        "n_train": len(X),
        "n_features": len(feature_cols),
        "feature_importance": fi_sorted,
        "elapsed_seconds": round(elapsed, 2),
        "model_path": str(path),
        "results_json": results_json,
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
                print(f"  {r['year']}: acc={r['accuracy']:.4f} auc={r['auc']:.4f} ll={r['log_loss']:.4f}  n={r['n_train']}+{r['n_test']}")

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
