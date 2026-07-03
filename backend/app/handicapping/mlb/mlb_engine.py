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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import select as sa_select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

# Local helpers
from app.handicapping.mlb.data_loader import MLBDataLoader, build_features, get_data_loader, get_model_features
from app.handicapping.calibrate_confidence import calibrate
from app.models.mlb.consolidated import MLBBettingLineConsolidated

# ── Cached pick-card feature names ──
_PICK_CARD_FEATURES: Optional[set] = None

async def _load_pick_card_feature_names(db) -> set:
    """Lazy-load the set of feature names where pick_card = true."""
    global _PICK_CARD_FEATURES
    if _PICK_CARD_FEATURES is not None:
        return _PICK_CARD_FEATURES
    result = await db.execute(text("SELECT name FROM mlb.features WHERE pick_card = true"))
    _PICK_CARD_FEATURES = set(r[0] for r in result.fetchall())
    return _PICK_CARD_FEATURES


def _extract_pick_card_features(row, feature_names: set) -> str:
    """Return JSON string of pick_card feature values from a DataFrame row."""
    return json.dumps({name: row.get(name) for name in feature_names if name in row.index or name in row}, default=str)
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
    from app.handicapping.db_training import get_live_training_run
    run = get_live_training_run("mlb", model_type)
    if run is None:
        logger.warning("  No live training_run for mlb/%s", model_type)
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

    ats = get_model_features("ats", live=True)
    ou = get_model_features("ou", live=True)
    _FEATURE_COLS = {"ats": ats, "ou": ou}
    logger.info("_get_features: loaded %d ats + %d ou features from mlb.features", len(ats), len(ou))
    return _FEATURE_COLS


def _extract_feature_vector(row: pd.Series, model_type: str) -> Optional[np.ndarray]:
    """Extract the feature vector of ``model_type`` features from one row.

    Feature columns come from ``mlb.features`` (live_ats/live_ou).
    Returns ``None`` if any required feature is missing or NaN.
    """
    cols = _get_features()[model_type]
    vals = []
    for c in cols:
        v = row.get(c)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            # Weather / ancillary features may be missing for upcoming games.
            # Fill with sensible defaults so the model still gets a vector.
            if c in ("temperature", "temp"):
                v = 80.0  # average summer game temp
            elif c in ("humidity",):
                v = 50.0
            elif c in ("wind_speed", "wind"):
                v = 5.0
            else:
                v = 0.0
            logger.debug("  Missing feature '%s' for game %s — filling %.1f", c, row.get("game_id"), v)
        vals.append(float(v))
    return np.array(vals, dtype=np.float32)



