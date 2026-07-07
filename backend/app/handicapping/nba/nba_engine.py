"""
NBA handicapping engine — load models, predict games, build pick cards,
run backtests, and save results to the database.

Mirrors ``nfl/engine.py`` but adapted for NBA (point-spread betting,
basketball stats instead of football).
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
import psycopg2.extras
import xgboost as xgb
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sklearn.metrics import roc_auc_score, mean_absolute_error, mean_squared_error

from app.handicapping.nba.data_loader import NBADataLoader, get_data_loader, get_model_features

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────────
MODELS_DIR = Path.home() / ".openclaw" / "workspace" / "earl-knows-football" / "data" / "models" / "nba"
MODELS_DIR.mkdir(parents=True, exist_ok=True)

_now = datetime.now()
CURRENT_SEASON = _now.year if _now.month >= 10 else _now.year - 1
DB_DSN: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)
NBA_SCHEMA = "nba"

# ── Async DB setup ───────────────────────────────────────────────────────────────
ASYNC_DSN: str = DB_DSN.replace("postgresql://", "postgresql+asyncpg://")
_async_engine = None
_async_sessionmaker = None


# ── Pick-card feature extraction ────────────────────────────────────────────────


def _extract_pick_card_features(row, feature_names: set) -> str:
    """Return JSON string of pick_card feature values from a DataFrame row.
    Ensures NaN/Inf floats are converted to None to keep stored JSON valid.
    """

    def _sanitize(v):
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v

    features = {
        name: _sanitize(row.get(name))
        for name in feature_names
        if name in row.index or name in row
    }
    return json.dumps(features, default=str)


def _get_async_session() -> async_sessionmaker:
    global _async_engine, _async_sessionmaker
    if _async_sessionmaker is None:
        _async_engine = create_async_engine(ASYNC_DSN, echo=False, pool_pre_ping=True)
        _async_sessionmaker = async_sessionmaker(_async_engine, expire_on_commit=False)
    return _async_sessionmaker


# ── Sync DB helpers ──────────────────────────────────────────────────────────────


def _get_sync_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(DB_DSN)


# ── Per-year model loaders (from pickle files) ────────────────────────────────


def _resolve_year_pkl_paths(model_type: str) -> Dict[int, Path]:
    """Return {year: Path} for all per-year model pickle files.

    ``model_type`` is ``'ats'`` or ``'ou'``.

    Loads the pkl_filename list from the current live training run
    (``nba.training_runs WHERE is_live = 't'``), parses
    ``<uuid>-<year>.pkl`` entries, and resolves them under ``MODELS_DIR``.

    Returns an empty dict when no live run exists or no pkl files found.
    """
    from app.handicapping.db_training import get_live_training_run
    run = get_live_training_run("nba", model_type)
    if run is None:
        logger.warning("  No live training_run for nba/%s", model_type)
        return {}

    raw = run.get("pkl_filename", "")
    if not raw:
        logger.warning("  training_run for nba/%s has empty pkl_filename", model_type)
        return {}

    parts = [s.strip() for s in raw.split(",") if s.strip()]
    out: Dict[int, Path] = {}
    for fname in parts:
        stem = fname.rsplit(".", 1)[0]
        if "-" in stem:
            year_str = stem.rsplit("-", 1)[-1]
            try:
                year = int(year_str)
            except ValueError:
                continue
            out[year] = MODELS_DIR / fname
    return out


def _load_model_for_year(year: int, model_type: str) -> Optional[xgb.Booster]:
    """Load a per-year XGBoost model from pickle."""
    paths = _resolve_year_pkl_paths(model_type)
    p = paths.get(year)
    if p is None or not p.exists():
        logger.warning("  No %s model found for %s (checked %s)", model_type, year, p)
        return None
    try:
        with open(p, "rb") as fh:
            model = pickle.load(fh)
        logger.info("  Loaded %s model for %s: %s", model_type, year, p.name)
        return model
    except Exception as exc:
        logger.error("  Failed to load %s model for %s: %s", model_type, year, exc)
        return None


def _get_models_for_season(year: int) -> Dict[str, Optional[xgb.Booster]]:
    """Load both ATS and OU models for a given year.

    Returns ``{"ats": model_or_None, "ou": model_or_None}``.
    """
    return {
        "ats": _load_model_for_year(year, "ats"),
        "ou": _load_model_for_year(year, "ou"),
    }


# ── Year evaluation ──────────────────────────────────────────────────────────────


def _evaluate_year_model(year_df: pd.DataFrame, model: xgb.Booster, model_type: str) -> Dict[str, Any]:
    """Evaluate a single per-year model on a full season's games.

    Returns dict with accuracy (ATS) or MAE/RMSE (OU), AUC, and game-level
    predictions (list of dicts).  Matches the NFL ``backtest_season`` pattern.
    """
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
            actual_cover = int((home_score - away_score + spread) > 0)
            labels.append(actual_cover)
            total += 1
            if int(prob > 0.5) == actual_cover:
                correct += 1
        else:
            ou_line = row.get("over_under", 0) or 0
            total_score = (row.get("home_score", 0) or 0) + (row.get("away_score", 0) or 0)
            actual_over = int(total_score > ou_line)
            labels.append(actual_over)
            total += 1
            if int(prob > 0.5) == actual_over:
                correct += 1

    accuracy = correct / total if total > 0 else 0.0
    auc = roc_auc_score(labels, probs) if len(set(labels)) > 1 and total > 1 else None

    return {
        "model_type": model_type,
        "total_games": total,
        "correct": correct,
        "accuracy": round(accuracy, 4),
        "auc": round(auc, 4) if auc is not None else None,
    }


_FEATURES_CACHE_ATS: Optional[List[str]] = None
_FEATURES_CACHE_OU: Optional[List[str]] = None


def _get_features(model_type: str) -> List[str]:
    """Return feature names from the live training run."""
    global _FEATURES_CACHE_ATS, _FEATURES_CACHE_OU
    if model_type == "ats" and _FEATURES_CACHE_ATS is not None:
        return _FEATURES_CACHE_ATS
    if model_type == "ou" and _FEATURES_CACHE_OU is not None:
        return _FEATURES_CACHE_OU
    conn = _get_sync_conn()
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


# ═══════════════════════════════════════════════════════════════════════════════════
# Handicapper module-level helpers (extracted from NBAHandicapper class)
# ═══════════════════════════════════════════════════════════════════════════════════


def _confidence_from_prob(prob: float) -> Tuple[str, float]:
    """Classify probability into confidence tier.

    Returns (label, raw_probability).
    """
    if prob >= 0.85:
        return ("lock", prob)
    elif prob >= 0.75:
        return ("strong", prob)
    elif prob >= 0.65:
        return ("moderate", prob)
    elif prob >= 0.55:
        return ("slight", prob)
    else:
        return ("push", prob)


def _prob_to_moneyline(prob: float) -> int:
    """Convert a win probability (0‑1) to American moneyline odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return -int(round(100 * prob / (1 - prob)))
    else:
        return int(round(100 * (1 - prob) / prob))


