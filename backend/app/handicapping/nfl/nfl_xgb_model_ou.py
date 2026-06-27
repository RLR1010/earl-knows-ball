"""
NFL OU XGBoost Model
====================
Structurally mirrors mlb_xgb_model_ou.py.

Trains XGBoost regressors to predict the total points (over/under) for NFL
games. Supports backtesting across historical seasons, full multi-year
training runs, single-year training + saving, and DB-persisted predictions.
"""

import os
import sys
import json
import math
import asyncio
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split

from app.handicapping.nfl.data_loader import (
    NFLDataLoader,
    build_features,
    ATS_FEATURES,
    OU_FEATURES,
    DEFAULT_TRAIN_FROM,
    DEFAULT_DB_URL,
    CURRENT_YEAR,
)
from app.handicapping.db_training import (
    save_training_run,
    update_pkl_filename,
)

# ════════════════════════════════════════════════════════════════════════
# Configuration
# ════════════════════════════════════════════════════════════════════════

SAVED_MODEL_DIR = Path("app/models/nfl")
SAVED_MODEL_DIR.mkdir(parents=True, exist_ok=True)

# Sport config — mirrors MLB OU pattern
SCHEMA = "nfl"
SPORT_NAME = "nfl"
TRAIN_FROM = int(os.getenv("NFL_OU_TRAIN_FROM", str(DEFAULT_TRAIN_FROM)))
FUNCTION_TYPE = "ou"
MODEL_TYPE = "regressor"  # XGBRegressor for OU

# XGBoost params (tuned for NFL OU regression)
XGB_PARAMS = {
    "n_estimators": 500,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 3,
    "gamma": 0.1,
    "reg_alpha": 0.1,
    "reg_lambda": 1.0,
    "random_state": 42,
    "eval_metric": "mae",
    "verbosity": 1,
    "objective": "reg:absoluteerror",
}


def log(msg: str):
    """Simple console logger."""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{SPORT_NAME.upper()} OU] {msg}")


# ════════════════════════════════════════════════════════════════════════
# Core Functions
# ════════════════════════════════════════════════════════════════════════