async def batch_predict_upcoming_games(
    db: AsyncSession,
    game_ids: List[int],
    _logger: logging.Logger,
    year: int = CURRENT_YEAR,
) -> List[Dict[str, Any]]:
    """
    Load models, build features, and generate predictions for a batch of
    upcoming MLB games.  This is the core prediction pipeline used by
    /ingest/mlb/lines-and-picks.

    Returns a list of dicts, one per game.
    """
    import pandas as pd
    import numpy as np

    ats_model = _load_model_for_year("ats", year)
    ou_model = _load_model_for_year("ou", year)
    _logger.info(
        f"Models loaded for {year} (ats={'loaded' if ats_model else 'none'}, "
        f"ou={'loaded' if ou_model else 'none'})"
    )

    dl = get_data_loader()
    all_historic = dl.load_games(status="FINAL", include_upcoming=False)
    target_games = dl.load_games(status=None, include_upcoming=True, game_ids=game_ids)
    combined = pd.concat([all_historic, target_games], ignore_index=True)
    df = build_features(combined)
    _logger.info(f"Feature df built: {df.shape[0]} rows, {df.shape[1]} cols")

    rows_result = await db.execute(
        sa_select(MLBBettingLineConsolidated).where(
            MLBBettingLineConsolidated.game_id.in_(game_ids)
        )
    )
    line_rows = {r.game_id: r for r in rows_result.scalars().all()}

    pick_results: List[Dict[str, Any]] = []
    for gid in game_ids:
        try:
            row = df[df["game_id"].astype(str) == str(gid)]
            if row.empty:
                _logger.warning(f"Game {gid} not in feature set")
                pick_results.append({"game_id": gid, "error": "not_in_feature_set"})
                continue
            row_s = row.iloc[0]

            line = line_rows.get(gid)
            spread = (
                float(line.closing_spread)
                if line and line.closing_spread
                else (
                    float(row_s.get("spread", row_s.get("h_line_runline", 1.5)))
                    if pd.notna(row_s.get("spread"))
                    else None
                )
            )
            total = (
                float(line.closing_ou)
                if line and line.closing_ou
                else (
                    float(row_s.get("over_under", row_s.get("ou_line", 8.5)))
                    if pd.notna(row_s.get("over_under"))
                    else None
                )
            )

            ats_feats = _extract_feature_vector(row_s, "ats")
            ou_feats = _extract_feature_vector(row_s, "ou")

            if ats_feats is not None and ats_model:
                pred_margin = float(ats_model.predict(ats_feats[np.newaxis, :])[0])
            else:
                pred_margin = 0.0

            if ou_feats is not None and ou_model:
                pred_total = float(ou_model.predict(ou_feats[np.newaxis, :])[0])
            else:
                pred_total = total or 8.5

            pred_home_covers = pred_margin > -(spread or 0) if spread else True
            pred_over = pred_total > (total or 8.5) if total else True
            pred_home_wins = pred_margin > 0

            pic_feats = await _load_pick_card_feature_names(db)  # lazy-cached
            await _save_api_prediction(
                db=db,
                row=row_s,
                year=year,
                spread=spread,
                total=total,
                pred_margin=pred_margin,
                pred_total=pred_total,
                pred_home_covers=pred_home_covers,
                pred_over=pred_over,
                pred_home_wins=pred_home_wins,
                pick_card_features=pic_feats,
            )

            pick_results.append(
                {
                    "game_id": gid,
                    "predicted_margin": round(pred_margin, 2),
                    "predicted_total": round(pred_total, 2),
                    "pred_home_covers": pred_home_covers,
                    "pred_over": pred_over,
                    "pred_home_wins": pred_home_wins,
                }
            )
        except Exception as exc:
            _logger.warning(f"Prediction failed for game {gid}: {exc}")
            pick_results.append({"game_id": gid, "error": str(exc)[:200]})

    await db.commit()
    return pick_results

