"""
NFL handicapping engine — load models, predict games, build pick cards,
run backtests, and save results to the database.

Mirrors ``mlb/mlb_engine.py`` but adapted for NFL (spread-based betting,
no run line, scores instead of runs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import xgboost as xgb
from sqlalchemy import text, create_engine
from sqlalchemy import delete as sa_delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.nfl.game_prediction import NFLGamePrediction
from app.handicapping.nfl.data_loader import NFLDataLoader, get_data_loader, get_model_features
from app.handicapping.calibrate_confidence import calibrate

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
# Match the same directory nfl_xgb_model_ats.py / nfl_xgb_model_ou.py save to:
MODELS_DIR = Path.home() / ".openclaw" / "workspace" / "earl-knows-football" / "data" / "models" / "nfl"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

_now = datetime.now()
CURRENT_SEASON = _now.year if _now.month >= 4 else _now.year - 1
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)
NFL_SCHEMA = "nfl"


def _profit_per_100(odds: float) -> float:
    """Convert American odds to profit per $100 risked."""
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


# ── Async DB setup ───────────────────────────────────────────────────────────────
ASYNC_DSN: str = DB_DSN.replace("postgresql://", "postgresql+asyncpg://")
_async_engine = None
_async_sessionmaker = None


# ── Pick-card feature extraction ────────────────────────────────────────────────


def _extract_pick_card_features(row, feature_metadata: Dict[str, Dict[str, str]]) -> str:
    """Return JSON string of pick_card feature values enriched with display_name
    and description from nfl.features.
    """

    def _sanitize(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    features = {}
    for name, meta in feature_metadata.items():
        if name in row.index or name in row:
            value = _sanitize(row.get(name))
            if value is not None:
                features[name] = {
                    "value": value,
                    "display_name": meta["display_name"],
                    "description": meta["description"],
                }
    return json.dumps(features, default=str)


def _get_async_session() -> async_sessionmaker:
    global _async_engine, _async_sessionmaker
    if _async_sessionmaker is None:
        _async_engine = create_async_engine(ASYNC_DSN, echo=False, pool_pre_ping=True)
        _async_sessionmaker = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _async_sessionmaker


# ── Model resolution helpers ─────────────────────────────────────────────────────


def _resolve_year_pkl_paths(model_type: str) -> Dict[int, Path]:
    """Return {year: Path} for all per-year model pickle files.

    ``model_type`` is ``'ats'`` or ``'ou'``.

    Loads the pkl_filename list from the current live training run
    (``nfl.training_runs WHERE is_live = 't'``), parses
    ``<uuid>-<year>.pkl`` entries, and resolves them under
    ``backend/app/handicapping/nfl/models/xgboost/``.

    Returns an empty dict when no live run exists or no pkl files found.
    """
    from app.handicapping.db_training import get_live_training_run
    run = get_live_training_run("nfl", model_type)
    if run is None:
        logger.warning("  No live training_run for nfl/%s", model_type)
        return {}

    raw = run.get("pkl_filename", "")
    if not raw:
        logger.warning("  training_run for nfl/%s has empty pkl_filename", model_type)
        return {}

    parts = [s.strip() for s in raw.split(",") if s.strip()]
    out: Dict[int, Path] = {}
    for fname in parts:
        # Expect pattern: <uuid>-<year>.pkl
        stem = fname.rsplit(".", 1)[0]   # remove .pkl
        if "-" in stem:
            year_str = stem.rsplit("-", 1)[-1]
            try:
                year = int(year_str)
            except ValueError:
                continue
            p = MODELS_DIR / fname
            if p.exists():
                out[year] = p
            else:
                logger.warning("  pkl file not found on disk: %s", p)
    if out:
        logger.info("  Year pkl files for nfl/%s: %s", model_type, out)
    else:
        logger.warning("  No year pkl files found for nfl/%s", model_type)
    return out


def _load_model_for_year(model_type: str, year: int) -> Optional[xgb.Booster]:
    """Load a per-year model for a specific calendar year."""
    paths = _resolve_year_pkl_paths(model_type)
    p = paths.get(year)
    if p and p.exists():
        with open(p, "rb") as f:
            model = pickle.load(f)
        logger.info("Loaded %s model for year %d from %s", model_type, year, p)
        return model
    logger.warning("No %s model found for year %d", model_type, year)
    return None


# ── Feature helpers ──────────────────────────────────────────────────────────────

_FEATURES_CACHE_ATS: Optional[List[str]] = None
_FEATURES_CACHE_OU: Optional[List[str]] = None
_PICK_CARD_FEATURE_METADATA: Optional[Dict[str, Dict[str, str]]] = None


def _load_pick_card_feature_metadata() -> Dict[str, Dict[str, str]]:
    """Return pick_card feature metadata: {name: {display_name, description}}.

    Cached module-level so the DB is hit only once per process.
    """
    global _PICK_CARD_FEATURE_METADATA
    if _PICK_CARD_FEATURE_METADATA is not None:
        return _PICK_CARD_FEATURE_METADATA
    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT name, display_name, description "
                "FROM nfl.features WHERE pick_card = TRUE"
            )
            _PICK_CARD_FEATURE_METADATA = {
                r[0]: {"display_name": r[1] or r[0], "description": r[2] or ""}
                for r in cur.fetchall()
            }
            return _PICK_CARD_FEATURE_METADATA
    finally:
        conn.close()


# ── Builder key → feature name mappings for enrichment ──
_NFL_HOME_STATS_FEATURE_MAP = {
    "points_for": "hpf",
    "points_against": "hpa",
    "win_pct_r5": "home_win_pct_r5",
    "margin_r5": "home_margin_r3",
    "margin_r10": "home_margin_r10",
    "cover_pct_r5": "home_cover_pct_r5",
    "season_ats_pct": "home_season_ats_pct",
    "embarrassed": "home_embarrassed",
    "rest_days": "home_rest_days",
}

_NFL_AWAY_STATS_FEATURE_MAP = {
    "points_for": "apf",
    "points_against": "apa",
    "win_pct_r5": "away_win_pct_r5",
    "margin_r5": "away_margin_r3",
    "margin_r10": "away_margin_r10",
    "cover_pct_r5": "away_cover_pct_r5",
    "season_ats_pct": "away_season_ats_pct",
    "embarrassed": "away_embarrassed",
    "rest_days": "away_rest_days",
}

_NFL_SITUATIONAL_FEATURE_MAP = {
    "travel_miles": "travel_miles",
    "tz_diff": "tz_diff",
    "dome": "is_dome",
    "rest": "rest_diff",
    "is_division": "is_div",
    "weather": "weather_condition",
    "wind": "wind",
    "venue": "venue",
    "surface": "surface",
    "roof_type": "roof_type",
}


def _enrich_dict_with_metadata(
    d: dict, key_map: dict, metadata: Dict[str, Dict[str, str]] | None,
) -> dict:
    """Replace flat values with ``{value, display_name, description}`` for every
    key in *d* that appears in *key_map* and has an entry in *metadata*."""
    if not metadata:
        return d
    result = dict(d)
    for k, feat_name in key_map.items():
        if k in result and feat_name in metadata:
            meta = metadata[feat_name]
            result[k] = {
                "value": d[k],
                "display_name": meta["display_name"],
                "description": meta["description"],
            }
    return result


def _get_features(model_type: str) -> List[str]:
    """Return feature columns from ``nfl.features`` for the given model type.

    Queries for features flagged ``live_ats = TRUE`` or ``live_ou = TRUE``
    (the flags that ``set_training_run_as_current`` marks).
    """
    global _FEATURES_CACHE_ATS, _FEATURES_CACHE_OU
    if model_type == "ats":
        if _FEATURES_CACHE_ATS is not None:
            return _FEATURES_CACHE_ATS
    elif model_type == "ou":
        if _FEATURES_CACHE_OU is not None:
            return _FEATURES_CACHE_OU

    conn = psycopg2.connect(DB_DSN)
    try:
        with conn.cursor() as cur:
            if model_type == "ats":
                feats = get_model_features(cur, live_ats_only=True)
                _FEATURES_CACHE_ATS = feats
            else:
                feats = get_model_features(cur, live_ou_only=True)
                _FEATURES_CACHE_OU = feats
    finally:
        conn.close()
    return feats


def _extract_feature_vector(
    row: pd.Series,
    model_type: str,
) -> Tuple[np.ndarray, List[str]]:
    """Build a feature vector (values, names) from a DataFrame row."""
    feature_names = _get_features(model_type)
    values = []
    names = []
    for feat in feature_names:
        val = row.get(feat)
        if val is None or (isinstance(val, float) and np.isnan(val)):
            val = 0.0
        values.append(float(val))
        names.append(feat)
    return np.array([values], dtype=np.float32), names


# ── Main handicapper class ───────────────────────────────────────────────────────


# ── Backtest season ──────────────────────────────────────────────────────────────


async def backtest_season(
    years: Optional[List[int]] = None,
    limit: Optional[int] = None,
    save_results: bool = True,
    db: AsyncSession = None,
) -> Dict[str, Any]:
    """Backtest NFL models over one or more seasons.

    Loads per-year pkl files from the live training run's ``pkl_filename``
    (stored in ``nfl.training_runs WHERE is_live = 't'``).  No training is
    performed.

    Parameters
    ----------
    years : list of int, optional
        Season years to test.  Defaults to all with pkl files available.
    limit : int, optional
        Max rows to load from the data loader (for quick tests).
    save_results : bool
        Whether to write per-game predictions to ``nfl.game_predictions``.

    Returns
    -------
    dict with ``ats_results``, ``ou_results``, ``test_years``.
    """
    ats_paths = _resolve_year_pkl_paths("ats")
    ou_paths = _resolve_year_pkl_paths("ou")

    if not ats_paths and not ou_paths:
        return {"error": "no live training run found with pkl files"}

    if years is None:
        years = [y for y in [2024, 2025] if y in ats_paths or y in ou_paths]

    logger.info("Backtest years (from pkl): %s", years)

    dl = get_data_loader()
    df = dl.load_data(limit=limit)
    if df.empty:
        return {"error": "no data"}

    df["total_points"] = df["home_score"] + df["away_score"]
    df = df.sort_values(["season_year", "week"]).reset_index(drop=True)

    ats_results: List[Dict[str, Any]] = []
    ou_results: List[Dict[str, Any]] = []

    for test_year in years:
        if test_year in ats_paths:
            model = _load_model_for_year("ats", test_year)
            if model is not None:
                year_df = df[df["season_year"] == test_year].copy()
                if year_df.empty:
                    logger.warning("  No data for %d ATS test", test_year)
                    ats_results.append({"year": test_year, "error": "no data"})
                else:
                    ats_res = _evaluate_year_model(year_df, model, "ats")
                    ats_res["year"] = test_year
                    ats_results.append(ats_res)
            else:
                ats_results.append({"year": test_year, "error": "model load failed"})
        else:
            ats_results.append({"year": test_year, "error": "no pkl path on disk"})

        if test_year in ou_paths:
            model = _load_model_for_year("ou", test_year)
            if model is not None:
                year_df = df[df["season_year"] == test_year].copy()
                if year_df.empty:
                    logger.warning("  No data for %d OU test", test_year)
                    ou_results.append({"year": test_year, "error": "no data"})
                else:
                    ou_res = _evaluate_year_model(year_df, model, "ou")
                    ou_res["year"] = test_year
                    ou_results.append(ou_res)
            else:
                ou_results.append({"year": test_year, "error": "model load failed"})
        else:
            ou_results.append({"year": test_year, "error": "no pkl path on disk"})

        if save_results:
            year_df = df[df["season_year"] == test_year]
            logger.info("Saving %d backtest predictions for %s...", len(year_df), test_year)
            ats_model = _load_model_for_year("ats", test_year)
            ou_model = _load_model_for_year("ou", test_year)
            for idx, row in year_df.iterrows():
                ats_feats = _get_features("ats") if ats_model is not None else None
                ou_feats = _get_features("ou") if ou_model is not None else None
                await _save_backtest_prediction(
                    int(row.get("game_id", 0)), row,
                    ats_model=ats_model,
                    ou_model=ou_model,
                    ats_features=ats_feats,
                    ou_features=ou_feats,
                    db=db,
                )

    return {
        "ats_results": ats_results,
        "ou_results": ou_results,
        "n_years": len([r for r in ats_results if "error" not in r]) + len([r for r in ou_results if "error" not in r]),
        "test_years": years,
    }


# ── Batch predict upcoming games ───────────────────────────────────────────────


async def batch_predict_upcoming_games(
    game_ids: List[int],
    year: Optional[int] = None,
    db: AsyncSession = None,
) -> List[Dict[str, Any]]:
    """Predict multiple upcoming NFL games — returns a list of pick-card dicts.
    Matches the MLB ``batch_predict_upcoming_games`` pattern.
    """
    # Load model once for all games
    year = year or CURRENT_NFL_YEAR
    ats_model = _load_model_for_year("ats", year)
    ou_model = _load_model_for_year("ou", year)
    dl = get_data_loader()

    results: List[Dict[str, Any]] = []
    for gid in game_ids:
        try:
            df = dl.load_inference_data(game_ids=[gid])
            if df.empty:
                logger.warning("No inference data for game %s", gid)
                continue
            row = df.iloc[0]

            home_str = _str_safe(row.get("home_abbr", row.get("home_team", "")))
            away_str = _str_safe(row.get("away_abbr", row.get("away_team", "")))
            spread = _float_safe(row.get("spread"))
            over_under = _float_safe(row.get("closing_ou", row.get("over_under", 0)))

            # ATS prediction
            # ATS prediction (regression: predicted home margin, home_score - away_score)
            ats_margin = 0.0
            if ats_model is not None:
                feats, names = _extract_feature_vector(row, "ats")
                if feats is not None:
                    dmat = xgb.DMatrix(feats, feature_names=names)
                    ats_margin = float(ats_model.predict(dmat)[0])

            # OU prediction (regression: predicts total_points directly)
            predicted_total = None
            if ou_model is not None:
                feats, names = _extract_feature_vector(row, "ou")
                if feats is not None:
                    dmat = xgb.DMatrix(feats, feature_names=names)
                    predicted_total = float(ou_model.predict(dmat)[0])

            # Build pick card
            pred_margin = ats_margin  # direct margin prediction (home_score - away_score)
            margin_conf = round(min(0.5 + abs(pred_margin + spread) * 0.04, 0.90), 4) if spread else 0.5

            spread_pick = None
            if spread is not None:
                if spread < 0:
                    home_covers = pred_margin > abs(spread)
                else:
                    home_covers = pred_margin > -spread
                spread_pick = home_str if home_covers else away_str

            ou_pick = None
            if predicted_total is not None and over_under is not None:
                ou_pick = "Over" if predicted_total > over_under else "Under"

            ml_pick = home_str if pred_margin > 0 else away_str

            predicted_home, predicted_away = None, None
            if predicted_total is not None and spread is not None:
                predicted_home = round((predicted_total + pred_margin) / 2)
                predicted_away = round((predicted_total - pred_margin) / 2)
                if pred_margin < 0:
                    predicted_home, predicted_away = predicted_away, predicted_home

            result: Dict[str, Any] = {
                "game_id": gid,
                "home_team": home_str,
                "away_team": away_str,
                "spread": spread,
                "predicted_home_score": predicted_home,
                "predicted_away_score": predicted_away,
                "margin_conf": margin_conf,
                "ats_prediction": pred_margin,
                "ou_predicted_total": round(predicted_total, 2) if predicted_total is not None else None,
                "ou_pick": ou_pick,
                "spread_pick": spread_pick,
                "ml_pick": ml_pick,
                "home_ml": _float_safe(row.get("home_ml")),
                "away_ml": _float_safe(row.get("away_ml")),
                "closing_ou": _float_safe(over_under),
                "spread_home_odds": _float_safe(row.get("spread_home_odds")),
                "spread_away_odds": _float_safe(row.get("spread_away_odds")),
                "over_odds": _float_safe(row.get("over_odds")),
                "under_odds": _float_safe(row.get("under_odds")),
            }
            # Enrich with handicapper info
            pc_feats = _load_pick_card_feature_metadata()
            result.update({
                "home_stats": _enrich_dict_with_metadata(
                    _build_nfl_home_stats(row),
                    _NFL_HOME_STATS_FEATURE_MAP, pc_feats,
                ),
                "away_stats": _enrich_dict_with_metadata(
                    _build_nfl_away_stats(row),
                    _NFL_AWAY_STATS_FEATURE_MAP, pc_feats,
                ),
                "situational": _enrich_dict_with_metadata(
                    _build_nfl_situational(row),
                    _NFL_SITUATIONAL_FEATURE_MAP, pc_feats,
                ),
                "splits": _build_nfl_splits(row),
                "home_abbr": home_str,
                "away_abbr": away_str,
            })
            # Calibrated confidence
            # Calibrated confidences computed in _save_api_prediction via calibrate()
            # Features for pick-card
            result["features"] = json.loads(_extract_pick_card_features(row, pc_feats))

            # Save to DB if a session was provided (API pattern)
            if db is not None:
                await _save_api_prediction(result)

            results.append(result)
        except Exception:
            logger.exception("Error handicapping game %s", gid)
    return results
def _evaluate_year_model(year_df: pd.DataFrame, model: xgb.Booster, model_type: str) -> Dict[str, Any]:
    """Evaluate a single per-year model on a full season's games.

    Both ATS and OU models are trained with reg:squarederror (regression):
      - ATS predicts home_score_margin  (continuous margin)
      - OU  predicts total_points       (continuous total)

    For ATS the model output is a MARGIN, not a probability.
    Convert to an ATS pick: predicted_margin + spread > 0.
    For OU, the model output is directly the predicted total.

    Returns dict with accuracy (ATS) or MAE/RMSE (OU), AUC, and game-level
    predictions (list of dicts).  Matches the MLB ``backtest_season`` pattern.
    """
    from sklearn.metrics import roc_auc_score, mean_absolute_error, mean_squared_error

    labels = []
    probs = []
    total = 0
    correct = 0

    for idx, row in year_df.iterrows():
        feat_vals, feat_names = _extract_feature_vector(row, model_type)
        dmat = xgb.DMatrix(feat_vals, feature_names=feat_names)
        prob = float(model.predict(dmat)[0])
        probs.append(prob)

        if model_type == "ats":
            spread = row.get("spread", 0) or 0
            home_score = row.get("home_score", 0) or 0
            away_score = row.get("away_score", 0) or 0
            margin_vs_spread = home_score - away_score + spread
            if margin_vs_spread == 0:
                # Push — skip in accuracy calculation
                continue
            actual_cover = int(margin_vs_spread > 0)
            # regression model outputs margin, not probability
            pred_cover = int(prob + spread > 0)
            labels.append(actual_cover)
            total += 1
            if pred_cover == actual_cover:
                correct += 1
        else:
            total_points = row.get("total_points", 0) or 0
            labels.append(total_points)

    result: Dict[str, Any] = {
        "total_games": total,
    }

    if model_type == "ats" and total > 0:
        result["accuracy"] = correct / total
        result["correct"] = correct
        result["n_train"] = 0
        result["n_test"] = total
        # probs holds raw margin predictions — AUC doesn't apply to regression
        # outputs, but we keep the field for consistency
        result["auc"] = None
    elif model_type == "ou" and len(labels) > 0:
        result["mae"] = round(float(mean_absolute_error(labels, probs)), 2)
        result["rmse"] = round(float(np.sqrt(mean_squared_error(labels, probs))), 2)
        result["n_train"] = 0
        result["n_test"] = len(labels)

    return result


# ── Save API prediction ─────────────────────────────────────────────────────────


async def _save_api_prediction(result: Dict[str, Any]) -> None:
    """Save a single game prediction using the NFLGamePrediction ORM model."""
    game_id = result.get("game_id")
    if not game_id:
        return

    async with get_async_session() as session:
        # Wipe any previous API prediction for this game
        await session.execute(
            sa_delete(NFLGamePrediction).where(
                NFLGamePrediction.game_id == game_id,
                NFLGamePrediction.source == "api",
            )
        )

        now = datetime.now(timezone.utc)
        source = result.get("source", "api")

        predicted_total = result.get("ou_predicted_total")
        pred_margin = result.get("ats_prediction", 0.0)  # direct margin prediction
        spread = result.get("spread", 0) or 0

        pred_home_score = None
        pred_away_score = None
        if predicted_total is not None:
            pred_home_score = max(0, round((predicted_total + pred_margin) / 2.0))
            pred_away_score = max(0, round((predicted_total - pred_margin) / 2.0))
            if pred_margin < 0:
                pred_home_score, pred_away_score = pred_away_score, pred_home_score

        ou_pick = result.get("ou_pick")
        spread_pick = result.get("spread_pick")
        ml_pick = result.get("ml_pick")

        # Map odds to the pick side
        ats_odds_value = None
        sp_pick = spread_pick or ""
        home_abbr = result.get("home_abbr") or result.get("home_team", "")
        away_abbr = result.get("away_abbr") or result.get("away_team", "")
        if sp_pick:
            pick_team = sp_pick.split(" ")[0]
            if pick_team == home_abbr:
                ats_odds_value = result.get("spread_home_odds")
            elif pick_team == away_abbr:
                ats_odds_value = result.get("spread_away_odds")

        ou_odds_value = None
        if ou_pick == "Over":
            ou_odds_value = result.get("over_odds")
        elif ou_pick == "Under":
            ou_odds_value = result.get("under_odds")

        ml_odds_value = None
        if ml_pick == home_abbr:
            ml_odds_value = result.get("home_ml")
        elif ml_pick == away_abbr:
            ml_odds_value = result.get("away_ml")

        # ── Raw confidence computations ───
        ats_raw = round(min(0.5 + abs(pred_margin + spread) * 0.04, 0.90), 4) if spread else 0.5
        ml_raw = round(min(0.5 + abs(pred_margin) * 0.025, 0.92), 4)
        ou_raw = round(min(0.5 + abs(predicted_total - result.get("closing_ou", 0)) * 0.07, 0.92), 4) if (predicted_total is not None and result.get("closing_ou")) else 0.5

        # ── Calibrated confidences ───
        ats_cal = calibrate(ats_raw, "ats", "nfl")
        ml_cal = calibrate(ml_raw, "ml", "nfl")
        ou_cal = calibrate(ou_raw, "ou", "nfl")

        # ── Expected value helpers ───
        def _ev(conf_: float, odds_: float) -> float:
            profit_if_win = 100.0 * _profit_per_100(odds_)
            return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

        ats_ev = _ev(ats_cal, ats_odds_value) if ats_odds_value else None
        ou_ev = _ev(ou_cal, ou_odds_value) if ou_odds_value else None
        ml_ev = _ev(ml_cal, ml_odds_value) if ml_odds_value else None

        # Handicapper info
        home_stats = result.get("home_stats")
        away_stats = result.get("away_stats")
        situational = result.get("situational")
        splits = result.get("splits")

        features = result.get("features", {})

        rec = NFLGamePrediction(
            game_id=game_id,
            predicted_home_score=pred_home_score,
            predicted_away_score=pred_away_score,
            predicted_total=predicted_total,
            predicted_margin=pred_margin,
            margin_conf=ats_raw,
            ml_conf=ml_raw,
            ou_conf=ou_raw,
            ou_pick=ou_pick,
            spread_pick=spread_pick,
            ml_pick=ml_pick,
            ats_conf_cal=ats_cal,
            ml_conf_cal=ml_cal,
            ou_conf_cal=ou_cal,
            ats_ev=ats_ev,
            ou_ev=ou_ev,
            ml_ev=ml_ev,
            ats_profit=result.get("ats_profit"),
            ou_profit=result.get("ou_profit"),
            ml_profit=result.get("ml_profit"),
            ats_odds=round(ats_odds_value) if ats_odds_value else None,
            ou_odds=round(ou_odds_value) if ou_odds_value else None,
            ml_odds=round(ml_odds_value) if ml_odds_value else None,
            home_stats_json=json.dumps(home_stats) if home_stats else None,
            away_stats_json=json.dumps(away_stats) if away_stats else None,
            situational_json=json.dumps(situational) if situational else None,
            splits_json=json.dumps(splits) if splits else None,
            features_json=json.dumps(features, default=str) if features else None,
            source=source,
            created_at=now,
        )
        session.add(rec)
        await session.commit()
async def _save_backtest_prediction(
    game_id: int,
    row,
    ats_model=None,
    ou_model=None,
    ats_features=None,
    ou_features=None,
    db: AsyncSession = None,
) -> None:
    """Save a single backtest prediction using the NFLGamePrediction ORM model.

    Mirrors the MLB ``_save_backtest_prediction`` pattern.
    Relies on ``row`` being a pd.Series / dict-like with the actual game outcome.
    """
    if not game_id:
        return

    engine = None
    close_session = False
    if db is None:
        engine = create_engine(DB_DSN.replace("+asyncpg", ""))
        sync_session = sessionmaker(bind=engine)
        sync_sesh = sync_session()
    else:
        sync_sesh = None

    try:
        home_str = _str_safe(row.get("home_abbr", row.get("home_team", "")))
        away_str = _str_safe(row.get("away_abbr", row.get("away_team", "")))
        spread = _float_safe(row.get("closing_spread", row.get("spread", 0)))
        over_under = _float_safe(row.get("closing_ou", row.get("over_under", 0)))

        # ── ATS prediction (regression: predicted home margin) ──────────────────
        ats_margin = 0.0
        if ats_model is not None:
            feats, names = _extract_feature_vector(row, "ats")
            if feats is not None:
                dmat = xgb.DMatrix(feats, feature_names=names)
                ats_margin = float(ats_model.predict(dmat)[0])

        # ── OU prediction (regression: predicts total_points directly) ────────
        predicted_total = None
        if ou_model is not None:
            feats, names = _extract_feature_vector(row, "ou")
            if feats is not None:
                dmat = xgb.DMatrix(feats, feature_names=names)
                predicted_total = float(ou_model.predict(dmat)[0])

        # ── Actuals ─────────────────────────────────────────────────────────────
        home_score = _int_safe(row.get("home_score"))
        away_score = _int_safe(row.get("away_score"))
        actual_total = (home_score or 0) + (away_score or 0)
        actual_margin = (home_score or 0) - (away_score or 0)

        # ── Picks ───────────────────────────────────────────────────────────────
        pred_margin = ats_margin  # direct margin prediction (home_score - away_score)
        margin_conf = round(min(0.5 + abs(pred_margin + spread) * 0.04, 0.90), 4) if spread else 0.5

        spread_pick = None
        if spread is not None:
            # Both branches: margin + spread > 0  (home covers if home score beats the spread)
            if spread < 0:
                home_covers = pred_margin > abs(spread)
            else:
                home_covers = pred_margin > -spread
            spread_pick = home_str if home_covers else away_str

        ou_pick = None
        if predicted_total is not None and over_under is not None:
            ou_pick = "Over" if predicted_total > over_under else "Under"

        ml_pick = home_str if pred_margin > 0 else away_str

        # ── Results ─────────────────────────────────────────────────────────────
        ats_result = "N/A"
        if spread_pick and home_score is not None and away_score is not None and spread is not None:
            margin_vs_spread = home_score - away_score + spread
            if margin_vs_spread > 0:
                ats_result = "Win" if spread_pick == home_str else "Loss"
            elif margin_vs_spread == 0:
                ats_result = "Push"
            else:
                ats_result = "Win" if spread_pick != home_str else "Loss"
        ou_result = None
        if actual_total > over_under:
            ou_result = "Win" if ou_pick == "Over" else "Loss"
        elif actual_total < over_under:
            ou_result = "Win" if ou_pick == "Under" else "Loss"
        else:
            ou_result = "Push"

        ml_result = None
        if home_score > away_score:
            ml_result = "Win" if ml_pick == home_str else "Loss"
        elif away_score > home_score:
            ml_result = "Win" if ml_pick == away_str else "Loss"
        # else: tie → ml_result stays None (not counted as win or loss)

        # ── Odds from row ──────────────────────────────────────────────────────
        closing_ou = _float_safe(row.get("closing_ou", row.get("over_under", over_under)))
        home_ml = _float_safe(row.get("closing_home_ml"))
        away_ml = _float_safe(row.get("closing_away_ml"))
        spread_home_odds = _float_safe(row.get("closing_spread_home_odds"))
        spread_away_odds = _float_safe(row.get("closing_spread_away_odds"))
        over_odds = _float_safe(row.get("closing_over_odds"))
        under_odds = _float_safe(row.get("closing_under_odds"))

        # ── Map odds to pick side ──────────────────────────────────────────────
        sp_pick_str = spread_pick or ""
        home_abbr = home_str
        away_abbr = away_str

        ats_odds_value = None
        if sp_pick_str:
            pt = sp_pick_str.split(" ")[0]
            ats_odds_value = spread_home_odds if pt == home_abbr else spread_away_odds

        ou_odds_value = None
        if ou_pick == "Over":
            ou_odds_value = over_odds
        elif ou_pick == "Under":
            ou_odds_value = under_odds

        ml_odds_value = None
        if ml_pick == home_abbr:
            ml_odds_value = home_ml
        elif ml_pick == away_abbr:
            ml_odds_value = away_ml

        # ── Profit (per $100 stake) ──────────────────────────────────────────────
        ats_profit = None
        if ats_result == "Push":
            ats_profit = 0.0
        elif ats_result == "Win" and ats_odds_value:
            ats_profit = round(100.0 * _profit_per_100(float(ats_odds_value)), 2)
        elif ats_result == "Loss":
            ats_profit = -100.0

        ou_profit = None
        if ou_result == "Push":
            ou_profit = 0.0
        elif ou_result == "Win" and ou_odds_value:
            ou_profit = round(100.0 * _profit_per_100(float(ou_odds_value)), 2)
        elif ou_result == "Loss":
            ou_profit = -100.0

        ml_profit = None
        if ml_result == "Win" and ml_odds_value:
            ml_profit = round(100.0 * _profit_per_100(float(ml_odds_value)), 2)
        elif ml_result == "Loss":
            ml_profit = -100.0

        # ── Raw confidence computations ───
        ml_raw = round(min(0.5 + abs(pred_margin) * 0.025, 0.92), 4)
        ou_confidence_diff = abs(predicted_total - closing_ou) if predicted_total is not None and closing_ou else None
        ou_raw = round(min(0.5 + ou_confidence_diff * 0.07, 0.92), 4) if ou_confidence_diff is not None else 0.5

        # ── Calibrated confidences ───
        ats_cal = calibrate(margin_conf, "ats", "nfl")
        ml_cal = calibrate(ml_raw, "ml", "nfl")
        ou_cal = calibrate(ou_raw, "ou", "nfl")

        # ── Expected value ───
        def _ev(conf_: float, odds_: float) -> float:
            profit_if_win = 100.0 * _profit_per_100(odds_)
            return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

        ats_ev = _ev(ats_cal, ats_odds_value) if ats_odds_value else None
        ml_ev = _ev(ml_cal, ml_odds_value) if ml_odds_value else None
        ou_ev = _ev(ou_cal, ou_odds_value) if ou_odds_value else None

        # ── Handicapper info ────────────────────────────────────────────────────
        pc_feats = _load_pick_card_feature_metadata()
        home_stats = _enrich_dict_with_metadata(
            _build_nfl_home_stats(row),
            _NFL_HOME_STATS_FEATURE_MAP, pc_feats,
        )
        away_stats = _enrich_dict_with_metadata(
            _build_nfl_away_stats(row),
            _NFL_AWAY_STATS_FEATURE_MAP, pc_feats,
        )
        situational = _enrich_dict_with_metadata(
            _build_nfl_situational(row),
            _NFL_SITUATIONAL_FEATURE_MAP, pc_feats,
        )
        splits = _build_nfl_splits(row)

        # Features JSON — pick_card feature values enriched with display_name/description
        features_json_str = _extract_pick_card_features(row, pc_feats)

        # ── Save via ORM ────────────────────────────────────────────────────────
        if sync_sesh is not None:
            # Backtest called without db — use sync session from same DSN
            sync_sesh.execute(
                sa_delete(NFLGamePrediction).where(
                    NFLGamePrediction.game_id == game_id,
                    NFLGamePrediction.source == "backtest",
                )
            )
            # Compute predicted scores from model total + margin (not actual scores)
            if predicted_total is not None:
                _pred_h = int(round((predicted_total + pred_margin) / 2))
                _pred_a = int(round((predicted_total - pred_margin) / 2))
                if pred_margin < 0:
                    _pred_h, _pred_a = _pred_a, _pred_h
                _pred_home_score = max(0, _pred_h)
                _pred_away_score = max(0, _pred_a)
            else:
                _pred_home_score = None
                _pred_away_score = None
            rec = NFLGamePrediction(
                game_id=game_id,
                predicted_home_score=_pred_home_score,
                predicted_away_score=_pred_away_score,
                predicted_total=predicted_total,
                predicted_margin=pred_margin,
                margin_conf=margin_conf,
                ml_conf=ml_raw,
                ou_conf=ou_raw,
                ou_pick=ou_pick,
                spread_pick=spread_pick,
                ml_pick=ml_pick,
                ats_conf_cal=ats_cal,
                ml_conf_cal=ml_cal,
                ou_conf_cal=ou_cal,
                ats_ev=ats_ev,
                ml_ev=ml_ev,
                ou_ev=ou_ev,
                actual_home_score=home_score,
                actual_away_score=away_score,
                actual_total=actual_total,
                actual_margin=actual_margin,
                ats_result=ats_result,
                ou_result=ou_result,
                ml_result=ml_result,
                ats_odds=round(ats_odds_value) if ats_odds_value else None,
                ou_odds=round(ou_odds_value) if ou_odds_value else None,
                ml_odds=round(ml_odds_value) if ml_odds_value else None,
                ats_profit=ats_profit,
                ou_profit=ou_profit,
                ml_profit=ml_profit,
                home_stats_json=json.dumps(home_stats) if home_stats else None,
                away_stats_json=json.dumps(away_stats) if away_stats else None,
                situational_json=json.dumps(situational) if situational else None,
                splits_json=json.dumps(splits) if splits else None,
                features_json=features_json_str,
                source="backtest",
                created_at=datetime.now(timezone.utc),
            )
            sync_sesh.add(rec)
            sync_sesh.commit()
        else:
            # With async session (used from backtest_season(db=...))
            await db.execute(
                sa_delete(NFLGamePrediction).where(
                    NFLGamePrediction.game_id == game_id,
                    NFLGamePrediction.source == "backtest",
                )
            )
            # Compute predicted scores from model total + margin (not actual scores)
            if predicted_total is not None:
                _pred_h = int(round((predicted_total + pred_margin) / 2))
                _pred_a = int(round((predicted_total - pred_margin) / 2))
                if pred_margin < 0:
                    _pred_h, _pred_a = _pred_a, _pred_h
                _pred_home_score = max(0, _pred_h)
                _pred_away_score = max(0, _pred_a)
            else:
                _pred_home_score = None
                _pred_away_score = None
            rec = NFLGamePrediction(
                game_id=game_id,
                predicted_home_score=_pred_home_score,
                predicted_away_score=_pred_away_score,
                predicted_total=predicted_total,
                predicted_margin=pred_margin,
                margin_conf=margin_conf,
                ml_conf=ml_raw,
                ou_conf=ou_raw,
                ou_pick=ou_pick,
                spread_pick=spread_pick,
                ml_pick=ml_pick,
                ats_conf_cal=ats_cal,
                ml_conf_cal=ml_cal,
                ou_conf_cal=ou_cal,
                ats_ev=ats_ev,
                ml_ev=ml_ev,
                ou_ev=ou_ev,
                actual_home_score=home_score,
                actual_away_score=away_score,
                actual_total=actual_total,
                actual_margin=actual_margin,
                ats_result=ats_result,
                ou_result=ou_result,
                ml_result=ml_result,
                ats_odds=round(ats_odds_value) if ats_odds_value else None,
                ou_odds=round(ou_odds_value) if ou_odds_value else None,
                ml_odds=round(ml_odds_value) if ml_odds_value else None,
                ats_profit=ats_profit,
                ou_profit=ou_profit,
                ml_profit=ml_profit,
                home_stats_json=json.dumps(home_stats) if home_stats else None,
                away_stats_json=json.dumps(away_stats) if away_stats else None,
                situational_json=json.dumps(situational) if situational else None,
                splits_json=json.dumps(splits) if splits else None,
                features_json=features_json_str,
                source="backtest",
                created_at=datetime.now(timezone.utc),
            )
            db.add(rec)
            await db.commit()

    except Exception:
        logger.exception("_save_backtest_prediction failed for game %s", game_id)
    finally:
        if engine:
            engine.dispose()
def _build_nfl_home_stats(row: pd.Series) -> Dict[str, Any]:
    """Build home team stats summary from a feature vector row."""
    return {
        "abbreviation": str(row.get("home_abbr", "")),
        "points_for": _float_safe(row.get("hpf")),
        "points_against": _float_safe(row.get("hpa")),
        "win_pct_r5": _float_safe(row.get("home_win_pct_r5")),
        "margin_r5": _float_safe(row.get("home_margin_r3")),
        "margin_r10": _float_safe(row.get("home_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("home_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("home_season_ats_pct")),
        "embarrassed": bool(row.get("home_embarrassed", 0)),
        "rest_days": _float_safe(row.get("home_rest_days", 7)),
    }


def _build_nfl_away_stats(row: pd.Series) -> Dict[str, Any]:
    """Build away team stats summary."""
    return {
        "abbreviation": str(row.get("away_abbr", "")),
        "points_for": _float_safe(row.get("apf")),
        "points_against": _float_safe(row.get("apa")),
        "win_pct_r5": _float_safe(row.get("away_win_pct_r5")),
        "margin_r5": _float_safe(row.get("away_margin_r3")),
        "margin_r10": _float_safe(row.get("away_margin_r10")),
        "cover_pct_r5": _float_safe(row.get("away_cover_pct_r5")),
        "season_ats_pct": _float_safe(row.get("away_season_ats_pct")),
        "embarrassed": bool(row.get("away_embarrassed", 0)),
        "rest_days": _float_safe(row.get("away_rest_days", 7)),
    }


def _build_nfl_situational(row: pd.Series) -> Dict[str, Any]:
    """Build situational data summary."""
    return {
        "travel_miles": _float_safe(row.get("travel_miles")),
        "is_dome": bool(row.get("is_dome", 0)),
        "rest_diff": _int_safe(row.get("rest_diff")),
        "tz_diff": _int_safe(row.get("tz_diff")),
        "is_short_week": bool(row.get("is_short", 0)),
        "temp": _float_safe(row.get("temp")),
        "wind": _float_safe(row.get("wind")),
        "venue": str(row.get("venue", "")),
        "surface": str(row.get("surface", "")),
        "roof_type": str(row.get("roof_type", "")),
    }


def _build_nfl_splits(row: pd.Series) -> Dict[str, Any]:
    """Build splits / betting trends summary."""
    return {
        "spread": _float_safe(row.get("closing_spread")),
        "opening_spread": _float_safe(row.get("opening_spread")),
        "spread_movement": _float_safe(row.get("spread_movement")),
        "closing_ou": _float_safe(row.get("closing_ou")),
        "opening_ou": _float_safe(row.get("opening_ou")),
        "ou_movement": _float_safe(row.get("ou_movement")),
        "sp_h_odds_mvmt": _float_safe(row.get("sp_h_odds_mvmt")),
        "sp_a_odds_mvmt": _float_safe(row.get("sp_a_odds_mvmt")),
        "home_implied": _float_safe(row.get("himp")),
        "away_implied": _float_safe(row.get("aimp")),
        "diff_implied": _float_safe(row.get("dimp")),
        "season_avg_pts": _float_safe(row.get("season_avg_pts")),
    }


# ── Safe casting helpers ─────────────────────────────────────────────────────────


def _int_safe(val: Any) -> Optional[int]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _float_safe(val: Any) -> Optional[float]:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        return round(float(val), 2)
    except (ValueError, TypeError):
        return None


def _str_safe(val: Any) -> str:
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return ""
    return str(val)


# ── CLI / smoke test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "backtest"

    if mode == "backtest":
        results = asyncio.run(backtest_season())
        print("\n=== NFL Backtest ===")
        if "ats_results" in results:
            print(f"ATS: {len(results['ats_results'])} years")
            for r in results["ats_results"]:
                if "error" in r:
                    print(f"  {r['year']}: ERROR — {r['error']}")
                else:
                    auc_str = f"auc={r['auc']:.4f}" if r.get('auc') is not None else "auc=N/A"
                    print(f"  {r['year']}: acc={r['accuracy']:.4f} {auc_str} n={r['n_train']}+{r['n_test']}")
        if "ou_results" in results:
            print(f"OU: {len(results['ou_results'])} years")
            for r in results["ou_results"]:
                if "error" in r:
                    print(f"  {r['year']}: ERROR — {r['error']}")
                else:
                    print(f"  {r['year']}: MAE={r['mae']:.2f} RMSE={r['rmse']:.2f} n={r['n_train']}+{r['n_test']}")

    elif mode == "handicap":
        if len(sys.argv) < 3:
            print("Usage: python engine.py handicap <game_id>")
            sys.exit(1)
        game_id = int(sys.argv[2])
        result = asyncio.run(batch_predict_upcoming_games([game_id]))
        print(json.dumps(result, indent=2, default=str) if result else "No prediction")

    else:
        print(f"Unknown mode: {mode}")
        print("Usage: python engine.py [backtest|handicap]")
        sys.exit(1)