def run_backtest(train_from: int, test_year: int,
                 skip_db: bool = False,
                 db_url: str = None,
                 engine=None,
                 log_fn: Optional[callable] = None) -> dict:
    """Run a single year backtest: train on train_from..test_year-1, test on test_year.

    Uses XGBRegressor to predict total points (over/under).

    Parameters
    ----------
    train_from : int
        Earliest season year for training data.
    test_year : int
        The season year to backtest against.
    skip_db : bool
        If True, skip saving predictions to DB.
    db_url : str, optional
        Database URL override.
    engine : sqlalchemy Engine, optional
        Pre-existing DB engine.
    log_fn : callable, optional
        Logging callback (defaults to built-in log).

    Returns
    -------
    dict
        Results dictionary with keys: test_year, train_years, total_games, test_games,
        feature_set, feature_importance, mae, roi, ats.
    """
    _log = log_fn or log
    _log(f"run_backtest: train_from={train_from}, test_year={test_year}")

    # ── 1. Load data ────────────────────────────────────────────────
    loader = NFLDataLoader(db_url=db_url)
    raw = asyncio.run(loader.load_games(min_year=train_from, max_year=test_year,
                                         engine=engine, log_fn=_log))
    df = build_features(raw, log_fn=_log)

    if df.empty:
        _log("No data loaded", "ERROR")
        return {"test_year": test_year, "error": "no_data"}

    # ── 2. Split into train / test ──────────────────────────────────
    train_df = df[df["season_year"] < test_year].copy()
    test_df = df[df["season_year"] == test_year].copy()

    _log(f"Train: {len(train_df)} games ({train_from}–{test_year-1}), "
         f"Test: {len(test_df)} games ({test_year})")

    if len(test_df) == 0:
        _log(f"No test data for {test_year}", "WARN")
        return {"test_year": test_year, "error": "no_test_data"}

    # ── 3. Identify available features ──────────────────────────────
    available = [c for c in OU_FEATURES if c in df.columns]
    _log(f"Using {len(available)} OU features")

    # ── 4. Prepare feature matrix / label ───────────────────────────
    X_train = train_df[available].values.astype(np.float32)
    y_train = train_df["total"].values.astype(np.float32)
    X_test = test_df[available].values.astype(np.float32)
    y_test = test_df["total"].values.astype(np.float32)

    # ── 5. Scale features ───────────────────────────────────────────
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    # ── 6. Train model ──────────────────────────────────────────────
    model = xgb.XGBRegressor(**XGB_PARAMS)
    model.fit(
        X_train_scaled, y_train,
        eval_set=[(X_train_scaled, y_train), (X_test_scaled, y_test)],
        verbose=False,
    )

    # ── 7. Predict & evaluate ──────────────────────────────────────
    y_pred = model.predict(X_test_scaled)

    mae = float(np.mean(np.abs(y_pred - y_test)))
    rmse = float(np.sqrt(np.mean((y_pred - y_test) ** 2)))
    _log(f"MAE: {mae:.2f}, RMSE: {rmse:.2f}")

    # ── 8. Over/Under betting ROI ──────────────────────────────────
    # Bet $1 on over if predicted > closing total, under if predicted < closing total
    wins = 0
    losses = 0
    pushes = 0
    results = []

    for i in range(len(y_test)):
        pred_total = y_pred[i]
        actual_total = y_test[i]
        closing_total = float(test_df.iloc[i].get("over_under", 0)) or 42.5

        # Determine over/under bet
        if pred_total > closing_total:
            bet_over = 1  # bet over
        elif pred_total < closing_total:
            bet_over = 0  # bet under
        else:
            bet_over = -1  # push / no bet

        actual_over = 1 if actual_total > closing_total else (0 if actual_total < closing_total else -1)

        if bet_over == -1 or actual_over == -1:
            pushes += 1
        elif bet_over == actual_over:
            wins += 1
        else:
            losses += 1

        results.append({
            "game_id": int(test_df.iloc[i]["game_id"]) if "game_id" in test_df.columns else i,
            "home": str(test_df.iloc[i].get("home_abbr", "")),
            "away": str(test_df.iloc[i].get("away_abbr", "")),
            "predicted_total": round(float(pred_total), 1),
            "actual_total": round(float(actual_total), 1),
            "closing_total": round(float(closing_total), 1),
            "error": round(float(abs(pred_total - actual_total)), 1),
            "home_score": int(test_df.iloc[i].get("home_score", 0)),
            "away_score": int(test_df.iloc[i].get("away_score", 0)),
        })

    total_bets = wins + losses + pushes
    roi = ((wins - losses) * 1.0 / max(total_bets, 1)) * 100 if total_bets > 0 else 0.0

    _log(f"W/L/P: {wins}/{losses}/{pushes}, ROI: {roi:.2f}%")

    # ── 9. Feature importance ──────────────────────────────────────
    importance = model.feature_importances_
    feat_imp = {name: float(imp) for name, imp in zip(available, importance)}

    # ── 10. Build result dict ────────────────────────────────────────
    result = {
        "test_year": test_year,
        "train_years": f"{train_from}–{test_year-1}",
        "total_games": len(df),
        "test_games": len(test_df),
        "feature_set": available,
        "feature_importance": feat_imp,
        "mae": round(mae, 2),
        "rmse": round(rmse, 2),
        "roi": round(roi, 2),
        "ats": {
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
        },
        "predictions": results,
    }

    # ── 11. Save predictions to DB ──────────────────────────────────
    if not skip_db and len(results) > 0:
        try:
            _save_backtest_prediction(results, test_year, engine=engine, db_url=db_url)
            _log(f"Saved {len(results)} predictions to DB")
        except Exception as e:
            _log(f"Failed to save predictions: {e}\n{traceback.format_exc()}", "WARN")

    return result


