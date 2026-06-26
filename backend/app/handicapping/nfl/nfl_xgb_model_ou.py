"""
NFL XGBoost OU (over/under) model — trains on total points vs closing OU.
Mirrors the MLB OU model pattern exactly, including PKL naming with
{train_id}-{test_year}.pkl and is_current tracking.

USAGE:
    from app.handicapping.nfl.nfl_xgb_model_ou import run_all_years, predict_ou
    results = await run_all_years()
    pred = await predict_ou(game_id, home_abbr, away_abbr)
"""
import math
import os
import pickle
import warnings
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from app.handicapping.db_training import save_training_run, update_pkl_filename

warnings.filterwarnings("ignore")

import logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("earl.nfl.ou_model").info

from app.handicapping.nfl.data_loader import (
    get_data_loader,
    build_features,
    OU_FEATURES,
)

# ── Constants ───────────────────────────────────────────────────────────

_DB_HELPERS_AVAILABLE = True

CURRENT_YEAR = date.today().year
DEFAULT_TRAIN_FROM = 2021

NFL_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl")
NFL_PKL_DIR.mkdir(parents=True, exist_ok=True)
_PKL_DIR = NFL_PKL_DIR

# Module-level caches
_ou_model: xgb.XGBRegressor | None = None
_ou_feature_cache: pd.DataFrame | None = None


def get_model_features() -> list[str]:
    return list(OU_FEATURES)


def _ensure_ou_features() -> list[str]:
    return get_model_features()


def _ou_label(df: pd.DataFrame) -> pd.Series:
    return df["home_score"] + df["away_score"]


def _score_label(df: pd.DataFrame) -> pd.Series:
    return df["home_score"] - df["away_score"]


# ── Backtest ────────────────────────────────────────────────────────────

async def run_backtest(
    raw: pd.DataFrame,
    feats: pd.DataFrame,
    test_year: int,
    feature_set: str = "full",
    train_years: list[int] | None = None,
    training_id: int | None = None,
) -> dict[str, Any]:
    """Run a single backtest year for the OU model.

    Parameters
    ----------
    raw : pd.DataFrame
        Raw game data.
    feats : pd.DataFrame
        Feature-engineered DataFrame.
    test_year : int
        Year to hold out for testing.
    feature_set : str
        Label for the feature set.
    train_years : list[int] | None
        Years to train on. Defaults to all years < test_year.
    training_id : int | None
        DB training_run ID — used to name the PKL {training_id}-{year}.pkl.

    Returns
    -------
    dict of backtest results.
    """
    if train_years is None:
        train_years = [y for y in sorted(feats["year"].dropna().unique()) if y < test_year]

    train_mask = feats["year"].isin(train_years)
    test_mask = feats["year"] == test_year

    y = _ou_label(feats)

    fcols = _ensure_ou_features()
    present = [c for c in fcols if c in feats.columns]
    missing = [c for c in fcols if c not in feats.columns]
    if missing:
        log(f"  OU: missing features: {missing}")

    X_train = feats.loc[train_mask, present].values
    y_train = y.loc[train_mask].values
    X_test = feats.loc[test_mask, present].values
    y_test = y.loc[test_mask].values

    if len(X_train) == 0 or len(X_test) == 0:
        log(f"OU: no data for test_year={test_year}")
        return {}

    model = xgb.XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    actual = y_test
    ous = feats.loc[test_mask, "ou"].values

    # OU evaluation
    ou_correct = 0
    total = len(preds)
    for i in range(total):
        pred_over = preds[i] > ous[i]
        actual_over = actual[i] > ous[i]
        if pred_over == actual_over:
            ou_correct += 1

    accuracy = ou_correct / total if total > 0 else 0.0
    profit = 0.0
    for i in range(total):
        pred_over = preds[i] > ous[i]
        actual_over = actual[i] > ous[i]
        profit += 90.91 if pred_over == actual_over else -100.0
    roi = (profit / (total * 100)) if total > 0 else 0.0

    # Save PKL — use training_id if provided else UUID
    if training_id is not None:
        pkl_stem = f"{training_id}-{test_year}"
    else:
        import uuid
        pkl_stem = f"{uuid.uuid4().hex[:8]}-{test_year}"
    pkl_path = _PKL_DIR / f"{pkl_stem}.pkl"
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)
    log(f"  OU model saved: {pkl_path.name}")

    result = {
        "test_year": test_year,
        "feature_set": feature_set,
        "ou": {
            "pct": round(float(accuracy), 4),
            "correct": int(ou_correct),
            "total": int(total),
            "roi": round(float(roi), 4),
        },
        "train_years": train_years,
        "pkl_stem": pkl_stem,
    }
    log(
        f"  OU {test_year}: {ou_correct}/{total} ({accuracy:.1%}) "
        f"ROI={roi:+.1%}"
    )
    return result


