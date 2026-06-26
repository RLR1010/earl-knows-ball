"""
NFL XGBoost ATS model — trains on margin of victory vs closing spread.
Mirrors the MLB ATS model pattern exactly, including PKL naming with
{train_id}-{test_year}.pkl and is_current tracking.

USAGE:
    from app.handicapping.nfl.nfl_xgb_model_ats import run_all_years, predict_ats
    results = await run_all_years()
    pred = await predict_ats(game_id, home_abbr, away_abbr)
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
log = logging.getLogger("earl.nfl.ats_model").info

from app.handicapping.nfl.data_loader import (
    get_data_loader,
    build_features,
    ATS_FEATURES,
)

# ── Constants ───────────────────────────────────────────────────────────

_DB_HELPERS_AVAILABLE = True

CURRENT_YEAR = date.today().year
DEFAULT_TRAIN_FROM = 2021

# PKL_DIR
NFL_PKL_DIR = Path("/home/rich/.openclaw/workspace/earl-knows-football/data/models/nfl")
NFL_PKL_DIR.mkdir(parents=True, exist_ok=True)
_PKL_DIR = NFL_PKL_DIR

# Module-level caches
_ats_model: xgb.XGBRegressor | None = None
_ats_feature_cache: pd.DataFrame | None = None


def get_model_features() -> list[str]:
    return list(ATS_FEATURES)


def _ensure_ats_features() -> list[str]:
    return get_model_features()


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
    """Run a single backtest year, saving the model PKL.

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

    y = _score_label(feats)

    fcols = _ensure_ats_features()
    present = [c for c in fcols if c in feats.columns]
    missing = [c for c in fcols if c not in feats.columns]
    if missing:
        log(f"  ATS: missing features: {missing}")

    X_train = feats.loc[train_mask, present].values
    y_train = y.loc[train_mask].values
    X_test = feats.loc[test_mask, present].values
    y_test = y.loc[test_mask].values

    if len(X_train) == 0 or len(X_test) == 0:
        log(f"ATS: no data for test_year={test_year}")
        return {}

    model = xgb.XGBRegressor(
        n_estimators=300, max_depth=5, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        random_state=42, n_jobs=-1,
    )
    model.fit(X_train, y_train)

    preds = model.predict(X_test)
    actual = y_test
    spreads = feats.loc[test_mask, "spread"].values

    # ATS evaluation
    ats_correct = 0
    total = len(preds)
    for i in range(total):
        pred_covers = preds[i] > -spreads[i]
        actual_covers = actual[i] > -spreads[i]
        if pred_covers == actual_covers:
            ats_correct += 1

    accuracy = ats_correct / total if total > 0 else 0.0
    profit = 0.0
    for i in range(total):
        pred_covers = preds[i] > -spreads[i]
        actual_covers = actual[i] > -spreads[i]
        profit += 90.91 if pred_covers == actual_covers else -100.0
    roi = (profit / (total * 100)) if total > 0 else 0.0

    # Save PKL — use training_id if provided (for batch) else use a UUID
    if training_id is not None:
        pkl_stem = f"{training_id}-{test_year}"
    else:
        import uuid
        pkl_stem = f"{uuid.uuid4().hex[:8]}-{test_year}"
    pkl_path = _PKL_DIR / f"{pkl_stem}.pkl"
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)
    log(f"  ATS model saved: {pkl_path.name}")

    # Build the nested result (mirrors MLB format)
    result = {
        "test_year": test_year,
        "feature_set": feature_set,
        "ats": {
            "pct": round(float(accuracy), 4),
            "correct": int(ats_correct),
            "total": int(total),
            "roi": round(float(roi), 4),
        },
        "train_years": train_years,
        "pkl_stem": pkl_stem,
    }
    log(
        f"  ATS {test_year}: {ats_correct}/{total} ({accuracy:.1%}) "
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

    Mirrors the MLB ATS pattern: creates one DB training_run, saves per-year
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

    # Determine test years — same as MLB: final 2 seasons
    all_years = sorted(feats["year"].dropna().unique().tolist())
    if len(all_years) < 2:
        log(f"Not enough unique years for ATS backtest: {all_years}")
        return []
    test_years = all_years[-2:]  # last 2

    for feature_set in feature_sets:
        for year in test_years:
            train_years = list(range(train_from, year))
            result = await run_backtest(raw, feats, year, feature_set, train_years)
            if result:
                total_results.append(result)

    # Save to DB (mirrors MLB pattern)
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

            # Build results list with name and flat fields for the admin frontend
            results_list = [_sanitize(r) for r in total_results]
            for r, flat_entry in zip(total_results, results_list):
                year = r["test_year"]
                flat_entry["name"] = f"{year} NFL ATS"
                flat_entry["ats_pct"] = r["ats"]["pct"]
                flat_entry["ats_correct"] = r["ats"]["correct"]
                flat_entry["ats_total"] = r["ats"]["total"]

            last_test_year = test_years[-1]
            last_train_years = list(range(train_from, last_test_year))

            # First save to get training_id, then rename PKLs
            db_run_id = save_training_run(
                sport="nfl",
                model_type="ats",
                test_year=last_test_year,
                train_years=last_train_years,
                results_json=results_list,
                pkl_filename="",  # placeholder, updated below
                algorithm="xgboost",
                description=f"ATS backtest {test_years[0]}-{test_years[-1]}",
            )

            # Rename temp PKLs to {db_run_id}-{year}.pkl and track names
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
    """Run backtest for a single test year.

    Saves a PKL as {uuid}-{year}.pkl and records the training run in the DB.
    """
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

    # Generate a UUID for the PKL before running
    import uuid
    temp_uuid = uuid.uuid4().hex[:8]

    result = await run_backtest(raw, feats, test_year, feature_set,
                                 train_years, training_id=None)
    if not result:
        return {}

    # Rename the temp PKL to use uuid as the stem
    temp_pkls = sorted(_PKL_DIR.glob(f"*-{test_year}.pkl"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    pkl_stem = result.get("pkl_stem", temp_uuid)
    stable_name = f"{pkl_stem}-{test_year}.pkl"
    # Actually the run_backtest already saved it under UUID stem
    # We just need to rename if db_run_id is available

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
            flat_result["name"] = f"{test_year} NFL ATS"
            flat_result["ats_pct"] = result["ats"]["pct"]
            flat_result["ats_correct"] = result["ats"]["correct"]
            flat_result["ats_total"] = result["ats"]["total"]

            db_run_id = save_training_run(
                sport="nfl",
                model_type="ats",
                test_year=test_year,
                train_years=train_years,
                results_json=[flat_result],
                pkl_filename=stable_name,
                algorithm="xgboost",
                description=f"ATS backtest {test_year}",
            )

            # Rename temp PKL to use db_run_id
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

def _load_ats_model(path: str | None = None):
    global _ats_model
    if _ats_model is not None:
        return _ats_model
    path = path or _PKL_DIR / "nfl_ats_model.pkl"
    if not os.path.exists(path):
        log(f"ATS model not found at {path}")
        return None
    with open(path, "rb") as f:
        _ats_model = pickle.load(f)
    log(f"ATS model loaded from {path}")
    return _ats_model


async def predict_ats(
    game_id: int,
    home_abbr: str,
    away_abbr: str,
    pkl_path: str | None = None,
) -> dict[str, Any] | None:
    """Inference for a single game."""
    global _ats_feature_cache

    model = _load_ats_model(pkl_path)
    if model is None:
        return None

    dl = get_data_loader()

    if _ats_feature_cache is None:
        log("ATS: building feature cache from all recent games...")
        raw_df = await dl.load_training_data(min_year=CURRENT_YEAR - 2,
                                              max_year=CURRENT_YEAR)
        upcoming = await dl.load_inference_data(year=CURRENT_YEAR)
        raw_df = pd.concat([raw_df, upcoming], ignore_index=True)
        feats = build_features(raw_df)
        feats = await dl._add_computed_features(feats)
        _ats_feature_cache = feats

    game_feats = _ats_feature_cache[_ats_feature_cache["game_id"] == game_id]

    if game_feats.empty:
        game_feats = _ats_feature_cache[
            (_ats_feature_cache["home_abbr"] == home_abbr)
            & (_ats_feature_cache["away_abbr"] == away_abbr)
        ].sort_values("year", ascending=False)
        if game_feats.empty:
            return None
        game_feats = game_feats.iloc[:1]

    fcols = _ensure_ats_features()
    present = [c for c in fcols if c in game_feats.columns]
    if not present:
        return None

    X = game_feats[present].values
    pred_margin = float(model.predict(X)[0])
    spread = float(game_feats["spread"].iloc[0]) if "spread" in game_feats.columns else 0.0

    ats_pick = "HOME" if pred_margin > -spread else "AWAY"
    confidence = min(abs(pred_margin + spread) / 10.0, 0.95) if spread != 0 else 0.5

    return {
        "ats_pick": ats_pick,
        "predicted_margin": round(pred_margin, 1),
        "spread": spread,
        "confidence": round(confidence, 3),
        "game_id": game_id,
    }


# ── Save / Load ─────────────────────────────────────────────────────────

def save_model(model: xgb.XGBRegressor, path: str = None) -> str:
    path = path or str(_PKL_DIR / "nfl_ats_model.pkl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    log(f"ATS model saved to {path}")
    return path


def load_model(path: str = None) -> xgb.XGBRegressor | None:
    global _ats_model
    path = path or str(_PKL_DIR / "nfl_ats_model.pkl")
    return _load_ats_model(path)


# ── CLI ─────────────────────────────────────────────────────────────────

async def demo():
    results = await run_all_years(skip_db=True)
    log(f"\nATS Results: {len(results)} years")
    for r in results:
        log(f"  {r['test_year']}: acc={r['ats']['pct']:.1%} ROI={r['ats']['roi']:+.1%}")


if __name__ == "__main__":
    import argparse, asyncio
    parser = argparse.ArgumentParser(description="NFL ATS XGBoost model")
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