def run_all_years(train_from: int = None,
                  skip_db: bool = False,
                  db_url: str = None,
                  engine=None,
                  log_fn: Optional[callable] = None) -> dict:
    """Run backtests for all available test years and save combined model.

    Process years from newest to oldest (with train_from as the lower bound),
    compile results, train a final overall model, and persist to DB.

    Parameters
    ----------
    train_from : int, optional
        Earliest training year (default TRAIN_FROM).
    skip_db : bool
        If True, skip DB persistence.
    db_url : str, optional
        Database URL override.
    engine : sqlalchemy Engine, optional
        Pre-existing DB engine.
    log_fn : callable, optional
        Logging callback.

    Returns
    -------
    dict
        Combined results with all per-year results and overall summary.
    """
    _log = log_fn or log
    train_from = train_from or TRAIN_FROM
    _log(f"run_all_years: train_from={train_from}")

    # ── 1. Load full data ───────────────────────────────────────────
    loader = NFLDataLoader(db_url=db_url)
    raw = asyncio.run(loader.load_games(min_year=train_from, max_year=CURRENT_YEAR,
                                         engine=engine, log_fn=_log))
    df = build_features(raw, log_fn=_log)

    if df.empty:
        _log("No data loaded", "ERROR")
        return {"error": "no_data"}

    # ── 2. Determine available test years ───────────────────────────
    all_years = sorted(df["season_year"].unique())
    test_years = [y for y in all_years if y > train_from]
    _log(f"Available test years: {test_years}")

    if not test_years:
        _log("No test years available", "ERROR")
        return {"error": "no_test_years"}

    # ── 3. Run per-year backtests (newest first) ────────────────────
    combined_results = []
    for ty in reversed(test_years):
        result = run_backtest(train_from, ty, skip_db=skip_db,
                              db_url=db_url, engine=engine, log_fn=_log)
        if "error" not in result:
            combined_results.append(result)
        else:
            _log(f"Skipping {ty}: {result.get('error')}", "WARN")

    if not combined_results:
        _log("No backtest results", "ERROR")
        return {"error": "no_results"}

    # ── 4. Overall summary ──────────────────────────────────────────
    total_wins = sum(r["ats"]["wins"] for r in combined_results)
    total_losses = sum(r["ats"]["losses"] for r in combined_results)
    total_pushes = sum(r["ats"]["pushes"] for r in combined_results)
    total_test = total_wins + total_losses + total_pushes
    avg_mae = float(np.mean([r["mae"] for r in combined_results if "mae" in r]))
    overall_roi = ((total_wins - total_losses) * 1.0 / max(total_test, 1)) * 100

    # ── 5. Build final feature set ──────────────────────────────────
    final_feature_set = sorted(set(
        f for r in combined_results for f in r.get("feature_set", [])
    ))

    # ── 6. Feature importance (average across models) ───────────────
    avg_importance: dict[str, float] = {}
    count = 0
    for r in combined_results:
        fi = r.get("feature_importance", {})
        if fi:
            count += 1
            for k, v in fi.items():
                avg_importance[k] = avg_importance.get(k, 0.0) + v
    if count > 0:
        for k in avg_importance:
            avg_importance[k] = round(avg_importance[k] / count, 6)

    # ── 7. Train final overall model ────────────────────────────────
    available = [c for c in final_feature_set if c in df.columns]
    if len(available) > 0:
        X_all = df[available].values.astype(np.float32)
        y_all = df["total"].values.astype(np.float32)

        scaler = StandardScaler()
        X_all_scaled = scaler.fit_transform(X_all)

        final_model = xgb.XGBRegressor(**XGB_PARAMS)
        final_model.fit(X_all_scaled, y_all, verbose=False)

        # ── 8. Save model PKL ───────────────────────────────────────
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_filename = f"nfl_ou_{timestamp}.pkl"
        model_path = SAVED_MODEL_DIR / model_filename
        import joblib
        joblib.dump({"model": final_model, "scaler": scaler, "features": available, "params": XGB_PARAMS},
                     model_path)
        _log(f"Saved model to {model_path}")

        latest_path = SAVED_MODEL_DIR / "nfl_ou_latest.pkl"
        joblib.dump({"model": final_model, "scaler": scaler, "features": available, "params": XGB_PARAMS},
                     latest_path)
        _log(f"Updated latest model symlink at {latest_path}")
    else:
        _log("No features available for final model", "WARN")
        model_filename = None
        model_path = None

    # ── 9. Compile results_json ─────────────────────────────────────
    results_json = {
        "sport": SPORT_NAME,
        "function_type": FUNCTION_TYPE,
        "model_type": MODEL_TYPE,
        "train_from": train_from,
        "total_games": int(len(df)),
        "avg_mae": round(avg_mae, 2),
        "overall_roi": round(overall_roi, 2),
        "overall_wins": total_wins,
        "overall_losses": total_losses,
        "overall_pushes": total_pushes,
        "feature_set": final_feature_set,
        "feature_importance": avg_importance,
        "params_used": XGB_PARAMS,
        "year_results": [
            {
                "test_year": r["test_year"],
                "train_years": r.get("train_years", ""),
                "total_games": r.get("total_games", 0),
                "test_games": r.get("test_games", 0),
                "feature_set": r.get("feature_set", []),
                "feature_importance": r.get("feature_importance", {}),
                "mae": r.get("mae", 0),
                "roi": r.get("roi", 0),
                "ou": r.get("ou", {"wins": 0, "losses": 0, "pushes": 0}),
            }
            for r in combined_results
        ],
        "model_filename": str(model_filename) if model_filename else None,
    }

    # ── 10. Save to DB ──────────────────────────────────────────────
    if not skip_db:
        try:
            run_id = save_training_run(
                sport=SPORT_NAME,
                model_type=MODEL_TYPE,
                results_json=results_json,
                pkl_filename=str(model_filename) if model_filename else "",
                algorithm="xgboost",
                description=f"OU backtest from {train_from} to {all_years[-1]}",
                test_year=combined_results[-1]["test_year"],
                train_years=combined_results[-1].get("train_years", []),
            )
            _log(f"Saved training_run id={run_id}")

            if model_filename:
                update_pkl_filename(sport=SPORT_NAME, training_id=run_id, pkl_filename=str(model_filename))
                _log(f"Updated pkl_filename to {model_filename}")
        except Exception as e:
            _log(f"DB save failed: {e}\n{traceback.format_exc()}", "WARN")
    else:
        _log("Skipping DB save (skip_db=True)")

    return results_json