# ── Multi-year runner (mirrors MLB exactly) ─────────────────────────────

async def run_all_years(
    hide_progress: bool = True,
    feature_sets: list[str] | None = None,
    train_from: int = DEFAULT_TRAIN_FROM,
    test_until: int | None = None,
    skip_db: bool = False,
) -> list[dict]:
    """Run backtests for all available years.

    Mirrors the MLB OU pattern: creates one DB training_run, saves per-year
    PKLs as {db_run_id}-{year}.pkl, and updates the pkl_filename column.
    """
    if test_until is None:
        test_until = CURRENT_YEAR
    if feature_sets is None:
        feature_sets = ["full"]

    total_results: list[dict] = []

    dl = get_data_loader()
    raw = await dl.load_training_data(min_year=train_from, max_year=test_until)
    feats = build_features(raw)
    feats = await dl._add_computed_features(feats)
    log(f"Loaded {len(raw)} games, {len(feats.columns)} features")

    # Determine test years
    all_years = sorted(feats["year"].dropna().unique().tolist())
    if len(all_years) < 2:
        log(f"Not enough unique years for OU backtest: {all_years}")
        return []
    test_years = all_years[-2:]

    for feature_set in feature_sets:
        for year in test_years:
            train_years = list(range(train_from, year))
            result = await run_backtest(raw, feats, year, feature_set, train_years)
            if result:
                total_results.append(result)

    # Save to DB
    if _DB_HELPERS_AVAILABLE and save_training_run and not skip_db and total_results:
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
                flat_entry["name"] = f"{year} NFL OU"
                flat_entry["ou_pct"] = r["ou"]["pct"]
                flat_entry["ou_correct"] = r["ou"]["correct"]
                flat_entry["ou_total"] = r["ou"]["total"]

            last_test_year = test_years[-1]
            last_train_years = list(range(train_from, last_test_year))

            db_run_id = save_training_run(
                sport="nfl",
                model_type="ou",
                test_year=last_test_year,
                train_years=last_train_years,
                results_json=results_list,
                pkl_filename="",
                algorithm="xgboost",
                description=f"OU backtest {test_years[0]}-{test_years[-1]}",
            )

            pkl_names = []
            for r in total_results:
                year = r["test_year"]
                stable_name = f"{db_run_id}-{year}.pkl"
                temp_pkls = sorted(_PKL_DIR.glob(f"*-{year}.pkl"),
                                   key=lambda p: p.stat().st_mtime, reverse=True)
                if temp_pkls:
                    try:
                        temp_pkls[0].rename(_PKL_DIR / stable_name)
                        pkl_names.append(stable_name)
                        log(f"  Pkl saved: {stable_name}")
                    except FileNotFoundError:
                        log(f"  WARNING: temp pkl for {year} not found")

            if pkl_names:
                update_pkl_filename("nfl", db_run_id, ",".join(pkl_names))

            log(f"  Saved training run {db_run_id}: {len(total_results)} years")
        except Exception as e:
            log(f"  WARNING: failed to save training run: {e}")

    return total_results


# ── Single-year runner ──────────────────────────────────────────────────

