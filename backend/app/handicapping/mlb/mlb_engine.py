"""
MLB Handicapping Engine — v2 (refactored)

Architecture
────────────
  • Feature engineering is delegated to MLBDataLoader.load_games() +
    build_features() — the same pipeline used for training.
  • Pickled model files are year-specific, stored at
    ~/.openclaw/workspace/earl-knows-football/data/models/mlb/{uuid}-{year}.pkl.
    The filenames live in mlb.training_runs.pkl_filename (comma-separated,
    one per year); the current run is marked is_current = TRUE.
  • No on-the-fly training in the engine.
  • Every route uses the same DataFrame-driven pipeline so inference and
    backtesting are structurally identical.
"""

import json
import logging
import math
import os
import pickle
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# Local helpers
from app.handicapping.mlb.data_loader import MLBDataLoader, build_features, get_data_loader, get_model_features
from app.handicapping.calibrate_confidence import calibrate
from app.models.mlb.game_prediction import MLBGamePrediction

logger = logging.getLogger("earl.mlb_handicapping")

CURRENT_YEAR = 2026

MODELS_DIR = Path.home() / ".openclaw" / "workspace" / "earl-knows-football" / "data" / "models" / "mlb"

# ═══════════════════════════════════════════════════════════════════
# Year-specific model loader from training_runs + disk pkl
# ═══════════════════════════════════════════════════════════════════

def _resolve_year_pkl_paths(model_type: str) -> Dict[int, Path]:
    """Query the current training_run for *model_type* and return a map of
    ``{year: Path}`` for every year covered by the ``pkl_filename`` field.

    The ``pkl_filename`` column is a comma-separated list like::

        uuid-2025.pkl,uuid-2026.pkl

    Each file lives under ``data/models/mlb/``.

    Returns an empty dict when no current run exists or no pkl files found.
    """
    from app.handicapping.db_training import get_current_training_run
    run = get_current_training_run("mlb", model_type)
    if run is None:
        logger.warning("  No current training_run for mlb/%s", model_type)
        return {}

    raw = run.get("pkl_filename", "")
    if not raw:
        logger.warning("  training_run for mlb/%s has empty pkl_filename", model_type)
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
        logger.info("  Year pkl files for mlb/%s: %s", model_type, out)
    else:
        logger.warning("  No year pkl files found for mlb/%s", model_type)
    return out


def _load_model_for_year(model_type: str, year: int) -> Any:
    """Load the pickled XGBoost model for *model_type* and *year* from disk.

    Raises ``FileNotFoundError`` if the file cannot be found.
    """
    paths = _resolve_year_pkl_paths(model_type)
    p = paths.get(year)
    if p is None:
        raise FileNotFoundError(
            f"No pkl file for mlb/{model_type} year {year}. "
            f"Available years: {sorted(paths.keys())}"
        )
    logger.info("  Loading %s model for year %s from %s", model_type, year, p)
    with open(p, "rb") as fh:
        return pickle.load(fh)


# ═══════════════════════════════════════════════════════════════════
# Feature column names — loaded from mlb.features DB table
# ═══════════════════════════════════════════════════════════════════

_FEATURE_COLS: Optional[Dict[str, List[str]]] = None


def _get_features() -> Dict[str, List[str]]:
    """Lazy-load feature column names from mlb.features via get_model_features().

    Queries ``SELECT name FROM mlb.features WHERE current_<type> = true``,
    matching the training pipeline.
    """
    global _FEATURE_COLS
    if _FEATURE_COLS is not None:
        return _FEATURE_COLS

    ats = get_model_features("ats")
    ou = get_model_features("ou")
    _FEATURE_COLS = {"ats": ats, "ou": ou}
    logger.info("_get_features: loaded %d ats + %d ou features from mlb.features", len(ats), len(ou))
    return _FEATURE_COLS


def _extract_feature_vector(row: pd.Series, model_type: str) -> Optional[np.ndarray]:
    """Extract the feature vector of ``model_type`` features from one row.

    Feature columns come from ``mlb.features`` (current_ats/current_ou).
    Returns ``None`` if any required feature is missing or NaN.
    """
    cols = _get_features()[model_type]
    vals = []
    for c in cols:
        v = row.get(c)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            logger.debug("  Missing feature '%s' for game %s", c, row.get("game_id"))
            return None
        vals.append(float(v))
    return np.array(vals, dtype=np.float32)