def run_single(train_from: int, test_year: int,
               skip_db: bool = False,
               db_url: str = None,
               engine=None,
               log_fn: Optional[callable] = None) -> dict:
    """Train on train_from through test_year-1, test on test_year, save model.

    Used for training a single-year OU model for production use.

    Parameters
    ----------
    train_from : int
        Earliest training season year.
    test_year : int
        Target season year (test data).
    skip_db : bool
        If True, skip DB persistence.
    db_url : str, optional
        Database URL override.
    engine : sqlalchemy Engine, optional
        Pre-existing DB engine.
    log_fn : callable, optional
        Logging callback.

    Returns
    -------
    dict
        Results dictionary with training results and model metadata.
    """
    _log = log_fn or log
    _log(f"run_single: train_from={train_from}, test_year={test_year}")

    # ── 1. Load data ────────────────────────────────────────────────
    loader = NFLDataLoader(db_url=db_url)
    raw = asyncio.run(loader.load_games(min_year=train_from, max_year=test_year,
                                         engine=engine, log_fn=_log))
    df = build_features(raw, log_fn=_log)

    if df.empty:
        _log("No data loaded", "ERROR")
        return {"error": "no_data"}

    # ── 2. Split ────────────────────────────────────────────────────
    train_df = df[df["season_year"] < test_year].copy()
    test_df = df[df["season_year"] == test_year].copy()
    _log(f"Train: {len(train_df)} games, Test: {len(test_df)} games")

    if len(test_df) == 0:
        _log(f"No test data for {test_year}", "WARN")
        return {"error": "no_test_data"}

    # ── 3. Features ─────────────────────────────────────────────────
    available = [c for c in OU_FEATURES if c in df.columns]
    _log(f"Using {len(available)} features")

    X_train = train_df[available].values.astype(np.float32)
    y_train = train_df["total"].values.astype(np.float32)
    X_test = test_df[available].values.astype(np.float32)
    y_test = test_df["total"].values.astype(np.float32)

    # ── 4. Scale & Train ────────────────────────────────────────────
    scaler = StandardScaler()
    X_train_scaled = scaler.fit_transform(X_train)
    X_test_scaled = scaler.transform(X_test)

    model = xgb.XGBRegressor(**XGB_PARAMS)
    model.fit(
        X_train_scaled, y_train,
        eval_set=[(X_train_scaled, y_train), (X_test_scaled, y_test)],
        verbose=False,
    )

    # ── 5. Predict & evaluate ──────────────────────────────────────
    y_pred = model.predict(X_test_scaled)

    mae = float(np.mean(np.abs(y_pred - y_test)))
    rmse = float(np.sqrt(np.mean((y_pred - y_test) ** 2)))
    _log(f"MAE: {mae:.2f}, RMSE: {rmse:.2f}")

    # ── 6. Over/Under betting ROI ──────────────────────────────────
    wins = 0
    losses = 0
    pushes = 0
    results_list = []

    for i in range(len(y_test)):
        pred_total = y_pred[i]
        actual_total = y_test[i]
        closing_total = float(test_df.iloc[i].get("over_under", 0)) or 42.5

        bet_over = 1 if pred_total > closing_total else (0 if pred_total < closing_total else -1)
        actual_over = 1 if actual_total > closing_total else (0 if actual_total < closing_total else -1)

        if bet_over == -1 or actual_over == -1:
            pushes += 1
        elif bet_over == actual_over:
            wins += 1
        else:
            losses += 1

        results_list.append({
            "game_id": int(test_df.iloc[i]["game_id"]) if "game_id" in test_df.columns else i,
            "home": str(test_df.iloc[i].get("home_abbr", "")),
            "away": str(test_df.iloc[i].get("away_abbr", "")),
            "predicted_total": round(float(pred_total), 1),
            "actual_total": round(float(actual_total), 1),
            "closing_total": round(float(closing_total), 1),
            "home_score": int(test_df.iloc[i].get("home_score", 0)),
            "away_score": int(test_df.iloc[i].get("away_score", 0)),
        })

    roi = ((wins - losses) * 1.0 / max((wins + losses + pushes), 1)) * 100

    # ── 7. Feature importance ──────────────────────────────────────
    importance = model.feature_importances_
    feat_imp = {name: float(imp) for name, imp in zip(available, importance)}

    # ── 8. Save model ───────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    model_filename = f"nfl_ou_single_{test_year}_{timestamp}.pkl"
    model_path = SAVED_MODEL_DIR / model_filename

    import joblib
    joblib.dump({"model": model, "scaler": scaler, "features": available, "params": XGB_PARAMS,
                  "train_from": train_from, "test_year": test_year},
                 model_path)
    _log(f"Saved model to {model_path}")

    latest_path = SAVED_MODEL_DIR / "nfl_ou_latest.pkl"
    joblib.dump({"model": model, "scaler": scaler, "features": available, "params": XGB_PARAMS},
                 latest_path)
    _log("Updated latest model")

    # ── 9. Build results object ─────────────────────────────────────
    results_json = {
        "sport": SPORT_NAME,
        "function_type": FUNCTION_TYPE,
        "model_type": MODEL_TYPE,
        "train_from": train_from,
        "test_year": test_year,
        "total_games": len(df),
        "test_games": len(test_df),
        "feature_set": available,
        "feature_importance": feat_imp,
        "mae": round(mae, 2),
        "rmse": round(rmse, 2),
        "roi": round(roi, 2),
        "ou": {"wins": wins, "losses": losses, "pushes": pushes},
        "params_used": XGB_PARAMS,
        "model_filename": model_filename,
        "predictions": results_list,
    }

    # ── 10. DB save ─────────────────────────────────────────────────
    if not skip_db:
        try:
            run_id = save_training_run(
                sport=SPORT_NAME,
                model_type=MODEL_TYPE,
                results_json=results_json,
                pkl_filename=str(model_filename) if model_filename else "",
                algorithm="xgboost",
                description=f"OU single run test_year={test_year}",
                test_year=test_year,
                train_years=train_years,
            )
            _log(f"Saved training_run id={run_id}")
            update_pkl_filename(sport=SPORT_NAME, training_id=run_id, pkl_filename=str(model_filename) if model_filename else "")
            _log(f"Updated pkl_filename to {model_filename}")
        except Exception as e:
            _log(f"DB save failed: {e}\n{traceback.format_exc()}", "WARN")

        try:
            _save_backtest_prediction(results_list, test_year, engine=engine, db_url=db_url)
            _log("Saved predictions to nfl.game_predictions")
        except Exception as e:
            _log(f"Prediction save failed: {e}\n{traceback.format_exc()}", "WARN")

    return results_json


