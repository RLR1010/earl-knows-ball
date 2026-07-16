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

from app.handicapping.calibrate_confidence import calibrate

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
import xgboost as xgb
from sqlalchemy import text, delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.models.nba.game_prediction import NBAGamePrediction
from sklearn.metrics import roc_auc_score, mean_absolute_error, mean_squared_error

from app.handicapping.nba.data_loader import NBADataLoader, get_data_loader, get_model_features

logger = logging.getLogger(__name__)


def _profit_per_100(odds: float) -> float:
    """Profit on a $100 bet at American odds."""
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


def _ev(conf_: float, odds_: float) -> float:
    """Expected value for a $100 bet given calibrated win prob and American odds."""
    profit_if_win = 100.0 * _profit_per_100(odds_)
    return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)


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
    if model_type == "ats":
        feats = get_model_features(target="ats")
        _FEATURES_CACHE_ATS = feats
    else:
        feats = get_model_features(target="ou")
        _FEATURES_CACHE_OU = feats
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

    # ATS prediction (regression model outputs MARGIN: home_score - away_score)
    ats_vals, ats_names = _extract_feature_vector(row, "ats")
    ats_dmat = xgb.DMatrix(ats_vals, feature_names=ats_names)
    pred_margin = float(ats_model.predict(ats_dmat)[0])

    # OU prediction (regression model outputs TOTAL: home_score + away_score)
    ou_vals, ou_names = _extract_feature_vector(row, "ou")
    ou_dmat = xgb.DMatrix(ou_vals, feature_names=ou_names)
    pred_total = float(ou_model.predict(ou_dmat)[0])

    # ATS confidence: how far is predicted margin from the spread line
    ats_conf_val = min(0.5 + abs(pred_margin + (spread or 0)) * 0.03, 0.90) if spread else 0.5
    ou_conf_val = min(0.5 + abs(pred_total - (ou_line or 0)) * 0.03, 0.90) if ou_line else 0.5
    ml_conf_val = min(0.5 + abs(pred_margin) * 0.03, 0.90)

    # Moneyline
    if home_team and away_team:
        favorite = home_team if pred_margin > 0 else away_team
        underdog = away_team if pred_margin > 0 else home_team
        # Derive ML odds from margin prediction
        ml_prob_est = 1.0 / (1.0 + 10.0 ** (-pred_margin / 10.0)) if pred_margin != 0 else 0.5
        ml_numeric = _prob_to_moneyline(ml_prob_est)
        ml_numeric_away = _prob_to_moneyline(1 - ml_prob_est)
    else:
        ml_numeric = 0
        ml_numeric_away = 0

    # ATS: home covers if predicted_margin > -(spread)
    home_covers = pred_margin > -(spread or 0)
    if home_covers:
        ats_side = home_team
        ats_spread = f"-{abs(spread):.1f}" if spread else "PK"
        ats_text = f"{home_team} {ats_spread}"
        ats_prob_val = ats_conf_val  # confidence IS the probability
    else:
        ats_side = away_team
        ats_spread = f"+{abs(spread):.1f}" if spread else "PK"
        ats_text = f"{away_team} {ats_spread}"
        ats_prob_val = ats_conf_val

    # OU: over if predicted_total > ou_line
    pred_over = pred_total > (ou_line or 0)
    if pred_over:
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
        "predicted_spread": pred_margin,                    # predicted margin
        "predicted_over_under": pred_total,                  # predicted total score
        "home_ats_prob": round(ats_conf_val, 4),
        "away_ats_prob": round(1 - ats_conf_val, 4),
        "ou_over_prob": round(ou_conf_val, 4),
        "ou_under_prob": round(1 - ou_conf_val, 4),
        "ats_pick": {
            "team": ats_side,
            "spread": ats_spread,
            "text": ats_text,
            "probability": round(ats_prob_val, 4),
            "confidence": round(ats_conf_val, 4),
        },
        "ou_pick": {
            "team": ou_side,
            "text": ou_text,
            "probability": round(ou_conf_val, 4),
            "confidence": round(ou_conf_val, 4),
        },
        "ml_pick": {
            "favorite": favorite if home_team else "",
            "underdog": underdog if away_team else "",
            "home_ml": ml_numeric,
            "away_ml": ml_numeric_away,
            "home_ml_prob": round(ml_prob_est, 4) if home_team else 0.5,
            "away_ml_prob": round(1 - ml_prob_est, 4) if away_team else 0.5,
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
        # Test on 2024 and 2025 by default (matching nfl/engine.py)
        years = [2024, 2025]

    dl = get_data_loader()
    if limit is not None:
        df = dl.load_games(limit=limit)
    else:
        df = dl.load_data()
    logger.info("Loaded %d NBA games from data loader%s", len(df), f" (limit={limit})" if limit else "")

    # Keep enough history for rolling-feature computation (at least one
    # year prior to the earliest test year, matching nfl/engine.py)
    min_data_year = min(years) - 1
    df = df[df["season_year"] >= min_data_year].copy()
    logger.info(
        "Filtered to season_year >= %d — %d games remaining (for rolling stats)",
        min_data_year, len(df),
    )

    ats_results: List[Dict[str, Any]] = []
    ou_results: List[Dict[str, Any]] = []
    total_game_preds = 0

    for year in years:
        ats_model = _load_model_for_year(year, "ats")
        ou_model = _load_model_for_year(year, "ou")

        if ats_model is None and ou_model is None:
            logger.warning("  No models for year %s – skipping", year)
            continue

        year_df = df[df["season_year"] == year].copy()
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
            sessionmaker = db or _get_async_session()
            async with sessionmaker() as session:
                for idx, row in year_df.iterrows():
                    ats_feats, ats_names = _extract_feature_vector(row, "ats")
                    ou_feats, ou_names = _extract_feature_vector(row, "ou")
                    gid = row.get("game_id")
                    await _save_backtest_prediction(
                        game_id=gid,
                        row=row,
                        ats_model=ats_model,
                        ou_model=ou_model,
                        ats_features=(ats_feats, ats_names),
                        ou_features=(ou_feats, ou_names),
                        db=session,
                    )
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


# ── Save API prediction ──────────────────────────────────────────────────────────


async def _save_api_prediction(
    row: pd.Series,
    pick_card: Dict[str, Any],
    db: Optional[async_sessionmaker] = None,
) -> None:
    """Save a predicted pick card using the NBAGamePrediction ORM model.

    ``row`` is the game row from the data loader (or DB query).
    ``pick_card`` is the dict returned by ``_build_pick_card``.

    Uses the async DB session when ``db`` is provided, otherwise
    creates one via ``_get_async_session()``.
    """
    import uuid as _uuid_mod

    game_id = pick_card.get("game_id") or row.get("game_id")
    if not game_id:
        logger.warning("No game_id in pick_card or row \u2014 skipping save")
        return

    now = datetime.now(timezone.utc)
    source = pick_card.get("model_version", "api")

    # Normalise game_id to a UUID if it looks numeric
    if isinstance(game_id, (int, str)):
        try:
            game_id = _uuid_mod.UUID(str(game_id))
        except (ValueError, _uuid_mod.ValueError, AttributeError):
            try:
                game_id = _uuid_mod.UUID(int=int(game_id))
            except (ValueError, TypeError):
                pass  # leave as-is

    # \u2500\u2500 Extract nested ATS / OU / ML picks \u2500\u2500
    ats_pick = pick_card.get("ats_pick", {})
    ou_pick = pick_card.get("ou_pick", {})
    ml_pick = pick_card.get("ml_pick", {})

    spread_pick = ats_pick.get("team", "") or pick_card.get("ats_side", "")
    ou_pick_side = ou_pick.get("team", "") or pick_card.get("ou_side", "")
    ml_pick_side = ml_pick.get("favorite", "") or pick_card.get("ml_favorite", "")

    # \u2500\u2500 Confidence / probability values \u2500\u2500
    # Confidence is now a float (from margin-spread gap), not a tuple; ats_pick.confidence IS the value
    margin_conf = ats_pick.get("confidence") or ats_pick.get("probability") or pick_card.get("ats_probability", 0.50)
    ou_conf = ou_pick.get("confidence") or ou_pick.get("probability") or pick_card.get("ou_probability", 0.50)

    ml_conf = ml_pick.get("confidence") or pick_card.get("ml_probability") or pick_card.get("home_ml_prob", 0.50)

    # ── Line / spread / odds values ──
    # Read odds from data_loader row (betting_agg CTE columns)
    spread_home_odds = _float_safe(row.get("spread_home_odds"))
    spread_away_odds = _float_safe(row.get("spread_away_odds"))
    ats_odds = spread_home_odds if spread_pick == str(row.get("home_team", "")) else spread_away_odds

    over_odds_val = _float_safe(row.get("over_odds"))
    under_odds_val = _float_safe(row.get("under_odds"))
    ou_odds = over_odds_val if ou_pick_side == "Over" else under_odds_val

    home_ml = _float_safe(row.get("home_moneyline"))
    away_ml = _float_safe(row.get("away_moneyline"))
    ml_odds = ml_pick.get("home_ml") or pick_card.get("home_ml")
    if _odds_row is None:
        # Fallback: try reading from data_loader row
        _odds_row = FakeRow(
            spread_home_odds=row.get("spread_home_odds"),
            spread_away_odds=row.get("spread_away_odds"),
            over_odds=row.get("over_odds"),
            under_odds=row.get("under_odds"),
            home_moneyline=row.get("home_moneyline"),
            away_moneyline=row.get("away_moneyline"),
        )

    spread_home_odds = _float_safe(_odds_row.spread_home_odds if _odds_row else None)
    spread_away_odds = _float_safe(_odds_row.spread_away_odds if _odds_row else None)
    ats_odds = spread_home_odds if spread_pick == str(row.get("home_team", "")) else spread_away_odds

    over_odds_val = _float_safe(_odds_row.over_odds if _odds_row else None)
    under_odds_val = _float_safe(_odds_row.under_odds if _odds_row else None)
    ou_odds = over_odds_val if ou_pick_side == "Over" else under_odds_val

    home_ml = _float_safe(_odds_row.home_moneyline if _odds_row else None)
    away_ml = _float_safe(_odds_row.away_moneyline if _odds_row else None)
    ml_odds = ml_pick.get("home_ml") or pick_card.get("home_ml")
    # \u2500\u2500 Predicted scores \u2500\u2500
    predicted_total = pick_card.get("predicted_over_under") or pick_card.get("ou_predicted_total")
    predicted_margin = pick_card.get("predicted_spread") or pick_card.get("spread", 0)

    pred_home_score = None
    pred_away_score = None
    if predicted_total is not None and predicted_margin is not None:
        pred_home_score = max(0.0, round((float(predicted_total) + float(predicted_margin)) / 2.0, 1))
        pred_away_score = max(0.0, round((float(predicted_total) - float(predicted_margin)) / 2.0, 1))

    # \u2500\u2500 Feature JSON \u2500\u2500
    features_ats_json = pick_card.get("features_ats_json")
    if not features_ats_json:
        ats_feats = _get_features("ats")
        features_ats_json = _extract_pick_card_features(row, set(ats_feats))
    features_ou_json = pick_card.get("features_ou_json")
    if not features_ou_json:
        ou_feats = _get_features("ou")
        features_ou_json = _extract_pick_card_features(row, set(ou_feats))

    features_dict = {}
    if features_ats_json:
        features_dict["ats"] = features_ats_json if isinstance(features_ats_json, dict) else json.loads(features_ats_json)
    if features_ou_json:
        features_dict["ou"] = features_ou_json if isinstance(features_ou_json, dict) else json.loads(features_ou_json)
    features_json_str = json.dumps(features_dict, default=str) if features_dict else None

    # \u2500\u2500 Handicapper info \u2500\u2500
    def _load_or_dumps(val: Any) -> Optional[str]:
        if val is None:
            return None
        if isinstance(val, str):
            return val
        return json.dumps(val, default=str)

    home_stats_json = _load_or_dumps(pick_card.get("home_stats_json") or pick_card.get("home_stats") or {})
    away_stats_json = _load_or_dumps(pick_card.get("away_stats_json") or pick_card.get("away_stats") or {})
    situational_json = _load_or_dumps(pick_card.get("situational_json") or pick_card.get("situational") or {})
    splits_json = _load_or_dumps(pick_card.get("splits_json") or pick_card.get("splits") or {})

    # \u2500\u2500 Calibrated confidences \u2500\u2500
    ats_conf_cal = pick_card.get("ats_conf_cal") or (
        calibrate(float(margin_conf), "ats", "nba") if margin_conf is not None else None
    )
    ml_conf_cal = pick_card.get("ml_conf_cal") or (
        calibrate(float(ml_conf), "ml", "nba") if ml_conf is not None else None
    )
    ou_conf_cal = pick_card.get("ou_conf_cal") or (
        calibrate(float(ou_conf), "ou", "nba") if ou_conf is not None else None
    )

    # ── Expected value (calibrated conf × odds) ──
    ats_ev = _ev(ats_conf_cal, ats_odds) if ats_conf_cal is not None and ats_odds else None
    ou_ev = _ev(ou_conf_cal, ou_odds) if ou_conf_cal is not None and ou_odds else None
    ml_ev = _ev(ml_conf_cal, ml_odds) if ml_conf_cal is not None and ml_odds else None

    _f = lambda v: round(float(v), 4) if v is not None else None

    # \u2500\u2500 Assemble ORM record \u2500\u2500
    rec = NBAGamePrediction(
        game_id=game_id,
        predicted_home_score=_f(pred_home_score),
        predicted_away_score=_f(pred_away_score),
        predicted_total=_f(predicted_total),
        predicted_margin=_f(predicted_margin),
        margin_conf=_f(margin_conf),
        ml_conf=_f(ml_conf),
        ou_conf=_f(ou_conf),
        ats_conf_cal=_f(ats_conf_cal),
        ml_conf_cal=_f(ml_conf_cal),
        ou_conf_cal=_f(ou_conf_cal),
        ou_pick=str(ou_pick_side) if ou_pick_side else None,
        spread_pick=str(spread_pick) if spread_pick else None,
        ml_pick=str(ml_pick_side) if ml_pick_side else None,
        ats_odds=_f(ats_odds),
        ou_odds=_f(ou_odds),
        ml_odds=_f(ml_odds),
        ats_ev=_f(ats_ev),
        ou_ev=_f(ou_ev),
        ml_ev=_f(ml_ev),
        home_stats_json=home_stats_json,
        away_stats_json=away_stats_json,
        situational_json=situational_json,
        splits_json=splits_json,
        features_json=features_json_str,
        source=source,
        created_at=now,
    )

    try:
        if db is not None:
            async with db() as session:
                await session.execute(
                    sa_delete(NBAGamePrediction).where(
                        NBAGamePrediction.game_id == game_id,
                        NBAGamePrediction.source == source,
                    )
                )
                session.add(rec)
                await session.commit()
        else:
            async with _get_async_session() as session:
                await session.execute(
                    sa_delete(NBAGamePrediction).where(
                        NBAGamePrediction.game_id == game_id,
                        NBAGamePrediction.source == source,
                    )
                )
                session.add(rec)
                await session.commit()
        logger.debug("Saved API prediction for game %s (source=%s)", game_id, source)
    except Exception as exc:
        logger.error("Failed to save API prediction for game %s: %s", game_id, exc)


async def _save_backtest_prediction(
    game_id,
    row,
    ats_model=None,
    ou_model=None,
    ats_features=None,
    ou_features=None,
    db: AsyncSession = None,
) -> None:
    """Save a single backtest prediction using the NBAGamePrediction ORM model.

    Mirrors the NFL/MLB _save_backtest_prediction pattern.
    Relies on row being a pd.Series / dict-like with the actual game outcome.
    """
    if not game_id:
        return

    engine = None
    close_session = False
    if db is None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session as SyncSession

        from app.core.config import settings as _st
        dsn = _st.database_url.replace("+asyncpg", "").replace("postgresql+asyncpg://", "postgresql://")
        engine = create_engine(dsn)
        db = SyncSession(engine)
        close_session = True

    try:
        home_team_id_str = str(row.get("home_team_id", ""))
        away_team_id_str = str(row.get("away_team_id", ""))
        home_str = home_team_id_str
        away_str = away_team_id_str
        spread = _float_safe(row.get("closing_spread", row.get("spread", 0)))
        over_under = _float_safe(row.get("closing_ou", row.get("over_under", 0)))

        # ── ATS prediction ────────────────────────────────────────────
        ats_proba = 0.5
        if ats_model is not None and ats_features is not None:
            feats, names = ats_features
            if feats is not None:
                dmat = xgb.DMatrix(feats, feature_names=names)
                ats_proba = float(ats_model.predict(dmat)[0])

        # ── OU prediction ─────────────────────────────────────────────
        ou_total = None
        if ou_model is not None and ou_features is not None:
            feats, names = ou_features
            if feats is not None:
                dmat = xgb.DMatrix(feats, feature_names=names)
                ou_total = float(ou_model.predict(dmat)[0])

        # ── Actuals ────────────────────────────────────
        home_score = _float_safe(row.get("home_score"))
        away_score = _float_safe(row.get("away_score"))
        actual_total = (home_score or 0) + (away_score or 0)
        actual_margin = (home_score or 0) - (away_score or 0)

        # ── Picks ─────────────────────────────────────────────────

        if ats_proba > -(spread or 0):
            spread_pick = home_str
        else:
            spread_pick = away_str

        ou_pick = None
        if ou_total is not None and over_under is not None:
            ou_pick = "Over" if ou_total > over_under else "Under"

        ml_pick = home_str if ats_proba > 0 else away_str  # regression model: margin > 0 = home wins

        # ── Results ───────────────────────────────────────────────────
        ats_result = None
        ou_result = None
        ml_result = None
        if home_score is not None and away_score is not None:
            ml_result = "Win" if (home_score > away_score and ml_pick == home_str) or (away_score > home_score and ml_pick == away_str) else "Loss"
        if spread is not None and spread != 0 and home_score is not None and away_score is not None:
            effective_margin = actual_margin + spread if spread_pick == home_str else -(actual_margin + spread)
            if effective_margin > 0:
                ats_result = "win"
            elif effective_margin == 0:
                ats_result = "push"
            else:
                ats_result = "loss"
        if over_under is not None and ou_total is not None and home_score is not None:
            if actual_total > over_under:
                ou_result = "over" if ou_pick == "Over" else "under"
            elif actual_total < over_under:
                ou_result = "under" if ou_pick == "Over" else "over"
            else:
                ou_result = "push"

        # ── Odds ──────────────────────────────────────────────────────
        # Extract odds from betting_lines_consolidated (via data_loader betting_agg CTE)
        # Read odds from data_loader DataFrame (via betting_agg CTE in SQL)
        spread_home_odds = _float_safe(row.get("spread_home_odds"))
        spread_away_odds = _float_safe(row.get("spread_away_odds"))
        over_odds_val = _float_safe(row.get("over_odds"))
        under_odds_val = _float_safe(row.get("under_odds"))
        home_ml = _float_safe(row.get("home_moneyline"))
        away_ml = _float_safe(row.get("away_moneyline"))

        # Map odds to the picked side
        ats_odds_value = spread_home_odds if spread_pick == home_str else spread_away_odds
        ou_odds_value = over_odds_val if ou_pick == "Over" else under_odds_val
        ml_odds_value = home_ml if ml_pick == home_str else away_ml

        # ── Profit/Loss (per $100 bet) ────────────────────────────
        # ATS/OU use the actual spread/over-under odds from betting_lines_consolidated
        ats_profit = 0.0
        if ats_result == "win" and ats_odds_value:
            ats_profit = round(100.0 * _profit_per_100(ats_odds_value), 2)
        elif ats_result == "loss":
            ats_profit = -100.0

        ou_profit = 0.0
        if ou_result == "over" and ou_odds_value:
            ou_profit = round(100.0 * _profit_per_100(ou_odds_value), 2)
        elif ou_result == "under":
            ou_profit = -100.0

        ml_profit = 0.0
        if ml_odds_value and ml_result == "Win":
            ml_profit = round(100.0 * _profit_per_100(float(ml_odds_value)), 2)
        elif ml_result == "Loss":
            ml_profit = -100.0
        # ── Handicapper info (best-effort; row may lack display columns) ──
        try:
            home_stats = _build_nba_home_stats(row)
        except Exception:
            home_stats = json.dumps({"team": str(row.get("home_team_id", ""))})
        try:
            away_stats = _build_nba_away_stats(row)
        except Exception:
            away_stats = json.dumps({"team": str(row.get("away_team_id", ""))})
        try:
            situational = _build_nba_situational(row)
        except Exception:
            situational = json.dumps({})
        try:
            splits = _build_nba_splits(row)
        except Exception:
            splits = json.dumps({})

        # ── Features JSON ────────────────────────────────────────────────────
        ats_feats = _get_features("ats") if callable(_get_features) else []
        ou_feats = _get_features("ou") if callable(_get_features) else []
        features_dict = {}
        if ats_features and ats_features[0] is not None and all(f in row.index for f in ats_feats if isinstance(f, str)):
            features_dict["ats"] = {f: _float_safe(row[f]) for f in ats_feats if isinstance(f, str)}
        if ou_features and ou_features[0] is not None and all(f in row.index for f in ou_feats):
            features_dict["ou"] = {f: _float_safe(row[f]) for f in ou_feats}
        features_json_str = json.dumps(features_dict, default=str) if features_dict else None

        # ── Predicted scores & confidence ────────────────────────────────────
        predicted_margin = ats_proba  # ATS regression model outputs margin (home_score - away_score) directly
        predicted_total = ou_total
        margin_conf = round(min(0.5 + abs(predicted_margin + spread) * 0.03, 0.90), 4) if spread else 0.5
        ml_conf_val = round(min(0.5 + abs(predicted_margin) * 0.025, 0.92), 4) if predicted_margin is not None else 0.5
        ou_conf_val = round(min(0.5 + abs(ou_total - over_under) * 0.04, 0.92), 4) if (ou_total is not None and over_under) else 0.5

        # ── Calibrate raw confidences ──
        ats_conf_cal = calibrate(float(margin_conf), "ats", "nba") if margin_conf is not None else None
        ml_conf_cal = calibrate(float(ml_conf_val), "ml", "nba") if ml_conf_val is not None else None
        ou_conf_cal = calibrate(float(ou_conf_val), "ou", "nba") if ou_conf_val is not None else None

        # ── Expected value ──
        ats_ev = _ev(ats_conf_cal, ats_odds_value) if ats_conf_cal is not None and ats_odds_value else None
        ou_ev = _ev(ou_conf_cal, ou_odds_value) if ou_conf_cal is not None and ou_odds_value else None
        ml_ev = _ev(ml_conf_cal, ml_odds_value) if ml_conf_cal is not None and ml_odds_value else None

        logger.info(
            "NBA backtest game %s: spread=%s, ats_proba=%s, pred_margin=%s, margin_conf=%s, "
            "ou_total=%s, over_under=%s, ml_result=%s",
            game_id, spread, ats_proba, predicted_margin, margin_conf,
            ou_total, over_under, ml_result
        )
        pred_home_score = None
        pred_away_score = None
        if predicted_total is not None and predicted_margin is not None:
            pred_home_score = max(0.0, round((predicted_total + predicted_margin) / 2.0, 1))
            pred_away_score = max(0.0, round((predicted_total - predicted_margin) / 2.0, 1))

        # ── Build ORM record ────────────────────────────────────────────
        _f = lambda v: round(float(v), 4) if v is not None else None

        rec = NBAGamePrediction(
            game_id=game_id,
            predicted_home_score=_f(pred_home_score),
            predicted_away_score=_f(pred_away_score),
            predicted_total=_f(predicted_total),
            predicted_margin=_f(predicted_margin),
            margin_conf=_f(margin_conf),
            ml_conf=_f(ml_conf_val),
            ou_conf=_f(ou_conf_val),
            ats_conf_cal=_f(ats_conf_cal),
            ml_conf_cal=_f(ml_conf_cal),
            ou_conf_cal=_f(ou_conf_cal),
            ats_ev=_f(ats_ev),
            ou_ev=_f(ou_ev),
            ml_ev=_f(ml_ev),
            ou_pick=ou_pick,
            spread_pick=spread_pick,
            ml_pick=ml_pick,
            actual_home_score=_f(home_score),
            actual_away_score=_f(away_score),
            actual_total=_f(actual_total),
            actual_margin=_f(actual_margin),
            ats_result=ats_result,
            ou_result=ou_result,
            ml_result=ml_result,
            ats_odds=round(ats_odds_value) if ats_odds_value else None,
            ou_odds=round(ou_odds_value) if ou_odds_value else None,
            ml_odds=round(ml_odds_value) if ml_odds_value else None,
            ats_profit=_f(ats_profit),
            ou_profit=_f(ou_profit),
            ml_profit=_f(ml_profit),
            home_stats_json=home_stats if isinstance(home_stats, str) else json.dumps(home_stats, default=str) if home_stats else None,
            away_stats_json=away_stats if isinstance(away_stats, str) else json.dumps(away_stats, default=str) if away_stats else None,
            situational_json=situational if isinstance(situational, str) else json.dumps(situational, default=str) if situational else None,
            splits_json=splits if isinstance(splits, str) else json.dumps(splits, default=str) if splits else None,
            features_json=features_json_str,
            source="backtest",
            created_at=datetime.now(timezone.utc),
        )

        # ── Save via ORM ───────────────────────────────────────────────────────────
        logger.info(
            "Saving backtest prediction game_id=%s: "
            "ml_conf=%s ou_conf=%s ml_result=%s margin_conf=%s "
            "ats_result=%s ou_result=%s ml_pick=%s",
            game_id, rec.ml_conf, rec.ou_conf, rec.ml_result,
            rec.margin_conf, rec.ats_result, rec.ou_result, rec.ml_pick,
        )
        if close_session:
            db.execute(
                sa_delete(NBAGamePrediction).where(
                    NBAGamePrediction.game_id == game_id,
                    NBAGamePrediction.source == "backtest",
                )
            )
            db.add(rec)
            db.commit()
        else:
            await db.execute(
                sa_delete(NBAGamePrediction).where(
                    NBAGamePrediction.game_id == game_id,
                    NBAGamePrediction.source == "backtest",
                )
            )
            db.add(rec)
            await db.commit()

    except Exception:
        logger.exception("_save_backtest_prediction failed for game %s", game_id)
    finally:
        if engine is not None:
            engine.dispose()



def _build_nba_home_stats(row: pd.Series) -> str:
    """Build home_stats_json for handicap info using NBA data loader columns."""
    return json.dumps({
        "team": _str_safe(row.get("home_team")),
        "abbreviation": _str_safe(row.get("home_abbr")),
        "ortg_r10": _float_safe(row.get("h_ortg_r10")),
        "drtg_r10": _float_safe(row.get("h_drtg_r10")),
        "net_rtg_r10": _float_safe(row.get("h_net_rtg_r10")),
        "pace_r10": _float_safe(row.get("h_pace_r10")),
        "adj_off_r10": _float_safe(row.get("h_adj_off_10")),
        "adj_def_r10": _float_safe(row.get("h_adj_def_10")),
        "ats_wins_r10": _float_safe(row.get("h_ats_wins_10")),
        "ats_margin_r10": _float_safe(row.get("h_ats_margin_10")),
        "ats_pct_r10": _calc_pct(row.get("h_ats_wins_10"), 10),
        "wins_r10": _float_safe(row.get("h_wins_10")),
        "win_pct_r10": _calc_pct(row.get("h_wins_10"), 10),
        "ou_wins_r10": _float_safe(row.get("h_ou_wins_10")),
        "ou_pct_r10": _calc_pct(row.get("h_ou_wins_10"), 10),
        "ou_margin_r5": _float_safe(row.get("h_ou_margin_5")),
        "ft_rate_r10": _float_safe(row.get("h_ft_rate_r10")),
        "three_in_four": bool(row.get("h_three_in_four", 0)),
        "implied_prob": _float_safe(row.get("h_implied")),
        "rest_days": _float_safe(row.get("rest_h")),
        "is_b2b": bool(row.get("home_b2b", 0)),
    })


def _build_nba_away_stats(row: pd.Series) -> str:
    """Build away_stats_json for handicap info using NBA data loader columns."""
    return json.dumps({
        "team": _str_safe(row.get("away_team")),
        "abbreviation": _str_safe(row.get("away_abbr")),
        "ortg_r10": _float_safe(row.get("a_ortg_r10")),
        "drtg_r10": _float_safe(row.get("a_drtg_r10")),
        "net_rtg_r10": _float_safe(row.get("a_net_rtg_r10")),
        "pace_r10": _float_safe(row.get("a_pace_r10")),
        "adj_off_r10": _float_safe(row.get("a_adj_off_10")),
        "adj_def_r10": _float_safe(row.get("a_adj_def_10")),
        "ats_wins_r10": _float_safe(row.get("a_ats_wins_10")),
        "ats_margin_r10": _float_safe(row.get("a_ats_margin_10")),
        "ats_pct_r10": _calc_pct(row.get("a_ats_wins_10"), 10),
        "wins_r10": _float_safe(row.get("a_wins_10")),
        "win_pct_r10": _calc_pct(row.get("a_wins_10"), 10),
        "ou_wins_r10": _float_safe(row.get("a_ou_wins_10")),
        "ou_pct_r10": _calc_pct(row.get("a_ou_wins_10"), 10),
        "ou_margin_r5": _float_safe(row.get("a_ou_margin_5")),
        "ft_rate_r10": _float_safe(row.get("a_ft_rate_r10")),
        "three_in_four": bool(row.get("a_three_in_four", 0)),
        "implied_prob": _float_safe(row.get("a_implied")),
        "rest_days": _float_safe(row.get("rest_a")),
        "is_b2b": bool(row.get("away_b2b", 0)),
    })


def _build_nba_situational(row: pd.Series) -> str:
    """Build situational_json for handicap info using NBA data loader columns."""
    return json.dumps({
        "rest_days_home": _float_safe(row.get("rest_h")),
        "rest_days_away": _float_safe(row.get("rest_a")),
        "rest_diff": _float_safe(row.get("rest_diff")),
        "home_b2b": bool(row.get("home_b2b", 0)),
        "away_b2b": bool(row.get("away_b2b", 0)),
        "travel_miles": _float_safe(row.get("travel_miles")),
        "home_at_home": True,
        "venue": "",
        "is_division_game": False,
        "season_week": _float_safe(row.get("season_week")),
        "two_games_today": bool(row.get("two_games", 0)),
    })


def _build_nba_splits(row: pd.Series) -> str:
    """Build splits_json for handicap info using NBA data loader columns."""
    return json.dumps({
        "closing_spread": _float_safe(row.get("closing_spread")),
        "opening_spread": _float_safe(row.get("opening_spread")),
        "spread_movement": _float_safe(row.get("spread_movement")),
        "closing_ou": _float_safe(row.get("closing_ou")),
        "opening_ou": _float_safe(row.get("opening_ou")),
        "spread_home_odds": _float_safe(row.get("spread_home_odds")),
        "spread_away_odds": _float_safe(row.get("spread_away_odds")),
        "home_ml": _float_safe(row.get("home_ml")),
        "away_ml": _float_safe(row.get("away_ml")),
        "home_implied_pct": _float_safe(row.get("h_implied")),
        "away_implied_pct": _float_safe(row.get("a_implied")),
        "home_ats_cover_pct_r10": _calc_pct(row.get("h_ats_wins_10"), 10),
        "away_ats_cover_pct_r10": _calc_pct(row.get("a_ats_wins_10"), 10),
        "home_ou_over_pct_r10": _calc_pct(row.get("h_ou_wins_10"), 10),
        "away_ou_over_pct_r10": _calc_pct(row.get("a_ou_wins_10"), 10),
    })


# ── Safe casting helpers ──────────────────────────────────────────────────────────


def _calc_pct(val, total: int) -> Optional[float]:
    """Compute a percentage from wins/total, safely handling None/NaN."""
    if val is None or total <= 0:
        return None
    try:
        fval = float(val)
        if np.isnan(fval):
            return None
        return round(fval / total, 3)
    except (ValueError, TypeError):
        return None


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

    def _configure_logging() -> None:
        """Ensure the root logger has a handler so CLI output is visible."""
        root = logging.getLogger()
        if not root.handlers:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
                datefmt="%H:%M:%S",
            )

    async def _main():
        _configure_logging()
        from dotenv import load_dotenv
        load_dotenv()
        # Ensure DATABASE_URL uses sync scheme for data-loader/psycopg2 compat
        raw = os.environ.get("DATABASE_URL", "")
        if "+asyncpg" in raw:
            os.environ["DATABASE_URL"] = raw.replace("+asyncpg", "").replace("postgresql+asyncpg://", "postgresql://")
        # Default: run backtest with 2024,2025 when no args or "backtest" specified
        if len(sys.argv) < 2 or sys.argv[1] == "backtest":
            kwargs = {"years": [2024, 2025]}
            if len(sys.argv) > 2:
                kwargs["years"] = [int(y) for y in sys.argv[2].split(",")]
            logger.info("Backtesting NBA seasons %s...", kwargs["years"])
            results = await backtest_season(**kwargs, limit=None, save_results=True)
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
            print("  backtest [years]    -- backtest models (default: 2024,2025)")
            print("  predict [days]      -- predict upcoming games (default 7 days)")
            print("  (default: backtest 2024,2025 when run with no args)")
            sys.exit(1)

    asyncio.run(_main())