async def _save_api_prediction(
    db: AsyncSession,
    row: pd.Series,
    year: int,
    spread: float | None,
    total: float | None,
    pred_margin: float,
    pred_total: float,
    pred_home_covers: bool,
    pred_over: bool,
    pred_home_wins: bool,
    pick_card_features: set | None = None,
) -> int:
    """Save a live (pre-game) prediction to ``mlb.game_predictions``.

    Unlike ``_save_backtest_prediction``, actual results are left as
    NULL because the game hasn't been played yet.  Confidence / EV
    are still computed from the model outputs and the real odds.
    """
    gid = str(row.get("game_id", ""))
    home_team = str(row.get("ha", ""))
    away_team = str(row.get("aa", ""))
    now = datetime.now(timezone.utc)

    # Real odds from consolidated line
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

    # Confidence heuristic (matches old MLBPickCard)
    rl_conf = min(0.5 + abs(pred_margin + spread) * 0.4, 0.90) if spread else 0.5
    ml_conf = min(0.5 + abs(pred_margin) * 0.25, 0.92)
    ou_conf = min(0.5 + abs(pred_total - total) * 0.25, 0.92) if total else 0.5
    margin_conf = rl_conf

    # EV at $100 stake
    def _ev(conf_: float, odds_: float) -> float:
        profit_if_win = 100.0 * _profit_per_100(odds_)
        return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

    ats_ev = _ev(rl_conf, rl_odds) if rl_odds else 0.0
    ou_ev = _ev(ou_conf, ou_odds) if ou_odds else 0.0
    ml_ev = _ev(ml_conf, ml_odds) if ml_odds else 0.0

    # Predicted score (inferred from margin + total)
    home_score_raw = (pred_total + pred_margin) / 2.0
    away_score_raw = (pred_total - pred_margin) / 2.0
    predicted_home_score = round(home_score_raw, 1)
    predicted_away_score = round(away_score_raw, 1)

    # Pick text
    if spread is not None:
        home_run_line_val = spread          # home team perspective
        away_run_line_val = -spread          # away team perspective
        if rl_picked_home:
            rl_pick_str = f"{home_team} {home_run_line_val:+g}"
        else:
            rl_pick_str = f"{away_team} {away_run_line_val:+g}"
    else:
        rl_pick_str = ""

    # Remove old prediction for this game+source pair, then insert fresh
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(MLBGamePrediction).where(
        MLBGamePrediction.game_id == int(gid),
        MLBGamePrediction.source == "api",
    ))
    await db.flush()

    gp = MLBGamePrediction(
        game_id=int(gid),
        predicted_home_runs=predicted_home_score,
        predicted_away_runs=predicted_away_score,
        predicted_total=round(pred_total, 2),
        predicted_margin=round(pred_margin, 2),
        ou_pick="Over" if ou_picked_over else "Under",
        run_line_pick=rl_pick_str,
        ml_pick="home" if ml_picked_home else "away",
        rl_conf=round(rl_conf, 4),
        ou_conf=round(ou_conf, 4),
        ml_conf=round(ml_conf, 4),
        margin_conf=round(margin_conf, 4),
        ats_ev=ats_ev,
        ou_ev=ou_ev,
        ml_ev=ml_ev,
        ats_odds=int(round(rl_odds)),
        ou_odds=int(round(ou_odds)),
        ml_odds=int(round(ml_odds)),
        home_stats_json=json.dumps(_build_mlb_home_stats(dict(row))),
        away_stats_json=json.dumps(_build_mlb_away_stats(dict(row))),
        situational_json=json.dumps(_build_mlb_situational(dict(row))),
        splits_json=json.dumps(_build_mlb_splits(dict(row))),
        features_json=_extract_pick_card_features(row, pick_card_features) if pick_card_features else None,
        source="api",
        created_at=now,
    )
    db.add(gp)
    await db.flush()
    return 1


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
                text("SELECT DISTINCT game_id FROM mlb.game_predictions WHERE source IN ('api', 'backtest')")
            )
            existing_preds = {str(row[0]) for row in r.fetchall()}
            logger.info("  resume=True — %d existing predictions found", len(existing_preds))
        except Exception:
            logger.warning("  resume=True but could not query game_predictions; evaluating all")

    # ── 4. Evaluate every game, save prediction record ────────────
    rl_w = rl_l = rl_p = 0
    ou_w = ou_l = ou_p = 0
    ml_w = ml_l = ml_p = 0
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

        if (home_score + away_score) == total:
            ou_p += 1
        elif pred_over == actual_over:
            ou_w += 1
        else:
            ou_l += 1

        if pred_home_wins == home_wins:
            ml_w += 1
        else:
            ml_l += 1

        # ── Save predictions to game_predictions ──
        pick_card_feats = await _load_pick_card_feature_names(db)
        saved += await _save_backtest_prediction(
            db, row, year,
            home_score, away_score, spread, total,
            pred_margin, pred_total, pred_home_covers, pred_over, pred_home_wins,
            home_covers, actual_over, home_wins,
            pick_card_features=pick_card_feats,
        )

    await db.commit()

    rl_pct = round(rl_w / (rl_w + rl_l) * 100, 2) if (rl_w + rl_l) else 0.0
    ou_pct = round(ou_w / (ou_w + ou_l) * 100, 2) if (ou_w + ou_l) else 0.0

    result = {
        "run_line": {"pct": rl_pct, "w": rl_w, "l": rl_l, "push": rl_p},
        "over_under": {"pct": ou_pct, "w": ou_w, "l": ou_l, "push": ou_p},
        "moneyline": {"pct": round(ml_w / (ml_w + ml_l) * 100, 2) if (ml_w + ml_l) else 0.0, "w": ml_w, "l": ml_l, "push": ml_p},
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
        "team_name": _str_safe(row.get("home_team_name", row.get("ha", ""))),
        "abbreviation": _str_safe(row.get("ha", "")),
        "wins": _int_safe(row.get("home_wins", 0)),
        "losses": _int_safe(row.get("home_losses", 0)),
        "pitcher": _str_safe(row.get("h_starter_name", "")),
        "runs_scored_avg": _float_safe(row.get("h_rf_avg", 0.0)),
        "runs_allowed_avg": _float_safe(row.get("h_ra_avg", 0.0)),
        "park_factor": _float_safe(row.get("park_factor", 1.0)),
    }