# ════════════════════════════════════════════════════════════════════════
# _save_backtest_prediction — persist predictions to DB
# ════════════════════════════════════════════════════════════════════════

def _save_backtest_prediction(results: list[dict],
                               test_year: int,
                               engine=None,
                               db_url: str = None):
    """Save backtest predictions to nfl.game_predictions table.

    Results are upserted with handicap JSON data including predicted total,
    closing total, and error.
    """
    if not results:
        return

    from sqlalchemy import create_engine, text

    if engine is None:
        engine = create_engine(db_url or DEFAULT_DB_URL)

    ts = datetime.now().isoformat()

    with engine.begin() as conn:
        for rec in results:
            game_id = rec.get("game_id")
            if not game_id:
                continue

            handicap_json = json.dumps({
                "model_type": "ou",
                "predicted_total": float(rec.get("predicted_total", 0)),
                "actual_total": float(rec.get("actual_total", 0)),
                "closing_total": float(rec.get("closing_total", 0)),
                "error": float(rec.get("error", 0)),
                "home_score": int(rec.get("home_score", 0)),
                "away_score": int(rec.get("away_score", 0)),
                "home_team": rec.get("home", ""),
                "away_team": rec.get("away", ""),
            })

            conn.execute(
                text("""
                    INSERT INTO nfl.game_predictions
                        (game_id, sport, prediction_type, prediction_json, created_at, updated_at)
                    VALUES
                        (:game_id, 'nfl', 'ou', :prediction_json::jsonb, :ts, :ts)
                    ON CONFLICT (game_id, sport, prediction_type)
                    DO UPDATE SET
                        prediction_json = EXCLUDED.prediction_json::jsonb,
                        updated_at = EXCLUDED.updated_at
                """),
                {
                    "game_id": game_id,
                    "prediction_json": handicap_json,
                    "ts": ts,
                },
            )