def _infer_margin(row: pd.Series, model_type: str, prob: float) -> float:
    """Crude margin inference — only used by _build_reasoning for display.

    For ATS: probability -> implied margin.
    For OU:  probability -> implied total.
    """
    if model_type == "ats":
        spread = float(row.get("h_line_runline", row.get("a_line_runline", 1.5)) or 1.5)
        return (prob - 0.5) * spread * 2.0
    total = float(row.get("h_line_total", row.get("a_line_total", 8.0)) or 8.0)
    return total + (prob - 0.5) * 2.0


# ═══════════════════════════════════════════════════════════════════
# MLBHandicapper — class-based entry-point used by handicap_mlb.py routes
# ═══════════════════════════════════════════════════════════════════

class MLBHandicapper:
    """Backward-compatible wrapper.  Instantiate once per request.

    ``handicap_game(game_id)`` is called by the routes.  Features are built
    via the data_loader pipeline; models use the **current** year's pkl file
    from the training_runs table.
    """

    def __init__(self, db: AsyncSession, sport_slug: str = "mlb") -> None:
        self._db = db
        self._sport = sport_slug
        self._game_df: Optional[pd.DataFrame] = None
        self._ats_model: Any = None
        self._ou_model: Any = None

    # ── public methods ───────────────────────────────────────────

    async def handicap_game(self, game_id: int | str) -> Optional[Dict[str, Any]]:
        """Return a pick-card dict for *game_id*."""
        logger.info("MLBHandicapper.handicap_game(%s)", game_id)

        if self._game_df is None:
            self._game_df = await self._load_feature_df()
            if self._game_df is None:
                return None
        if self._ats_model is None:
            self._ats_model = _load_model_for_year("ats", CURRENT_YEAR)
        if self._ou_model is None:
            self._ou_model = _load_model_for_year("ou", CURRENT_YEAR)

        row = self._game_df[self._game_df["game_id"].astype(str) == str(game_id)]
        if row.empty:
            logger.warning("  game_id %s not found in feature DataFrame", game_id)
            return None
        row = row.iloc[0]

        ats_pred = self._predict_ats(row)
        ou_pred = self._predict_ou(row)
        return _build_pick_card(row, ats_pred, ou_pred)

    async def handicap_date(self, target_date: date) -> List[Dict[str, Any]]:
        """Generate pick cards for all games on *target_date*."""
        logger.info("MLBHandicapper.handicap_date(%s)", target_date)
        dl = get_data_loader()
        games = dl.load_games(seasons=[target_date.year], status=None, include_upcoming=True)
        if games.empty:
            return []
        df = build_features(games)
        target_str = str(target_date)
        df = df[df["game_date"].astype(str) == target_str].copy()
        if df.empty:
            return []

        if self._ats_model is None:
            self._ats_model = _load_model_for_year("ats", CURRENT_YEAR)
        if self._ou_model is None:
            self._ou_model = _load_model_for_year("ou", CURRENT_YEAR)

        cards: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            ats_pred = self._predict_ats(row) if self._ats_model else None
            ou_pred = self._predict_ou(row) if self._ou_model else None
            cards.append(_build_pick_card(row, ats_pred, ou_pred))
        return cards

    # ── internals ────────────────────────────────────────────────

    async def _load_feature_df(self) -> Optional[pd.DataFrame]:
        dl = get_data_loader()
        games = dl.load_games(status="FINAL")
        if games.empty:
            logger.warning("MLBHandicapper: no games loaded")
            return None
        df = build_features(games)
        logger.info("MLBHandicapper: feature DF loaded (%d rows, %d cols)", len(df), len(df.columns))
        return df

    def _predict_ats(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        if self._ats_model is None:
            return None
        try:
            feats = _extract_feature_vector(row, "ats")
            if feats is None:
                return None
            pred_margin = float(self._ats_model.predict(feats[np.newaxis, :])[0])
            spread = float(row.get("spread", row.get("h_line_runline", 1.5)) or 1.5)
            # home covers iff predicted margin + spread > 0  (matches training eval)
            home_cover_prob = min(max((pred_margin + spread) / (spread * 4) + 0.5, 0.0), 1.0)
            return {
                "home_cover_prob": round(float(home_cover_prob), 4),
                "model_margin": round(pred_margin, 2),
            }
        except Exception as exc:
            logger.warning("_predict_ats failed for %s: %s", row.get("game_id"), exc)
            return None

    def _predict_ou(self, row: pd.Series) -> Optional[Dict[str, Any]]:
        if self._ou_model is None:
            return None
        try:
            feats = _extract_feature_vector(row, "ou")
            if feats is None:
                return None
            pred_total = float(self._ou_model.predict(feats[np.newaxis, :])[0])
            line_total = float(row.get("h_line_total", row.get("a_line_total", 8.0)) or 8.0)
            return {
                "over_prob": round(float(min(max(pred_total / line_total, 0.0), 1.0)), 4),
                "predicted_total": round(pred_total, 2),
            }
        except Exception as exc:
            logger.warning("_predict_ou failed for %s: %s", row.get("game_id"), exc)
            return None


# ═══════════════════════════════════════════════════════════════════
# Pick Card Builder
# ═══════════════════════════════════════════════════════════════════

def _build_pick_card(
    game_row: pd.Series,
    ats_pred: Optional[Dict[str, Any]],
    ou_pred: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the pick-card dict from a single feature-row + prediction results."""
    home_team = str(game_row.get("ha", ""))
    away_team = str(game_row.get("aa", ""))

    card: Dict[str, Any] = {
        "game_id": str(game_row.get("game_id", "")),
        "home_team": home_team,
        "away_team": away_team,
        "game_date": str(game_row.get("game_date", date.today())),
        "season_year": int(game_row.get("season_year", CURRENT_YEAR)),
        "ats_pick": None,
        "ou_pick": None,
        "confidence": 0.0,
        "reasoning": _build_reasoning(game_row, ats_pred, ou_pred),
    }

    if ats_pred:
        home_cover_prob = ats_pred["home_cover_prob"]
        if home_cover_prob >= 0.50:
            pick_team = home_team
            pick_side = "home"
            confidence = calibrate(home_cover_prob, "mlb", "ats")
        else:
            pick_team = away_team
            pick_side = "away"
            confidence = calibrate(1 - home_cover_prob, "mlb", "ats")
        card["ats_pick"] = {
            "pick": pick_team,
            "side": pick_side,
            "cover_probability": home_cover_prob,
            "model_margin": ats_pred["model_margin"],
        }
        card["confidence"] = max(card["confidence"], confidence)

    if ou_pred:
        over_prob = ou_pred["over_prob"]
        card["ou_pick"] = {
            "pick": "over" if over_prob >= 0.50 else "under",
            "over_probability": over_prob,
            "predicted_total": ou_pred["predicted_total"],
        }
        conf = calibrate(max(over_prob, 1 - over_prob), "mlb", "ou")
        card["confidence"] = max(card["confidence"], conf)

    row_dict = dict(game_row)
    card["handicap_info"] = {
        "home_stats": _build_mlb_home_stats(row_dict),
        "away_stats": _build_mlb_away_stats(row_dict),
        "situational": _build_mlb_situational(row_dict),
        "splits": _build_mlb_splits(row_dict),
    }

    return card


def _build_reasoning(
    game_row: pd.Series,
    ats_pred: Optional[Dict[str, Any]],
    ou_pred: Optional[Dict[str, Any]],
) -> str:
    """Generate a readable reasoning string (abbreviated for pick cards)."""
    parts: List[str] = []
    home_team = str(game_row.get("ha", ""))
    away_team = str(game_row.get("aa", ""))

    rest_h = int(game_row.get("h_rest_days", 0))
    rest_a = int(game_row.get("a_rest_days", 0))
    rest_diff = rest_h - rest_a
    parts.append(f"{home_team} rest={rest_h}d, {away_team} rest={rest_a}d (diff={rest_diff:+d})")

    dome_h = int(game_row.get("h_dome_flag", 0))
    if dome_h:
        parts.append("Home dome")

    h_form = game_row.get("h_form_10", "n/a")
    if h_form != "n/a":
        parts.append(f"{home_team} L10: {float(h_form):.0%}")

    h_era = game_row.get("h_starter_era", None)
    a_era = game_row.get("a_starter_era", None)
    if h_era is not None and not np.isnan(h_era):
        parts.append(f"Starter ERA: {home_team}={h_era:.2f}")
    if a_era is not None and not np.isnan(a_era):
        parts.append(f"{away_team}={a_era:.2f}")

    h_ops = game_row.get("h_ops", None)
    a_ops = game_row.get("a_ops", None)
    if h_ops is not None and not np.isnan(h_ops):
        parts.append(f"OPS: {home_team}={h_ops:.3f}, {away_team}={a_ops:.3f}")

    if ats_pred:
        prob = ats_pred["home_cover_prob"]
        if prob >= 0.50:
            parts.append(f"ATS->{home_team} ({prob:.1%})")
        else:
            parts.append(f"ATS->{away_team} ({(1 - prob):.1%})")

    if ou_pred:
        op = ou_pred["over_prob"]
        parts.append(f"O/U->{'Over' if op >= 0.50 else 'Under'} ({op:.1%})")

    return " | ".join(parts)


# ═══════════════════════════════════════════════════════════════════
# backtest_season — standalone function, matches route signature
# ═══════════════════════════════════════════════════════════════════

async def backtest_season(
    db: AsyncSession,
    year: int,
    resume: bool = True,
    num_games: int = 10,
) -> Dict[str, Any]:
    """Backtest MLB models over a full season using year-specific pkl files.

    Called from ``GET /handicapping/mlb/backtest/{year}``.

    The pkl files are year-specific (one per year) — the current
    ``training_runs.pkl_filename`` is a comma-separated list, and we pick
    the file matching *year*.  Models live at ``data/models/mlb/``.

    For every game this also saves a prediction record to
    ``mlb.game_predictions`` (source='api') so the Admin predictions page
    can display and aggregate results.
    """
    logger.info("backtest_season: year=%s resume=%s", year, resume)

    # ── 1. Load year-specific models from disk ───────────────────
    try:
        ats_model = _load_model_for_year("ats", year)
    except FileNotFoundError as exc:
        logger.error("ATS model not available for %s: %s", year, exc)
        return _zeros_return()

    try:
        ou_model = _load_model_for_year("ou", year)
    except FileNotFoundError as exc:
        logger.error("OU model not available for %s: %s", year, exc)
        return _zeros_return()

    # ── 2. Load games + build features (single pipeline) ─────────
    dl = get_data_loader()
    # Load all data from 2020 for rolling stats (same as training pipeline), then filter to target year
    games = dl.load_games(seasons=list(range(2020, year + 1)), status="FINAL")
    if games.empty:
        logger.warning("  No games found for %s", year)
        return _zeros_return()

    df = build_features(games)
    logger.info("  Feature DataFrame: %d rows x %d cols", len(df), len(df.columns))

    df = df[df["season_year"] == year].copy()
    if df.empty:
        logger.warning("  No games for season_year=%s after feature build", year)
        return _zeros_return()

    # ── 3. Resume: skip already-predicted games ──────────────────
    existing_preds: set = set()
    if resume:
        try:
            r = await db.execute(
                text("SELECT DISTINCT game_id FROM mlb.game_predictions WHERE source = 'api'")
            )
            existing_preds = {str(row[0]) for row in r.fetchall()}
            logger.info("  resume=True — %d existing predictions found", len(existing_preds))
        except Exception:
            logger.warning("  resume=True but could not query game_predictions; evaluating all")

    # ── 4. Evaluate every game, save prediction record ────────────
    rl_w = rl_l = rl_p = 0
    ou_w = ou_l = ou_p = 0
    saved = 0

    for _, row in df.iterrows():
        gid = str(row.get("game_id", ""))
        if resume and gid in existing_preds:
            continue

        home_score = int(row.get("home_score", 0))
        away_score = int(row.get("away_score", 0))
        margin = home_score - away_score

        spread = float(row.get("spread", row.get("h_line_runline", 1.5)) or 1.5)
        # Use ou_line (aliased from over_under in build_features), fallback to 8.0
        total = float(row.get("ou_line", row.get("over_under", 8.0)) or 8.0)

        # ── Predictions ──
        feats_ats = _extract_feature_vector(row, "ats")
        feats_ou = _extract_feature_vector(row, "ou")

        pred_margin = float(ats_model.predict(feats_ats[np.newaxis, :])[0]) if feats_ats is not None else 0.0
        pred_total = float(ou_model.predict(feats_ou[np.newaxis, :])[0]) if feats_ou is not None else 0.0

        pred_home_covers = (pred_margin + spread) > 0
        pred_over = pred_total > total
        pred_home_wins = pred_margin > 0

        # Actual outcomes
        home_covers = (margin + spread) > 0
        actual_over = (home_score + away_score) > total
        home_wins = margin > 0

        # ── Accuracy counts ──
        if pred_home_covers == home_covers:
            rl_w += 1
        elif (home_score + spread) == away_score:
            rl_p += 1
        else:
            rl_l += 1

        if pred_over == actual_over:
            ou_w += 1
        elif (home_score + away_score) == total:
            ou_p += 1
        else:
            ou_l += 1

        # ── Save predictions to game_predictions ──
        saved += await _save_backtest_prediction(
            db, row, year,
            home_score, away_score, spread, total,
            pred_margin, pred_total, pred_home_covers, pred_over, pred_home_wins,
            home_covers, actual_over, home_wins,
        )

    await db.commit()

    rl_pct = round(rl_w / (rl_w + rl_l) * 100, 2) if (rl_w + rl_l) else 0.0
    ou_pct = round(ou_w / (ou_w + ou_l) * 100, 2) if (ou_w + ou_l) else 0.0

    result = {
        "run_line": {"pct": rl_pct, "w": rl_w, "l": rl_l, "push": rl_p},
        "over_under": {"pct": ou_pct, "w": ou_w, "l": ou_l, "push": ou_p},
        "moneyline": {"pct": 0.0, "w": 0, "l": 0, "push": 0},
    }
    logger.info("  Saved %d predictions. Result: %s", saved, result)
    return result


def _int_safe(v, default: int = 0) -> int:
    try:
        return int(v) if v is not None and not (isinstance(v, float) and math.isnan(v)) else default
    except (ValueError, TypeError):
        return default


def _float_safe(v, default: Optional[float] = None):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _str_safe(v, default: str = "") -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return default
    return str(v)


def _build_mlb_home_stats(row: dict) -> dict:
    """Build home team stats dict from a feature row."""
    return {
        "team_name": _str_safe(row.get("ha", "")),
        "abbreviation": _str_safe(row.get("ha", "")),
        "wins": _int_safe(row.get("h_home_wins", 0)),
        "losses": _int_safe(row.get("h_home_losses", 0)),
        "pitcher": _str_safe(row.get("home_pitcher_name", "TBD")),
        "runs_scored_avg": _float_safe(row.get("h_runs_scored_avg", 0.0)),
        "runs_allowed_avg": _float_safe(row.get("h_runs_allowed_avg", 0.0)),
        "park_factor": _float_safe(row.get("h_park_factor", 1.0)),
    }


def _build_mlb_away_stats(row: dict) -> dict:
    """Build away team stats dict from a feature row."""
    return {
        "team_name": _str_safe(row.get("aa", "")),
        "abbreviation": _str_safe(row.get("aa", "")),
        "wins": _int_safe(row.get("a_away_wins", 0)),
        "losses": _int_safe(row.get("a_away_losses", 0)),
        "pitcher": _str_safe(row.get("away_pitcher_name", "TBD")),
        "runs_scored_avg": _float_safe(row.get("a_runs_scored_avg", 0.0)),
        "runs_allowed_avg": _float_safe(row.get("a_runs_allowed_avg", 0.0)),
        "park_factor": _float_safe(row.get("a_park_factor", 1.0)),
    }


def _build_mlb_situational(row: dict) -> dict:
    """Build situational data dict from a feature row."""
    roof = _str_safe(row.get("roof_type", "Outdoor")).lower()
    return {
        "venue": _str_safe(row.get("venue", "")),
        "roof_type": roof,
        "surface": _str_safe(row.get("surface", "")),
        "day_night": _str_safe(row.get("day_night", "")),
        "temperature": _float_safe(row.get("temperature")),
        "wind_speed": _float_safe(row.get("wind_speed")),
        "wind_direction": _str_safe(row.get("wind_direction", "")),
        "weather_condition": _str_safe(row.get("weather_condition", "")),
        "attendance": _int_safe(row.get("attendance", 0)),
        "is_dome": "dome" in roof or "retractable" in roof,
    }


def _build_mlb_splits(row: dict) -> dict:
    """Build splits/line-movement dict from a feature row."""
    open_spread = _float_safe(row.get("opening_spread"))
    close_spread = _float_safe(row.get("closing_spread"))
    open_ou = _float_safe(row.get("opening_total"))
    close_ou = _float_safe(row.get("closing_total"))

    spread_move = round(open_spread - close_spread, 1) if (open_spread is not None and close_spread is not None) else None
    total_move = round(close_ou - open_ou, 1) if (open_ou is not None and close_ou is not None) else None

    return {
        "opening_line": {"spread": open_spread, "total": open_ou},
        "closing_line": {"spread": close_spread, "total": close_ou},
        "line_movement": {
            "spread": spread_move,
            "total": total_move,
        },
        "moneyline": {
            "home": _float_safe(row.get("home_moneyline")),
            "away": _float_safe(row.get("away_moneyline")),
        },
    }


async def _save_backtest_prediction(
    db: AsyncSession,
    row: pd.Series,
    year: int,
    home_score: int, away_score: int, spread: float, total: float,
    pred_margin: float, pred_total: float,
    pred_home_covers: bool, pred_over: bool, pred_home_wins: bool,
    home_covers: bool, actual_over: bool, home_wins: bool,
) -> int:
    """Save a single game\'s prediction to ``mlb.game_predictions``.

    Computes profit, confidence, and EV for each pick (run-line, OU,
    moneyline) using real odds from the betting lines, $100 per bet.
    """
    gid = str(row.get("game_id", ""))
    home_team = str(row.get("ha", ""))
    away_team = str(row.get("aa", ""))
    margin = home_score - away_score

    now = datetime.utcnow()

    # Real odds from betting lines
    home_rl_odds = _safe_int(row.get("closing_spread_home_odds"), -110)
    away_rl_odds = _safe_int(row.get("closing_spread_away_odds"), -110)
    over_odds = _safe_int(row.get("closing_over_odds"), -110)
    under_odds = _safe_int(row.get("closing_under_odds"), -110)
    home_ml_odds = _safe_int(row.get("home_moneyline"), 0)
    away_ml_odds = _safe_int(row.get("away_moneyline"), 0)

    rl_picked_home = pred_home_covers
    ou_picked_over = pred_over
    ml_picked_home = pred_home_wins

    rl_odds = home_rl_odds if rl_picked_home else away_rl_odds
    ou_odds = over_odds if ou_picked_over else under_odds
    ml_odds = home_ml_odds if ml_picked_home else away_ml_odds

    # Results
    if pred_home_covers == home_covers:
        rl_result = "Win"
    elif (home_score + spread) == away_score:
        rl_result = "Push"
    else:
        rl_result = "Loss"

    ou_result = "Win" if pred_over == actual_over else ("Loss" if pred_over != actual_over else "Push")
    ml_result = "Win" if pred_home_wins == home_wins else "Loss"

    # Profit at $100 per pick
    def _pl(result_: str, odds_: float) -> float:
        if result_ == "Win":
            return round(100.0 * _profit_per_100(odds_), 2)
        if result_ == "Loss":
            return -100.0
        return 0.0

    ats_profit = _pl(rl_result, rl_odds)
    ou_profit = _pl(ou_result, ou_odds)
    ml_profit = _pl(ml_result, ml_odds)

    # Confidence heuristic (matches old MLBPickCard)
    rl_conf = min(0.5 + abs(pred_margin + spread) * 0.4, 0.90)
    ml_conf = min(0.5 + abs(pred_margin) * 0.25, 0.92)
    ou_conf = min(0.5 + abs(pred_total - total) * 0.25, 0.92)
    margin_conf = rl_conf
    overall_conf = max(rl_conf, ou_conf, ml_conf)

    # EV at $100 stake
    def _ev(conf_: float, odds_: float) -> float:
        profit_if_win = 100.0 * _profit_per_100(odds_)
        return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

    ats_ev = _ev(rl_conf, rl_odds)
    ou_ev = _ev(ou_conf, ou_odds)
    ml_ev = _ev(ml_conf, ml_odds)

    # Predicted score
    predicted_home_score = int(max(0, round(
        (pred_margin + pred_total) / 2.0 + 0.5 * (pred_margin if pred_margin > 0 else 0)
    )))
    predicted_away_score = int(max(0, round(
        (pred_total - pred_margin) / 2.0 + 0.5 * (-pred_margin if pred_margin < 0 else 0)
    )))

    # Pick text
    if rl_picked_home:
        sign = "+" if spread > 0 else ""
        rl_pick_str = f"{home_team} {sign}{-spread:+g}"
    else:
        sign = "+" if spread < 0 else ""
        rl_pick_str = f"{away_team} {sign}{spread:+g}"
    rl_pick_str = rl_pick_str.replace("+", "").replace("-", "-")

    # Remove old prediction for this game+source pair, then insert fresh
    gp = MLBGamePrediction(
        game_id=int(gid),
        predicted_home_runs=predicted_home_score,
        predicted_away_runs=predicted_away_score,
        predicted_total=round(pred_total, 2),
        predicted_margin=round(pred_margin, 2),
        ou_pick="Over" if ou_picked_over else "Under",
        run_line_pick=rl_pick_str,
        ml_pick="home" if ml_picked_home else "away",
        actual_home_runs=home_score,
        actual_away_runs=away_score,
        actual_total=home_score + away_score,
        actual_margin=margin,
        run_line_result=rl_result,
        ou_result=ou_result,
        ml_result=ml_result,
        ats_odds=int(round(rl_odds)),
        ou_odds=int(round(ou_odds)),
        ml_odds=int(round(ml_odds)),
        ats_profit=ats_profit,
        ou_profit=ou_profit,
        ml_profit=ml_profit,
        rl_conf=round(rl_conf, 4),
        ou_conf=round(ou_conf, 4),
        ml_conf=round(ml_conf, 4),
        margin_conf=round(margin_conf, 4),
        ats_ev=ats_ev,
        ou_ev=ou_ev,
        ml_ev=ml_ev,
        home_stats_json=json.dumps(_build_mlb_home_stats(dict(row))),
        away_stats_json=json.dumps(_build_mlb_away_stats(dict(row))),
        situational_json=json.dumps(_build_mlb_situational(dict(row))),
        splits_json=json.dumps(_build_mlb_splits(dict(row))),
        source="api",
        created_at=now,
    )
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(MLBGamePrediction).where(
        MLBGamePrediction.game_id == int(gid),
        MLBGamePrediction.source == "api",
    ))
    await db.flush()
    db.add(gp)
    return 1


def _profit_per_100(odds: float) -> float:
    """Return the profit on a $100 bet at *odds* (American format)."""
    if odds < 0:
        return 100.0 / abs(odds)
    return odds / 100.0


def _break_even_prob(odds: float) -> float:
    """Implied win probability from American odds."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100.0)
    return 100.0 / (odds + 100.0)


def _safe_int(val, default: int = -110) -> int:
    """Coerce a value to int; return *default* if None / NaN / invalid."""
    if val is None:
        return default
    if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _compat_build_extra_features(df: pd.DataFrame) -> pd.DataFrame:
    """No-op."""
    return df

def _compat_build_extra_features(df: pd.DataFrame) -> pd.DataFrame:
    """No-op: all feature engineering is in ``build_features()`` from
    ``data_loader``.  Kept for backward API compatibility."""
    return df