async def run_single(
    test_year: int,
    feature_set: str = "full",
    train_from: int | None = None,
    skip_db: bool = False,
) -> dict[str, Any]:
    """Run backtest for a single test year."""
    if train_from is None:
        train_from = DEFAULT_TRAIN_FROM

    dl = get_data_loader()
    raw = await dl.load_training_data(min_year=train_from, max_year=test_year)
    if raw.empty:
        log(f"No games found")
        return {}

    feats = build_features(raw)
    feats = await dl._add_computed_features(feats)

    all_years = sorted(feats["year"].dropna().unique().tolist())
    train_years = [y for y in all_years if y < test_year]

    result = await run_backtest(raw, feats, test_year, feature_set,
                                 train_years, training_id=None)
    if not result:
        return {}

    pkl_stem = result.get("pkl_stem", "")
    stable_name = f"{pkl_stem}-{test_year}.pkl"

    # Save to DB
    if _DB_HELPERS_AVAILABLE and save_training_run and not skip_db:
        try:
            def _sanitize(obj):
                if isinstance(obj, dict):
                    return {k: _sanitize(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [_sanitize(v) for v in obj]
                elif isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
                    return None
                return obj

            flat_result = _sanitize(result)
            flat_result["name"] = f"{test_year} NFL OU"
            flat_result["ou_pct"] = result["ou"]["pct"]
            flat_result["ou_correct"] = result["ou"]["correct"]
            flat_result["ou_total"] = result["ou"]["total"]

            temp_pkls = sorted(_PKL_DIR.glob(f"*-{test_year}.pkl"),
                               key=lambda p: p.stat().st_mtime, reverse=True)

            db_run_id = save_training_run(
                sport="nfl",
                model_type="ou",
                test_year=test_year,
                train_years=train_years,
                results_json=[flat_result],
                pkl_filename=stable_name,
                algorithm="xgboost",
                description=f"OU backtest {test_year}",
            )

            if temp_pkls:
                try:
                    new_name = f"{db_run_id}-{test_year}.pkl"
                    temp_pkls[0].rename(_PKL_DIR / new_name)
                    update_pkl_filename("nfl", db_run_id, new_name)
                    log(f"  Pkl saved: {new_name}")
                except FileNotFoundError:
                    pass

            log(f"  Saved training run {db_run_id}")
        except Exception as e:
            log(f"  WARNING: failed to save training run: {e}")

    return result


# ── Inference ───────────────────────────────────────────────────────────

def _load_ou_model(path: str | None = None):
    global _ou_model
    if _ou_model is not None:
        return _ou_model
    path = path or str(_PKL_DIR / "nfl_ou_model.pkl")
    if not os.path.exists(path):
        log(f"OU model not found at {path}")
        return None
    with open(path, "rb") as f:
        _ou_model = pickle.load(f)
    log(f"OU model loaded from {path}")
    return _ou_model


async def predict_ou(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
    pkl_path: str | None = None,
) -> dict[str, Any] | None:
    """Inference for a single game."""
    global _ou_feature_cache

    model = _load_ou_model(pkl_path)
    if model is None:
        return None

    dl = get_data_loader()

    if _ou_feature_cache is None:
        log("OU: building feature cache from all recent games...")
        raw_df = await dl.load_training_data(min_year=CURRENT_YEAR - 2,
                                              max_year=CURRENT_YEAR)
        upcoming = await dl.load_inference_data(year=CURRENT_YEAR)
        raw_df = pd.concat([raw_df, upcoming], ignore_index=True)
        feats = build_features(raw_df)
        feats = await dl._add_computed_features(feats)
        _ou_feature_cache = feats

    game_feats = _ou_feature_cache[_ou_feature_cache["game_id"] == game_id]

    if game_feats.empty:
        game_feats = _ou_feature_cache[
            (_ou_feature_cache["home_abbr"] == home_abbr)
            & (_ou_feature_cache["away_abbr"] == away_abbr)
        ].sort_values("year", ascending=False)
        if game_feats.empty:
            return None
        game_feats = game_feats.iloc[:1]

    fcols = _ensure_ou_features()
    present = [c for c in fcols if c in game_feats.columns]
    if not present:
        return None

    X = game_feats[present].values
    pred_total = float(model.predict(X)[0])
    ou_line = float(game_feats["ou"].iloc[0]) if "ou" in game_feats.columns else 0.0

    ou_pick = "OVER" if pred_total > ou_line else "UNDER"
    confidence = min(abs(pred_total - ou_line) / 20.0, 0.95) if ou_line != 0 else 0.5

    return {
        "ou_pick": ou_pick,
        "predicted_total": round(pred_total, 1),
        "ou_line": ou_line,
        "confidence": round(confidence, 3),
        "game_id": game_id,
    }


# ── Save / Load ─────────────────────────────────────────────────────────

def save_model(model: xgb.XGBRegressor, path: str = None) -> str:
    path = path or str(_PKL_DIR / "nfl_ou_model.pkl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    log(f"OU model saved to {path}")
    return path


def load_model(path: str = None) -> xgb.XGBRegressor | None:
    global _ou_model
    path = path or str(_PKL_DIR / "nfl_ou_model.pkl")
    return _load_ou_model(path)


# ── CLI ─────────────────────────────────────────────────────────────────

async def demo():
    results = await run_all_years(skip_db=True)
    log(f"\nOU Results: {len(results)} years")
    for r in results:
        log(f"  {r['test_year']}: acc={r['ou']['pct']:.1%} ROI={r['ou']['roi']:+.1%}")


if __name__ == "__main__":
    import argparse, asyncio
    parser = argparse.ArgumentParser(description="NFL OU XGBoost model")
    parser.add_argument("--mode", choices=["single", "all"], default="all",
                        help="Run single test year or all years")
    parser.add_argument("--test-year", type=int, default=None,
                        help="Year to test (single mode)")
    parser.add_argument("--skip-db", action="store_true", default=False,
                        help="Skip saving training run to DB")
    args = parser.parse_args()

    if args.mode == "all":
        asyncio.run(demo())
    else:
        asyncio.run(run_single(test_year=args.test_year or CURRENT_YEAR - 1))