# ════════════════════════════════════════════════════════════════════════
# CLI Entry Point
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NFL OU XGBoost Model Trainer")
    parser.add_argument("--mode", choices=["all", "single"], default="all",
                        help="Run mode: all = backtest all years, single = single year")
    parser.add_argument("--train-from", type=int, default=DEFAULT_TRAIN_FROM,
                        help=f"Earliest training season year (default: {DEFAULT_TRAIN_FROM})")
    parser.add_argument("--test-year", type=int, default=CURRENT_YEAR - 1,
                        help=f"Test season year (default: {CURRENT_YEAR - 1})")
    parser.add_argument("--skip-db", action="store_true", default=False,
                        help="Skip saving to database")
    parser.add_argument("--db-url", type=str, default=None,
                        help="Database connection URL")

    args = parser.parse_args()

    log(f"Starting NFL OU training run")
    log(f"Mode: {args.mode}, train_from: {args.train_from}, skip_db: {args.skip_db}")

    if args.mode == "all":
        result = run_all_years(
            train_from=args.train_from,
            skip_db=args.skip_db,
            db_url=args.db_url,
        )
    else:
        result = run_single(
            train_from=args.train_from,
            test_year=args.test_year,
            skip_db=args.skip_db,
            db_url=args.db_url,
        )

    if "error" in result:
        log(f"Run failed: {result['error']}", "ERROR")
        sys.exit(1)

    log("Run complete")
    print(json.dumps(result, indent=2, default=str))