def _build_mlb_away_stats(row: dict) -> dict:
    """Build away team stats dict from a feature row."""
    return {
        "team_name": _str_safe(row.get("away_team_name", row.get("aa", ""))),
        "abbreviation": _str_safe(row.get("aa", "")),
        "wins": _int_safe(row.get("away_wins", 0)),
        "losses": _int_safe(row.get("away_losses", 0)),
        "pitcher": _str_safe(row.get("a_starter_name", "")),
        "runs_scored_avg": _float_safe(row.get("a_rf_avg", 0.0)),
        "runs_allowed_avg": _float_safe(row.get("a_ra_avg", 0.0)),
        "park_factor": _float_safe(row.get("park_factor", 1.0)),
    }


def _build_mlb_situational(row: dict) -> dict:
    """Build situational data dict from a feature row."""
    roof = _str_safe(row.get("roof_type", "Outdoor")).lower()
    is_dome = "dome" in roof or "retractable" in roof
    is_div = bool(row.get("is_div", False))

    # Rest days
    rest_h = _int_safe(row.get("rest_h"))
    rest_a = _int_safe(row.get("rest_a"))
    rest_diff = _float_safe(row.get("rest_diff"))
    is_short_week = (rest_h is not None and rest_h <= 1) or (rest_a is not None and rest_a <= 1)

    # Travel & timezone
    travel_miles = _int_safe(row.get("travel_miles"))
    tz_diff = _int_safe(row.get("tz_diff"))

    # Travel advantage: whichever team traveled fewer miles (away team usually travels)
    travel_advantage = "Home" if (travel_miles is not None and travel_miles < 300) else None

    # Composite situation score (simple heuristic: rest diff + home + division + travel)
    situation_score = 0
    if rest_diff is not None:
        situation_score += rest_diff  # positive = home more rested
    if is_dome or is_div:
        situation_score += 1
    if travel_miles is not None and travel_miles > 500:
        situation_score += 1  # away team traveled far = home advantage

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
        "is_dome": is_dome,
        "rest_home": rest_h,
        "rest_away": rest_a,
        "rest_diff": rest_diff,
        "travel_miles": travel_miles,
        "tz_diff": tz_diff,
        "is_division": is_div,
        "is_short_week": is_short_week,
        "travel_advantage": travel_advantage,
        "situation_score": situation_score,
        "rest_home_hours": _float_safe(row.get("rest_h_hours")),
        "rest_away_hours": _float_safe(row.get("rest_a_hours")),
        "rest_diff_hours": _float_safe(row.get("rest_diff_hours")),
        "wind_calculated": _float_safe(row.get("wind_calculated")),
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
    pick_card_features: set | None = None,
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

    if (home_score + away_score) == total:
        ou_result = "Push"
    elif pred_over == actual_over:
        ou_result = "Win"
    else:
        ou_result = "Loss"
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

    # Predicted score (inferred from margin + total)
    home_score_raw = (pred_total + pred_margin) / 2.0
    away_score_raw = (pred_total - pred_margin) / 2.0
    predicted_home_score = round(home_score_raw, 1)
    predicted_away_score = round(away_score_raw, 1)

    # Pick text
    home_run_line_val = spread          # home team perspective
    away_run_line_val = -spread          # away team perspective
    if rl_picked_home:
        rl_pick_str = f"{home_team} {home_run_line_val:+g}"
    else:
        rl_pick_str = f"{away_team} {away_run_line_val:+g}"

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
        features_json=_extract_pick_card_features(row, pick_card_features) if pick_card_features else None,
        source="backtest",
        created_at=now,
    )
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(MLBGamePrediction).where(
        MLBGamePrediction.game_id == int(gid),
        MLBGamePrediction.source.in_(["api", "backtest"]),
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
    """No-op: all feature engineering is in ``build_features()`` from
    ``data_loader``.  Kept for backward API compatibility."""
    return df


if __name__ == "__main__":
    import argparse, asyncio
    from app.database import get_db

    parser = argparse.ArgumentParser(description="MLB backtest runner")
    parser.add_argument("--years", nargs="*", default=["2025", "2026"],
                        help="Year(s) to backtest (e.g. 2025 2026)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip games that already have a prediction")
    parser.add_argument("--num-games", type=int, default=None,
                        help="Number of games to evaluate (default: all)")
    args = parser.parse_args()
    years = [int(y) for y in args.years]

    async def _run():
        async for db in get_db():
            for year in years:
                print(f"\n{'='*60}")
                print(f"Backtesting {year}...")
                print(f"{'='*60}")
                result = await backtest_season(db, year, resume=args.resume, num_games=args.num_games or 0)
                print(f"{year} done: {result}")
            break

    asyncio.run(_run())