def _ml_prob_from_spread(home_prob: float, spread: float) -> float:
    """Estimate moneyline probability given a spread probability.

    MLB-style adjustment: if the spread is within ±3, the ML prob
    is slightly closer to 0.5; otherwise it leans toward the spread prob.
    """
    if abs(spread) <= 3:
        return home_prob * 0.6 + 0.2
    else:
        return home_prob * 0.8 + 0.1


# ── Pick card builder ────────────────────────────────────────────────────────────


async def _build_pick_card(
    row: pd.Series,
    ats_model: xgb.Booster,
    ou_model: xgb.Booster,
    year: int,
    game_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a full pick-card dict for a single NBA game.

    Adapted from NBAHandicapper.handicap_game's inline logic.
    Returns same dict structure as the old class method ``_build_pick_card``.
    """
    home_team = str(row.get("home_team", ""))
    away_team = str(row.get("away_team", ""))
    spread = float(row.get("spread", 0) or 0)
    ou_line = float(row.get("over_under", 0) or 0)

    # ATS prediction
    ats_vals, ats_names = _extract_feature_vector(row, "ats")
    ats_dmat = xgb.DMatrix(ats_vals, feature_names=ats_names)
    implied_cover = float(ats_model.predict(ats_dmat)[0])

    # OU prediction
    ou_vals, ou_names = _extract_feature_vector(row, "ou")
    ou_dmat = xgb.DMatrix(ou_vals, feature_names=ou_names)
    ou_prob = float(ou_model.predict(ou_dmat)[0])

    # Confidence
    confidence, raw_prob = _confidence_from_prob(implied_cover)
    ou_confidence, ou_raw = _confidence_from_prob(ou_prob)

    # Moneyline conversion
    # Pick the side (home cover / away cover)
    if home_team and away_team:
        ml_prob = _ml_prob_from_spread(implied_cover, spread)
        ml_numeric = _prob_to_moneyline(ml_prob)
        favorite = home_team if spread < 0 else away_team
        underdog = away_team if spread < 0 else home_team
        ml_prob_away = 1 - ml_prob
        ml_numeric_away = _prob_to_moneyline(ml_prob_away)
    else:
        ml_numeric = 0
        ml_numeric_away = 0

    home_prob = implied_cover
    away_prob = 1 - implied_cover

    # Build the ATS pick
    if implied_cover > 0.5:
        ats_side = home_team
        ats_spread = f"-{abs(spread):.1f}"
        ats_text = f"{home_team} {ats_spread}"
        cover_conf = _confidence_from_prob(implied_cover)
    else:
        ats_side = away_team
        ats_spread = f"+{abs(spread):.1f}"
        ats_text = f"{away_team} {ats_spread}"
        cover_conf = _confidence_from_prob(away_prob)

    # Build the OU pick
    if ou_prob > 0.5:
        ou_side = "Over"
        ou_text = f"Over {ou_line:.1f}"
    else:
        ou_side = "Under"
        ou_text = f"Under {ou_line:.1f}"

    pick_card: Dict[str, Any] = {
        "game_id": game_id or int(row.get("game_id", 0)),
        "home_team": home_team,
        "away_team": away_team,
        "spread": spread,
        "over_under": ou_line,
        "predicted_spread": spread,                         # placeholder – no pure spread model
        "predicted_over_under": ou_line,                     # placeholder
        "home_ats_prob": round(home_prob, 4),
        "away_ats_prob": round(away_prob, 4),
        "ou_over_prob": round(ou_prob, 4),
        "ou_under_prob": round(1 - ou_prob, 4),
        "ats_pick": {
            "team": ats_side,
            "spread": ats_spread,
            "text": ats_text,
            "probability": round(max(home_prob, away_prob), 4),
            "confidence": cover_conf,
        },
        "ou_pick": {
            "team": ou_side,
            "text": ou_text,
            "probability": round(max(ou_prob, 1 - ou_prob), 4),
            "confidence": ou_confidence,
        },
        "ml_pick": {
            "favorite": favorite if home_team else "",
            "underdog": underdog if away_team else "",
            "home_ml": ml_numeric,
            "away_ml": ml_numeric_away,
            "home_ml_prob": round(ml_prob, 4) if home_team else 0.5,
            "away_ml_prob": round(ml_prob_away, 4) if away_team else 0.5,
        },
        "model_version": f"nba-xgboost-{year}",
        "model_type": "xgboost",
        "sport": "nba",
        "year": year,
    }

    return pick_card


# ═══════════════════════════════════════════════════════════════════════════════════
# Backtest & batch-prediction functions (mirror nfl/engine.py)
# ═══════════════════════════════════════════════════════════════════════════════════


async def backtest_season(
    years: Optional[List[int]] = None,
    limit: Optional[int] = None,
    save_results: bool = True,
    db: Optional[async_sessionmaker] = None,
) -> Dict[str, Any]:
    """Backtest NBA models across one or more seasons.

    Mirrors ``nfl/engine.py:backtest_season``.

    Parameters
    ----------
    years : list of int, optional
        Season years to backtest (default: current NBA season).
    limit : int, optional
        Max games to load from the data loader (for quick tests).
    save_results : bool
        Persist predictions to ``nba.game_predictions``.
    db : async_sessionmaker, optional
        DB session factory (auto-created if not provided).

    Returns
    -------
    dict
        ``{"ats": [...], "ou": [...]}`` with per-year evaluation results.
    """
    if years is None:
        years = [CURRENT_SEASON]

    dl = get_data_loader()
    df = dl.load_data(limit=limit)
    logger.info("Loaded %d NBA games from data loader%s", len(df), f" (limit={limit})" if limit else "")

    ats_results: List[Dict[str, Any]] = []
    ou_results: List[Dict[str, Any]] = []
    total_game_preds = 0

    for year in years:
        ats_model = _load_model_for_year(year, "ats")
        ou_model = _load_model_for_year(year, "ou")

        if ats_model is None and ou_model is None:
            logger.warning("  No models for year %s – skipping", year)
            continue

        year_df = df[df["season"] == year].copy()
        if year_df.empty:
            logger.warning("  No data for season %s – skipping", year)
            continue

        logger.info("  Backtesting %d games for season %s", len(year_df), year)

        if ats_model:
            ats_res = _evaluate_year_model(year_df, ats_model, "ats")
            ats_results.append({"year": year, **ats_res})
            logger.info("    ATS: %d/%d (%.1f%%)",
                        ats_res["correct"], ats_res["total_games"],
                        ats_res["accuracy"] * 100)

        if ou_model:
            ou_res = _evaluate_year_model(year_df, ou_model, "ou")
            ou_results.append({"year": year, **ou_res})
            logger.info("    OU:  %d/%d (%.1f%%)",
                        ou_res["correct"], ou_res["total_games"],
                        ou_res["accuracy"] * 100)

        # Optionally save per-game predictions
        if save_results and (ats_model or ou_model):
            for idx, row in year_df.iterrows():
                _save_backtest_prediction(row, ats_model, ou_model)
                total_game_preds += 1

    logger.info("Backtest complete: %d years, %d game predictions saved",
                len(years), total_game_preds)
    return {"ats": ats_results, "ou": ou_results}


async def batch_predict_upcoming_games(
    days_ahead: int = 7,
    save_to_db: bool = True,
    db: Optional[async_sessionmaker] = None,
) -> List[Dict[str, Any]]:
    """Predict upcoming NBA games and optionally save to the database.

    Mirrors ``nfl/engine.py:batch_predict_upcoming_games``.

    Parameters
    ----------
    days_ahead : int
        How many days into the future to look (default 7).
    save_to_db : bool
        Persist predictions to ``nba.game_predictions``.
    db : async_sessionmaker, optional
        DB session factory (auto-created if not provided).

    Returns
    -------
    list of dict
        Pick-card dicts for each upcoming game.
    """
    from app.handicapping.nba.data_loader import NBADataLoader

    db = db or _get_async_session()
    now = datetime.now(timezone.utc)
    cutoff = now.isoformat()

    logger.info("Fetching upcoming NBA games (next %d days)...", days_ahead)

    # Load upcoming games from the DB
    upcoming = await _fetch_upcoming_games(db, now, days_ahead)
    if not upcoming:
        logger.info("  No upcoming NBA games found in the next %d days", days_ahead)
        return []

    # Determine season year from the first game's date
    first_date = upcoming[0].get("game_date") or upcoming[0].get("date", "")
    year = _season_from_date(first_date) if first_date else CURRENT_SEASON

    # Load models for this season
    ats_model = _load_model_for_year(year, "ats")
    ou_model = _load_model_for_year(year, "ou")
    if ats_model is None or ou_model is None:
        logger.warning("  Missing models for season %s – cannot predict", year)
        return []

    # Convert upcoming games to DataFrame for feature extraction
    df = pd.DataFrame(upcoming)
    if "game_id" not in df.columns:
        logger.warning("  Upcoming games have no game_id column")
        return []

    # We need pre-computed matches with spread & over_under from the
    # betting_lines table.  Merge if needed.
    df = await _enrich_with_betting_lines(db, df, now)

    pick_cards: List[Dict[str, Any]] = []
    for idx, row in df.iterrows():
        try:
            pick_card = await _build_pick_card(row, ats_model, ou_model, year,
                                                game_id=int(row["game_id"]))
            pick_cards.append(pick_card)

            if save_to_db:
                await _save_api_prediction(row, pick_card, db=db)
        except Exception as exc:
            logger.error("  Failed to predict game %s: %s", row.get("game_id"), exc)
            continue

    logger.info("  Predicted %d / %d upcoming NBA games", len(pick_cards), len(df))
    return pick_cards


def _season_from_date(date_str: str) -> int:
    """Return the NBA season year for a date string.

    NBA seasons start in October, so a date in Oct-Dec belongs to
    the *next* calendar year's season (e.g. Oct 2024 → season 2025).
    """
    try:
        dt = datetime.fromisoformat(date_str)
        return dt.year if dt.month >= 10 else dt.year - 1  # noqa: SIM114
    except (ValueError, TypeError):
        try:
            dt = pd.Timestamp(date_str)
            return dt.year if dt.month >= 10 else dt.year - 1
        except Exception:
            return CURRENT_SEASON


async def _fetch_upcoming_games(
    db: async_sessionmaker,
    now: datetime,
    days_ahead: int,
) -> List[Dict[str, Any]]:
    """Fetch upcoming NBA games from the database."""
    async with db() as session:
        stmt = text("""
            SELECT
                g.id AS game_id,
                g.home_team,
                g.away_team,
                g.game_date,
                g.season
            FROM nba.games g
            WHERE g.game_date >= :now
              AND g.game_date < :cutoff
              AND g.status = 'scheduled'
            ORDER BY g.game_date ASC
        """)
        result = await session.execute(stmt, {
            "now": now,
            "cutoff": now.replace(hour=23, minute=59, second=59) + pd.Timedelta(days=days_ahead - 1),
        })
        rows = result.mappings().all()
        return [dict(r) for r in rows]


async def _enrich_with_betting_lines(
    db: async_sessionmaker,
    df: pd.DataFrame,
    now: datetime,
) -> pd.DataFrame:
    """Add latest spread & over_under from nba.betting_lines to the DataFrame.

    For games that already have spread/ou lines, fall back to those.
    """
    if df.empty:
        return df

    game_ids = [int(gid) for gid in df["game_id"].tolist() if gid]
    if not game_ids:
        return df

    async with db() as session:
        stmt = text("""
            SELECT DISTINCT ON (bl.game_id)
                bl.game_id,
                bl.spread,
                bl.over_under
            FROM nba.betting_lines bl
            WHERE bl.game_id = ANY(:game_ids)
            ORDER BY bl.game_id, bl.updated_at DESC
        """)
        result = await session.execute(stmt, {"game_ids": game_ids})
        lines = {r["game_id"]: r for r in result.mappings().all()}

    df["spread"] = df["game_id"].apply(lambda gid: lines.get(int(gid), {}).get("spread", 0.0))
    df["over_under"] = df["game_id"].apply(lambda gid: lines.get(int(gid), {}).get("over_under", 0.0))

    return df


# ── Save API prediction ──────────────────────────────────────────────────────────


async def _save_api_prediction(
    row: pd.Series,
    pick_card: Dict[str, Any],
    db: Optional[async_sessionmaker] = None,
    conn: Optional[psycopg2.extensions.connection] = None,
) -> None:
    """Save a predicted pick card to ``nba.game_predictions`` (async).

    ``row`` is the game row from the data loader (or DB query).
    ``pick_card`` is the dict returned by ``_build_pick_card``.

    Uses async DB session when ``db`` is provided, otherwise falls
    back to synchronous ``conn``.
    """
    game_id = pick_card.get("game_id") or int(row.get("game_id", 0))
    home_team = pick_card.get("home_team") or str(row.get("home_team", ""))
    away_team = pick_card.get("away_team") or str(row.get("away_team", ""))

    ats_pick = pick_card.get("ats_pick", {})
    ou_pick = pick_card.get("ou_pick", {})
    ml_pick = pick_card.get("ml_pick", {})

    home_ats_prob = pick_card.get("home_ats_prob", 0.5)
    away_ats_prob = pick_card.get("away_ats_prob", 0.5)
    ou_over_prob = pick_card.get("ou_over_prob", 0.5)
    ou_under_prob = pick_card.get("ou_under_prob", 0.5)

    ats_side = ats_pick.get("team", "")
    ats_spread = ats_pick.get("spread", "PK")
    ats_text = ats_pick.get("text", "")
    ats_prob = ats_pick.get("probability", 0.5)
    ats_conf_label = ""
    ats_conf_value = 0.0
    if ats_pick.get("confidence"):
        ats_conf_label, ats_conf_value = ats_pick["confidence"]

    ou_side = ou_pick.get("team", "")
    ou_text = ou_pick.get("text", "")
    ou_prob = ou_pick.get("probability", 0.5)
    ou_conf_label = ""
    ou_conf_value = 0.0
    if ou_pick.get("confidence"):
        ou_conf_label, ou_conf_value = ou_pick["confidence"]

    ml_favorite = ml_pick.get("favorite", "")
    ml_underdog = ml_pick.get("underdog", "")
    home_ml = ml_pick.get("home_ml", 0)
    away_ml = ml_pick.get("away_ml", 0)
    home_ml_prob = ml_pick.get("home_ml_prob", 0.5)
    away_ml_prob = ml_pick.get("away_ml_prob", 0.5)

    model_version = pick_card.get("model_version", "nba-xgboost-current")
    model_type = pick_card.get("model_type", "xgboost")

    now_ts = datetime.now(timezone.utc)

    # ── feature set used for prediction
    feature_set_ats = _get_features("ats")
    feature_set_ou = _get_features("ou")

    features_ats_json = _extract_pick_card_features(row, set(feature_set_ats))
    features_ou_json = _extract_pick_card_features(row, set(feature_set_ou))

    upsert_sql = text("""
        INSERT INTO nba.game_predictions (
            game_id, home_team, away_team,
            predicted_spread, predicted_over_under,
            home_ats_prob, away_ats_prob,
            ou_over_prob, ou_under_prob,
            ats_side, ats_spread, ats_text, ats_probability, ats_confidence_label, ats_confidence_value,
            ou_side, ou_text, ou_probability, ou_confidence_label, ou_confidence_value,
            ml_favorite, ml_underdog, home_ml, away_ml, home_ml_prob, away_ml_prob,
            model_version, model_type, features_ats_json, features_ou_json,
            home_stats_json, away_stats_json, situational_json, splits_json,
            created_at, updated_at
        ) VALUES (
            :game_id, :home_team, :away_team,
            :predicted_spread, :predicted_over_under,
            :home_ats_prob, :away_ats_prob,
            :ou_over_prob, :ou_under_prob,
            :ats_side, :ats_spread, :ats_text, :ats_probability, :ats_confidence_label, :ats_confidence_value,
            :ou_side, :ou_text, :ou_probability, :ou_confidence_label, :ou_confidence_value,
            :ml_favorite, :ml_underdog, :home_ml, :away_ml, :home_ml_prob, :away_ml_prob,
            :model_version, :model_type, :features_ats_json, :features_ou_json,
            :home_stats_json, :away_stats_json, :situational_json, :splits_json,
            :created_at, :updated_at
        )
        ON CONFLICT (game_id) DO UPDATE SET
            home_ats_prob = EXCLUDED.home_ats_prob,
            away_ats_prob = EXCLUDED.away_ats_prob,
            ou_over_prob = EXCLUDED.ou_over_prob,
            ou_under_prob = EXCLUDED.ou_under_prob,
            ats_side = EXCLUDED.ats_side,
            ats_spread = EXCLUDED.ats_spread,
            ats_text = EXCLUDED.ats_text,
            ats_probability = EXCLUDED.ats_probability,
            ats_confidence_label = EXCLUDED.ats_confidence_label,
            ats_confidence_value = EXCLUDED.ats_confidence_value,
            ou_side = EXCLUDED.ou_side,
            ou_text = EXCLUDED.ou_text,
            ou_probability = EXCLUDED.ou_probability,
            ou_confidence_label = EXCLUDED.ou_confidence_label,
            ou_confidence_value = EXCLUDED.ou_confidence_value,
            ml_favorite = EXCLUDED.ml_favorite,
            ml_underdog = EXCLUDED.ml_underdog,
            home_ml = EXCLUDED.home_ml,
            away_ml = EXCLUDED.away_ml,
            home_ml_prob = EXCLUDED.home_ml_prob,
            away_ml_prob = EXCLUDED.away_ml_prob,
            model_version = EXCLUDED.model_version,
            features_ats_json = EXCLUDED.features_ats_json,
            features_ou_json = EXCLUDED.features_ou_json,
            updated_at = EXCLUDED.updated_at
    """)

    if db:
        async with db() as session:
            await session.execute(upsert_sql, {
                "game_id": game_id,
                "home_team": home_team,
                "away_team": away_team,
                "predicted_spread": pick_card.get("spread", 0),
                "predicted_over_under": pick_card.get("over_under", 0),
                "home_ats_prob": home_ats_prob,
                "away_ats_prob": away_ats_prob,
                "ou_over_prob": ou_over_prob,
                "ou_under_prob": ou_under_prob,
                "ats_side": ats_side,
                "ats_spread": ats_spread,
                "ats_text": ats_text,
                "ats_probability": ats_prob,
                "ats_confidence_label": ats_conf_label,
                "ats_confidence_value": ats_conf_value,
                "ou_side": ou_side,
                "ou_text": ou_text,
                "ou_probability": ou_prob,
                "ou_confidence_label": ou_conf_label,
                "ou_confidence_value": ou_conf_value,
                "ml_favorite": ml_favorite,
                "ml_underdog": ml_underdog,
                "home_ml": home_ml,
                "away_ml": away_ml,
                "home_ml_prob": home_ml_prob,
                "away_ml_prob": away_ml_prob,
                "model_version": model_version,
                "model_type": model_type,
                "features_ats_json": features_ats_json,
                "features_ou_json": features_ou_json,
                "home_stats_json": pick_card.get("home_stats_json") or json.dumps({}),
                "away_stats_json": pick_card.get("away_stats_json") or json.dumps({}),
                "situational_json": pick_card.get("situational_json") or json.dumps({}),
                "splits_json": pick_card.get("splits_json") or json.dumps({}),
                "created_at": now_ts,
                "updated_at": now_ts,
            })
            await session.commit()
        logger.debug("  Saved API prediction for game %s (%s vs %s)",
                      game_id, home_team, away_team)
    elif conn:
        with conn.cursor() as cur:
            cur.execute(
                upsert_sql,
                {
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "predicted_spread": pick_card.get("spread", 0),
                    "predicted_over_under": pick_card.get("over_under", 0),
                    "home_ats_prob": home_ats_prob,
                    "away_ats_prob": away_ats_prob,
                    "ou_over_prob": ou_over_prob,
                    "ou_under_prob": ou_under_prob,
                    "ats_side": ats_side,
                    "ats_spread": ats_spread,
                    "ats_text": ats_text,
                    "ats_probability": ats_prob,
                    "ats_confidence_label": ats_conf_label,
                    "ats_confidence_value": ats_conf_value,
                    "ou_side": ou_side,
                    "ou_text": ou_text,
                    "ou_probability": ou_prob,
                    "ou_confidence_label": ou_conf_label,
                    "ou_confidence_value": ou_conf_value,
                    "ml_favorite": ml_favorite,
                    "ml_underdog": ml_underdog,
                    "home_ml": home_ml,
                    "away_ml": away_ml,
                    "home_ml_prob": home_ml_prob,
                    "away_ml_prob": away_ml_prob,
                    "model_version": model_version,
                    "model_type": model_type,
                    "features_ats_json": features_ats_json,
                    "features_ou_json": features_ou_json,
                    "home_stats_json": pick_card.get("home_stats_json") or json.dumps({}),
                    "away_stats_json": pick_card.get("away_stats_json") or json.dumps({}),
                    "situational_json": pick_card.get("situational_json") or json.dumps({}),
                    "splits_json": pick_card.get("splits_json") or json.dumps({}),
                    "created_at": now_ts,
                    "updated_at": now_ts,
                },
            )
        conn.commit()
        logger.debug("  Saved API prediction (sync) for game %s (%s vs %s)",
                      game_id, home_team, away_team)


# ── Save backtest prediction ──────────────────────────────────────────────────────


def _save_backtest_prediction(
    row: pd.Series,
    ats_model: Optional[xgb.Booster] = None,
    ou_model: Optional[xgb.Booster] = None,
    ats_features: Optional[List[str]] = None,
    ou_features: Optional[List[str]] = None,
) -> None:
    """Save a backtest prediction row to ``nba.game_predictions`` (sync).

    Extracts feature vectors from the row, runs ATS and OU models,
    generates picks, and persists everything including actual results
    for later analysis.
    """
    game_id = _int_safe(row.get("id")) or _int_safe(row.get("game_id"))
    home_team = _str_safe(row.get("home_team"))
    away_team = _str_safe(row.get("away_team"))
    season = _int_safe(row.get("season")) or CURRENT_SEASON
    spread = _float_safe(row.get("spread")) or 0.0
    ou_line = _float_safe(row.get("over_under")) or 0.0
    home_score = _float_safe(row.get("home_score")) or 0.0
    away_score = _float_safe(row.get("away_score")) or 0.0
    actual_total = home_score + away_score
    actual_margin = home_score - away_score

    ats_feat = ats_features or (_get_features("ats") if ats_features is None else ats_features)
    ou_feat = ou_features or (_get_features("ou") if ou_features is None else ou_features)

    # Determine actual cover / over
    actual_cover = (actual_margin + spread) > 0
    actual_over = actual_total > ou_line

    # ATS prediction
    home_ats_prob = 0.5
    ats_side = ""
    ats_spread_str = ""
    ats_text = ""
    ats_probability = 0.5
    ats_confidence_label = ""
    ats_confidence_value = 0.0
    if ats_model is not None:
        ats_vals, ats_names = _extract_feature_vector(row, "ats")
        dmat = xgb.DMatrix(ats_vals, feature_names=ats_names)
        home_ats_prob = float(ats_model.predict(dmat)[0])
        away_ats_prob = 1.0 - home_ats_prob
        if home_ats_prob > 0.5:
            ats_side = home_team
            ats_spread_str = f"-{abs(spread):.1f}"
            ats_text = f"{home_team} {ats_spread_str}"
            ats_probability = home_ats_prob
            ats_confidence_label, ats_confidence_value = _confidence_from_prob(home_ats_prob)
        else:
            ats_side = away_team
            ats_spread_str = f"+{abs(spread):.1f}"
            ats_text = f"{away_team} {ats_spread_str}"
            ats_probability = away_ats_prob
            ats_confidence_label, ats_confidence_value = _confidence_from_prob(away_ats_prob)
    else:
        away_ats_prob = 0.5

    # OU prediction
    ou_over_prob = 0.5
    ou_side = ""
    ou_text = ""
    ou_probability = 0.5
    ou_confidence_label = ""
    ou_confidence_value = 0.0
    if ou_model is not None:
        ou_vals, ou_names = _extract_feature_vector(row, "ou")
        dmat = xgb.DMatrix(ou_vals, feature_names=ou_names)
        ou_over_prob = float(ou_model.predict(dmat)[0])
        ou_under_prob = 1.0 - ou_over_prob
        if ou_over_prob > 0.5:
            ou_side = "Over"
            ou_text = f"Over {ou_line:.1f}"
            ou_probability = ou_over_prob
            ou_confidence_label, ou_confidence_value = _confidence_from_prob(ou_over_prob)
        else:
            ou_side = "Under"
            ou_text = f"Under {ou_line:.1f}"
            ou_probability = ou_under_prob
            ou_confidence_label, ou_confidence_value = _confidence_from_prob(ou_under_prob)
    else:
        ou_under_prob = 0.5

    # Moneyline
    if home_ats_prob > 0.5:
        ml_prob = home_ats_prob * 0.8 + 0.1
        ml_numeric = _prob_to_moneyline(ml_prob)
        ml_prob_away = 1 - ml_prob
        ml_numeric_away = _prob_to_moneyline(ml_prob_away)
        ml_favorite = home_team if spread < 0 else away_team
        ml_underdog = away_team if spread < 0 else home_team
    else:
        away_ml_prob = away_ats_prob * 0.8 + 0.1
        ml_numeric_away = _prob_to_moneyline(away_ml_prob)
        ml_prob = 1 - away_ml_prob
        ml_numeric = _prob_to_moneyline(ml_prob)
        ml_favorite = home_team if spread < 0 else away_team
        ml_underdog = away_team if spread < 0 else home_team

    # Build handicap info for PRIME DIRECTIVE compliance
    home_stats = _build_nba_home_stats(row)
    away_stats = _build_nba_away_stats(row)
    situational = _build_nba_situational(row)
    splits = _build_nba_splits(row)

    features_ats_json = _extract_pick_card_features(row, set(ats_feat))
    features_ou_json = _extract_pick_card_features(row, set(ou_feat))

    conn = _get_sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO nba.game_predictions (
                    game_id, home_team, away_team,
                    predicted_spread, predicted_over_under,
                    home_ats_prob, away_ats_prob,
                    ou_over_prob, ou_under_prob,
                    ats_side, ats_spread, ats_text, ats_probability,
                    ats_confidence_label, ats_confidence_value,
                    ou_side, ou_text, ou_probability,
                    ou_confidence_label, ou_confidence_value,
                    ml_favorite, ml_underdog, home_ml, away_ml,
                    home_ml_prob, away_ml_prob,
                    model_version, model_type,
                    features_ats_json, features_ou_json,
                    home_stats_json, away_stats_json,
                    situational_json, splits_json,
                    actual_home_score, actual_away_score,
                    actual_total, actual_margin,
                    actual_cover, actual_over,
                    created_at, updated_at
                ) VALUES (
                    %(game_id)s, %(home_team)s, %(away_team)s,
                    %(predicted_spread)s, %(predicted_over_under)s,
                    %(home_ats_prob)s, %(away_ats_prob)s,
                    %(ou_over_prob)s, %(ou_under_prob)s,
                    %(ats_side)s, %(ats_spread)s, %(ats_text)s, %(ats_probability)s,
                    %(ats_confidence_label)s, %(ats_confidence_value)s,
                    %(ou_side)s, %(ou_text)s, %(ou_probability)s,
                    %(ou_confidence_label)s, %(ou_confidence_value)s,
                    %(ml_favorite)s, %(ml_underdog)s, %(home_ml)s, %(away_ml)s,
                    %(home_ml_prob)s, %(away_ml_prob)s,
                    %(model_version)s, %(model_type)s,
                    %(features_ats_json)s, %(features_ou_json)s,
                    %(home_stats_json)s, %(away_stats_json)s,
                    %(situational_json)s, %(splits_json)s,
                    %(actual_home_score)s, %(actual_away_score)s,
                    %(actual_total)s, %(actual_margin)s,
                    %(actual_cover)s, %(actual_over)s,
                    %(created_at)s, %(updated_at)s
                )
                ON CONFLICT (game_id) DO UPDATE SET
                    home_ats_prob = EXCLUDED.home_ats_prob,
                    away_ats_prob = EXCLUDED.away_ats_prob,
                    ou_over_prob = EXCLUDED.ou_over_prob,
                    ou_under_prob = EXCLUDED.ou_under_prob,
                    ats_side = EXCLUDED.ats_side,
                    ats_spread = EXCLUDED.ats_spread,
                    ats_text = EXCLUDED.ats_text,
                    ats_probability = EXCLUDED.ats_probability,
                    ats_confidence_label = EXCLUDED.ats_confidence_label,
                    ats_confidence_value = EXCLUDED.ats_confidence_value,
                    ou_side = EXCLUDED.ou_side,
                    ou_text = EXCLUDED.ou_text,
                    ou_probability = EXCLUDED.ou_probability,
                    ou_confidence_label = EXCLUDED.ou_confidence_label,
                    ou_confidence_value = EXCLUDED.ou_confidence_value,
                    actual_home_score = EXCLUDED.actual_home_score,
                    actual_away_score = EXCLUDED.actual_away_score,
                    actual_total = EXCLUDED.actual_total,
                    actual_margin = EXCLUDED.actual_margin,
                    actual_cover = EXCLUDED.actual_cover,
                    actual_over = EXCLUDED.actual_over,
                    updated_at = EXCLUDED.updated_at
                """,
                {
                    "game_id": game_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "predicted_spread": spread,
                    "predicted_over_under": ou_line,
                    "home_ats_prob": round(home_ats_prob, 4),
                    "away_ats_prob": round(away_ats_prob, 4),
                    "ou_over_prob": round(ou_over_prob, 4),
                    "ou_under_prob": round(ou_under_prob, 4),
                    "ats_side": ats_side,
                    "ats_spread": ats_spread_str,
                    "ats_text": ats_text,
                    "ats_probability": round(ats_probability, 4),
                    "ats_confidence_label": ats_confidence_label,
                    "ats_confidence_value": round(ats_confidence_value, 4),
                    "ou_side": ou_side,
                    "ou_text": ou_text,
                    "ou_probability": round(ou_probability, 4),
                    "ou_confidence_label": ou_confidence_label,
                    "ou_confidence_value": round(ou_confidence_value, 4),
                    "ml_favorite": ml_favorite,
                    "ml_underdog": ml_underdog,
                    "home_ml": _int_safe(ml_numeric) or 0,
                    "away_ml": _int_safe(ml_numeric_away) or 0,
                    "home_ml_prob": round(ml_prob, 4),
                    "away_ml_prob": round(ml_prob_away, 4),
                    "model_version": f"nba-xgboost-{season}",
                    "model_type": "xgboost",
                    "features_ats_json": features_ats_json,
                    "features_ou_json": features_ou_json,
                    "home_stats_json": home_stats,
                    "away_stats_json": away_stats,
                    "situational_json": situational,
                    "splits_json": splits,
                    "actual_home_score": _float_safe(home_score) or 0.0,
                    "actual_away_score": _float_safe(away_score) or 0.0,
                    "actual_total": _float_safe(actual_total) or 0.0,
                    "actual_margin": _float_safe(actual_margin) or 0.0,
                    "actual_cover": actual_cover,
                    "actual_over": actual_over,
                    "created_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                },
            )
        conn.commit()
        logger.debug("  Saved backtest prediction for game %s (%s vs %s)",
                      game_id, home_team, away_team)
    except Exception as exc:
        logger.error("  Failed to save backtest prediction for game %s: %s", game_id, exc)
    finally:
        conn.close()


# ── Handicap info builders (PRIME DIRECTIVE) ──────────────────────────────────────


def _build_nba_home_stats(row: pd.Series) -> str:
    """Build home_stats_json for handicap info."""
    home_team = _str_safe(row.get("home_team"))
    home_ppg = _float_safe(row.get("home_ppg"))
    home_oppg = _float_safe(row.get("home_oppg"))
    home_win_pct = _float_safe(row.get("home_win_pct"))
    home_ats_pct = _float_safe(row.get("home_ats_pct"))
    home_ou_pct = _float_safe(row.get("home_ou_pct"))
    home_recent = _str_safe(row.get("home_recent"))
    return json.dumps({
        "team": home_team,
        "ppg": home_ppg,
        "oppg": home_oppg,
        "win_pct": home_win_pct,
        "ats_pct": home_ats_pct,
        "ou_pct": home_ou_pct,
        "recent_games": home_recent,
    })


def _build_nba_away_stats(row: pd.Series) -> str:
    """Build away_stats_json for handicap info."""
    away_team = _str_safe(row.get("away_team"))
    away_ppg = _float_safe(row.get("away_ppg"))
    away_oppg = _float_safe(row.get("away_oppg"))
    away_win_pct = _float_safe(row.get("away_win_pct"))
    away_ats_pct = _float_safe(row.get("away_ats_pct"))
    away_ou_pct = _float_safe(row.get("away_ou_pct"))
    away_recent = _str_safe(row.get("away_recent"))
    return json.dumps({
        "team": away_team,
        "ppg": away_ppg,
        "oppg": away_oppg,
        "win_pct": away_win_pct,
        "ats_pct": away_ats_pct,
        "ou_pct": away_ou_pct,
        "recent_games": away_recent,
    })


def _build_nba_situational(row: pd.Series) -> str:
    """Build situational_json for handicap info."""
    return json.dumps({
        "rest_days_home": _float_safe(row.get("home_rest")),
        "rest_days_away": _float_safe(row.get("away_rest")),
        "is_back_to_back": bool(row.get("is_b2b")),
        "home_at_home": True,
        "venue": _str_safe(row.get("venue")),
        "is_division_game": bool(row.get("is_div_game")),
    })


def _build_nba_splits(row: pd.Series) -> str:
    """Build splits_json for handicap info."""
    return json.dumps({
        "home_ats_home": _float_safe(row.get("home_ats_home")),
        "home_ats_away": _float_safe(row.get("home_ats_away")),
        "home_ou_home": _float_safe(row.get("home_ou_home")),
        "home_ou_away": _float_safe(row.get("home_ou_away")),
        "away_ats_home": _float_safe(row.get("away_ats_home")),
        "away_ats_away": _float_safe(row.get("away_ats_away")),
        "away_ou_home": _float_safe(row.get("away_ou_home")),
        "away_ou_away": _float_safe(row.get("away_ou_away")),
        "home_line_movement": _str_safe(row.get("home_line_movement")),
        "away_line_movement": _str_safe(row.get("away_line_movement")),
    })


# ── Safe casting helpers ──────────────────────────────────────────────────────────


def _int_safe(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _float_safe(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _str_safe(val) -> str:
    if val is None:
        return ""
    return str(val)


# ── CLI / smoke test ──────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys

    async def main():
        if len(sys.argv) > 1 and sys.argv[1] == "backtest":
            years_to_test = [2022, 2023, 2024, 2025]
            if len(sys.argv) > 2:
                years_to_test = [int(y) for y in sys.argv[2].split(",")]
            logger.info("Backtesting NBA seasons %s...", years_to_test)
            results = await backtest_season(years=years_to_test, limit=None, save_results=True)
            for mt in ("ats", "ou"):
                for r in results.get(mt, []):
                    print(f"  {mt.upper()} {r['year']}: "
                          f"{r.get('correct',0)}/{r.get('total_games',0)} "
                          f"({r.get('accuracy',0)*100:.1f}%) "
                          f"AUC={r.get('auc','N/A')}")

        elif len(sys.argv) > 1 and sys.argv[1] == "predict":
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
            logger.info("Predicting NBA games for next %d days...", days)
            cards = asyncio.run(batch_predict_upcoming_games(days_ahead=days, save_to_db=True))
            print(f"  Predicted {len(cards)} games")

        else:
            print("Usage: python -m backend.app.handicapping.nba.nba_engine [backtest|predict]")
            print("  backtest [years]    — backtest models (e.g. '2022,2023,2024,2025')")
            print("  predict [days]      — predict upcoming games (default 7 days)")
            sys.exit(1)

    asyncio.run(main())
