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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# App helpers
from app.database import async_session
from app.handicapping.mlb.data_loader import (
    MLBDataLoader, build_features, get_data_loader, get_model_features,
    _GAME_QUERY_COLUMNS,
)
from app.handicapping.calibrate_confidence import calibrate, build_calibration
from app.handicapping.shap_attribution import compute_attribution
from app.models.mlb.consolidated import MLBBettingLineConsolidated

# ── Cached pick-card feature names ──
_PICK_CARD_FEATURE_METADATA: Optional[Dict[str, Dict[str, str]]] = None

async def _load_pick_card_feature_metadata(db) -> Dict[str, Dict[str, str]]:
    """Lazy-load pick_card=true feature metadata: {name: {display_name, description}}."""
    global _PICK_CARD_FEATURE_METADATA
    if _PICK_CARD_FEATURE_METADATA is not None:
        return _PICK_CARD_FEATURE_METADATA
    result = await db.execute(
        text(
            "SELECT name, display_name, description "
            "FROM mlb.features WHERE pick_card = true"
        )
    )
    _PICK_CARD_FEATURE_METADATA = {
        r[0]: {
            "display_name": r[1] or r[0],
            "description": r[2] or "",
        }
        for r in result.fetchall()
    }
    return _PICK_CARD_FEATURE_METADATA


def _extract_pick_card_features(row, feature_metadata: Dict[str, Dict[str, str]]) -> str:
    """Return JSON string of pick_card feature values enriched with display_name
    and description from mlb.features.
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


# ── Builder key → feature name mappings for enrichment ──
_HOME_STATS_FEATURE_MAP = {
    "runs_scored_avg": "h_rf_avg",
    "runs_allowed_avg": "h_ra_avg",
    "park_factor": "park_factor",
}

_AWAY_STATS_FEATURE_MAP = {
    "runs_scored_avg": "a_rf_avg",
    "runs_allowed_avg": "a_ra_avg",
    "park_factor": "park_factor",
}

_SITUATIONAL_FEATURE_MAP = {
    "rest_home": "rest_h",
    "rest_away": "rest_a",
    "rest_diff": "rest_diff",
    "travel_miles": "travel_miles",
    "tz_diff": "tz_diff",
    "is_division": "is_div",
    "rest_home_hours": "rest_h_hours",
    "rest_away_hours": "rest_a_hours",
    "rest_diff_hours": "rest_diff_hours",
    "wind_calculated": "wind_calculated",
    "temperature": "temperature",
    "wind_speed": "wind_speed",
    "wind_direction": "wind_direction",
    "weather_condition": "weather_condition",
    "surface": "surface",
    "venue": "venue",
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


def _model_file_for_year(model_type: str, year: int) -> Optional[str]:
    """Return the basename of the pkl model file for *model_type*/*year*
    (e.g. ``a1b2c3-2026.pkl``), or ``None`` if it cannot be resolved.

    Stored on every pick for model provenance/audit.
    """
    paths = _resolve_year_pkl_paths(model_type)
    p = paths.get(year)
    return p.name if p is not None else None


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


# ── Column projection for load_games ───────────────────────────────────────
# GAME_QUERY projects 291 columns and builds them all via LATERAL joins;
# `load_games(columns=...)` now restricts the SELECT so Postgres skips the
# unselected LATERALs — load time tracks the requested feature set instead of
# all 291. These two helpers compute the exact raw GAME_QUERY aliases a given
# path needs so we never load/build columns the model or pick-card won't touch.
#
# RAW_BUILDFEATURES_INPUTS: the raw GAME_QUERY columns that build_features()
# reads as inputs for its derived columns (e.g. h_win_pct -> h_winpct, h_wins ->
# home_wins, h_p_rest -> h_pitcher_rest). Kept ALWAYS so build_features can
# regenerate every derived feature from the projected frame. Computed by
# scanning build_features for result/df/frame[row] keys; stable — change with the
# function if it starts reading a new raw column.

_BUILDFEATURES_RAW_INPUTS = [
    "h_win_pct", "a_win_pct", "h_win_pct_l10", "a_win_pct_l10",
    "h_wins", "h_losses", "a_wins", "a_losses",
    "h_wins_l10", "h_losses_l10", "a_wins_l10", "a_losses_l10",
    "h_p_rest", "a_p_rest", "h_p_era_20", "a_p_era_20",
    "h_p_era_5", "a_p_era_5", "h_over_count", "a_over_count",
    "h_over_pct", "a_over_pct", "h_at_bats", "a_at_bats",
    "h_prior_rf_home", "a_prior_rf_away", "h_rf", "a_rf",
    "h_cum_ops", "a_cum_ops", "h_lineup_ops", "a_lineup_ops",
    "h_lineup_ops_minus_team", "a_lineup_ops_minus_team",
    "combo_era_r5", "combo_era_r10",
    # raw inputs for build_features-derived combo_era_r5/r10/r15, combo_era_r10_diff
    # (= h_era_10 - a_era_10), era_diff, h/a_combo_era_r15, h/a_cum_era_vs_l5/l10
    # (h/a_era_5/10/15 from trs_h/trs_a rolling stats). combo_era_* are NOT raw
    # GAME_QUERY aliases, so without these raw team rolling-era columns a lean
    # projection silently drops them -> combo_era_r10 falls back to 4.5 and
    # combo_era_r10_diff reads 0.0 for every scheduled game (regression from the
    # 2026-08-21 projection overhaul).
    "h_era_5", "a_era_5", "h_era_10", "a_era_10", "h_era_15", "a_era_15",
    "closing_home_implied_probability", "closing_away_implied_probability",
    # ^ raw blc aliases that build_features derives h_implied/a_implied and
    #   home/away_implied_probability from (in _REQUIRED_ROW_COLUMNS). Without
    #   these raw columns a lean projection silently drops them -> Away/Home
    #   Implied Win Prob reads 0.5000 for every scheduled game (same projection
    #   regression as combo_era_* below).
    # raw inputs for build_features-derived features rest_diff (h_rest/a_rest)
    # and is_div (home_abbr/away_abbr). Without these the derived features stay
    # NaN and any lean projection silently drops them (model trains on fewer
    # features than live inference -> Feature shape mismatch, expected: 101 got 103).
    "h_rest", "a_rest", "home_abbr", "away_abbr",
    # raw inputs for the derived pitcher SPLIT ERA + day/night ERA features
    # (h_pitcher_home_era / *_day_era / *_night_era / *_day_night_era) and the
    # venue ERA. These are build_features-derived, so their raw YTD split
    # sources must always be projected or the derived feature reads NaN.
    "h_p_home_era_ytd", "h_p_road_era_ytd", "h_p_day_era_ytd", "h_p_night_era_ytd",
    "a_p_home_era_ytd", "a_p_road_era_ytd", "a_p_day_era_ytd", "a_p_night_era_ytd",
    "h_pitcher_venue_era", "a_pitcher_venue_era",
    "h_pitcher_venue_starts", "a_pitcher_venue_starts",
    "day_night",
    # raw inputs for the derived bullpen ERA/IP L5 features (bullpen_era_l5 and
    # bullpen_ip_l5) + the prior-year team ERA fallback (never 0).
    "h_bullpen_er_l5", "a_bullpen_er_l5",
    "h_bullpen_ip_l5", "a_bullpen_ip_l5",
    "h_prior_era", "a_prior_era",
    # ALL raw P_ALIASES source columns (h/a_p_* rate columns for 5/10/20/ytd).
    # These feed build_features()'s derived pitcher-rate features
    # (h/a_pitcher_{era,whip,k9}_{l5,l10,l20}, kbb_l10, *_ytd, qs_rate). The
    # derived names are NOT raw GAME_QUERY aliases, so without their raw sources
    # here a lean projection silently drops them -> the LIVE model reads 0/NaN
    # while training reads real values (Feature shape mismatch / drifting picks).
    "h_p_k9_20", "a_p_k9_20", "h_p_whip_20", "a_p_whip_20",
    "h_p_k9_5", "a_p_k9_5", "h_p_whip_5", "a_p_whip_5",
    "h_p_k9_10", "a_p_k9_10", "h_p_whip_10", "a_p_whip_10",
    "h_p_kbb_10", "a_p_kbb_10",
    "h_p_fip_ytd", "a_p_fip_ytd",
    "h_p_era_ytd", "a_p_era_ytd", "h_p_whip_ytd", "a_p_whip_ytd",
    "h_p_k9_ytd", "a_p_k9_ytd", "h_p_bb9_ytd", "a_p_bb9_ytd",
    "h_p_qs_rate_ytd", "a_p_qs_rate_ytd",
    "h_p_starts_ytd", "a_p_starts_ytd",

    # AUDIT 2026-08-23: every remaining raw GAME_QUERY alias that build_features()
    # READS but the 08-21 projection overhaul left out of this list. All 38 are
    # gated in build_features (`if X in result.columns`), so with them unprojected
    # a lean live/training projection silently dropped them -> each derived feature
    # fell back to a constant. Added in one batch:
    #   - L5/L10 rolling team line stats (h/a_avg_5/10, h/a_ops_5/10, h/a_whip_5/10,
    #     h/a_k9_10, h/a_over_pct5) -> h/a_cum_*_vs_l5/l10, h/a_over_freq5
    #   - side-run-factor (h_home_runs_per_game / a_away_runs_per_game) -> h/a_side_rf
    #   - exact rest hours (h/a_prev_game_date) -> rest_h/a_hours
    #   - venue park factor / roof / name / capacity -> park_factor, is_dome,
    #     retractable-roof temp neutralization, venue-winpct
    #   - team/division names (h/a_team_name, adiv, hdiv) -> division features
    #   - starter names (h/a_starter_name) -> pitching matchup display/config
    #   - prior-year rolling fallbacks (h/a_prior_avg/ops/whip) used when a team's
    #     current rolling window is empty
    #   - a_p_era_10 (away starter ERA L10) -> era_diff fallback branch
    "h_avg_5", "a_avg_5", "h_avg_10", "a_avg_10",
    "h_ops_5", "a_ops_5", "h_ops_10", "a_ops_10",
    "h_whip_5", "a_whip_5", "h_whip_10", "a_whip_10",
    "h_k9_10", "a_k9_10", "h_over_pct5", "a_over_pct5",
    "h_home_runs_per_game", "a_away_runs_per_game",
    "h_prev_game_date", "a_prev_game_date",
    "h_prior_avg", "a_prior_avg", "h_prior_ops", "a_prior_ops",
    "h_prior_whip", "a_prior_whip", "h_p_era_10", "a_p_era_10",
    "hdiv", "adiv", "home_team_name", "away_team_name",
    "home_starter_name", "away_starter_name",
    "venue_park_factor", "venue_roof", "venue_name", "venue_capacity",
]

# Context/target columns the prediction + pick-card pipeline reads directly off
# the row (NOT registered as model features, but must always be present).
_REQUIRED_ROW_COLUMNS = [
    "game_id", "season_year", "season_id", "game_date",
    "home_team_id", "away_team_id", "home_team", "away_team",
    "home_score", "away_score",
    "h_line_runline", "a_line_runline",
    "closing_spread", "closing_ou", "over_under", "spread",
    "h_implied_probability", "a_implied_probability",
    "h_moneyline", "a_moneyline", "h_probability", "a_probability",
    "status",
]


def _projection_columns(feature_names: list[str]) -> list:
    """Reduce a feature-name list to the raw GAME_QUERY aliases to load.

    ``feature_names`` may contain build_features-derived names that aren't raw
    GAME_QUERY aliases (e.g. h_winpct) — those are produced later by
    build_features from `_BUILDFEATURES_RAW_INPUTS`, so they are skipped here
    and their inputs are loaded instead. Returns the union of: raw feature
    aliases, the build_features raw inputs, and the required row/context cols,
    restricted to aliases that actually exist in the query (unknowns ignored).
    """
    if not _GAME_QUERY_COLUMNS:
        return None  # never restrict if we can't introspect the query
    needed = set(feature_names) | set(_BUILDFEATURES_RAW_INPUTS) | set(_REQUIRED_ROW_COLUMNS)
    keep = [c for c in _GAME_QUERY_COLUMNS if c in needed]
    return keep if keep else None


async def _inference_feature_names(db: AsyncSession) -> list:
    """Raw GAME_QUERY columns the live inference + pick-card pipeline needs.

    Live inference must load every feature the models consume (live_ats ∪
    live_ou) AND every feature shown on the pick card (pick_card=true), per
    Rich's spec. Only the current year's data is loaded; this helper computes
    which columns to project so build_features + the feature extraction see
    everything they need without materializing unused LATERALs.
    """
    names = set(_get_features()["ats"]) | set(_get_features()["ou"])
    res = await db.execute(text("SELECT name FROM mlb.features WHERE pick_card = true"))
    names |= {r for r in res.scalars().all() if r}
    # Team-abbreviation columns are required downstream: build_features derives
    # `ha`/`aa` from them, and _save_api_prediction needs those for ml_pick /
    # run_line_pick (empty team names => blank picks). They aren't model or
    # pick_card features, so always include them.
    names |= {"home_abbr", "away_abbr"}
    return _projection_columns(sorted(names))


def _extract_feature_vector(row: pd.Series, model_type: str) -> Optional[np.ndarray]:
    """Extract the feature vector of ``model_type`` features from one row.

    Feature columns come from ``mlb.features`` (live_ats/live_ou).
    Returns ``None`` if any required feature is missing or NaN.
    """
    cols = _get_features()[model_type]
    vals = []
    for c in cols:
        v = row.get(c)
        if v is not None and not (isinstance(v, float) and np.isnan(v)):
            vals.append(float(v))
            continue

        # ── Model-path imputation ────────────────────────────────────
        # The raw data layer (build_features) preserves real values / NULL.
        # THIS is the ONLY place missing values become numbers for the model,
        # using a reasoned prior — never a blind 0 that the model could read
        # as "dominant here". The user-facing pick card NEVER sees these fills:
        # it reads the raw row, so a missing stat stays blank/None there.
        #
        # See backend/docs/mlb_imputation_table.md (approved by Rich).
        imputed = _impute_feature(row, c)
        if imputed is not None:
            v = imputed
            logger.debug("  Imputed feature '%s' for game %s — %.3f", c, row.get("game_id"), v)
        else:
            v = 0.0
        vals.append(float(v))
    return np.array(vals, dtype=np.float32)


def _impute_feature(row: pd.Series, c: str) -> Optional[float]:
    """Return the model-side imputed value for a missing feature ``c``.

    Called ONLY from the model extraction path (never for the pick card).
    Returns ``None`` when no better prior exists (caller falls back to 0.0).
    """
    # Pitcher VENUE ERA: missing (no starts at this park) -> the pitcher's
    # home/road season ERA as the closest true prior. Putting 0 here would
    # read to the model as "dominant at this venue", which is wrong.
    if c == "a_pitcher_venue_era":
        return _nanok(row.get("a_p_road_era_ytd"))
    if c == "h_pitcher_venue_era":
        return _nanok(row.get("h_p_home_era_ytd"))

    # Pitcher split ERAs (home/road/day/night): a missing split -> season ytd ERA
    # as the prior. A missing night-split must not read as "0 ERA."
    split = {
        "h_pitcher_home_era": "h_p_era_ytd",
        "h_pitcher_road_era": "h_p_era_ytd",
        "h_pitcher_day_era": "h_p_era_ytd",
        "h_pitcher_night_era": "h_p_era_ytd",
        "h_pitcher_day_night_era": "h_p_era_ytd",
        "a_pitcher_home_era": "a_p_era_ytd",
        "a_pitcher_road_era": "a_p_era_ytd",
        "a_pitcher_day_era": "a_p_era_ytd",
        "a_pitcher_night_era": "a_p_era_ytd",
        "a_pitcher_day_night_era": "a_p_era_ytd",
    }
    if c in split:
        return _nanok(row.get(split[c]))

    # Pitcher rest: missing (unknown / opener) -> league-avg ~4 days, NOT 0
    # (0 would read as "pitched back-to-back").
    if c in ("h_pitcher_rest", "a_pitcher_rest"):
        return 4.0

    # Pitcher K/BB splits (l20/l10): a missing window -> season ytd K/BB
    kbb_l = {
        "h_pitcher_kbb_l20": "h_p_kbb_ytd",
        "h_pitcher_kbb_l10": "h_p_kbb_ytd",
        "a_pitcher_kbb_l20": "a_p_kbb_ytd",
        "a_pitcher_kbb_l10": "a_p_kbb_ytd",
    }
    if c in kbb_l:
        return _nanok(row.get(kbb_l[c]))

    # Team runs-for game (home/away): a truly missing value -> prior-season RF
    # (never 0 -- a 0 reads as 'no offense'). Prior-season already fills the raw
    # row for normal cases; this guards the rare fully-missing case.
    rf_prior = {
        "h_home_rf": "h_prior_rf_home",
        "a_away_rf": "a_prior_rf_away",
        "h_rf": "h_prior_rf",
        "a_rf": "a_prior_rf",
    }
    if c in rf_prior:
        return _nanok(row.get(rf_prior[c]))

    # Bullpen ERA (last-5): missing window -> prior-year team ERA (not 0 and not
    # the old flat 4.5). home bullpen -> h_prior_era, away -> a_prior_era.
    bp_era = {
        "h_bullpen_era_l5": "h_prior_era",
        "a_bullpen_era_l5": "a_prior_era",
    }
    if c in bp_era:
        return _nanok(row.get(bp_era[c]))

    # Weather — use realistic league/season averages, not the old crude 80/50.
    if c in ("temperature", "temp"):
        return 69.0   # league avg MLB temp
    if c in ("humidity",):
        return 55.0
    if c in ("wind_speed", "wind"):
        return 5.0

    return None


def _nanok(v) -> Optional[float]:
    """float(v) if v is a finite number, else None."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None



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

    # ── Guard: only predict for SCHEDULED, not-yet-started games ──
    from sqlalchemy import text as _sa_t
    valid = await db.execute(
        _sa_t(
            "SELECT id FROM mlb.games "
            "WHERE id = ANY(:gids) AND status = 'SCHEDULED' AND date > NOW()"
        ),
        {"gids": list(game_ids)},
    )
    valid_ids = {row[0] for row in valid.fetchall()}
    filtered = [gid for gid in game_ids if gid in valid_ids]
    dropped = len(game_ids) - len(filtered)
    if dropped:
        _logger.warning(f"Dropped {dropped} game(s) from prediction — not SCHEDULED or already started")
    game_ids = filtered
    if not game_ids:
        _logger.info("No games to predict after SCHEDULED/time filter")
        return []

    ats_model = _load_model_for_year("ats", year)
    ou_model = _load_model_for_year("ou", year)
    ats_model_file = _model_file_for_year("ats", year)
    ou_model_file = _model_file_for_year("ou", year)
    _logger.info(
        f"Models loaded for {year} (ats={'loaded' if ats_model else 'none'}, "
        f"ou={'loaded' if ou_model else 'none'}, "
        f"ats_pkl={ats_model_file or '?'}, ou_pkl={ou_model_file or '?'})"
    )

    dl = get_data_loader()
    # Live inference: load ONLY the current year. Rolling/Prior-season/lineup-OPS
    # features are computed PER ROW by the GAME_QUERY LATERALs (rolling stats scan
    # the full DB history; prior-season averages come from `mlb.prior_team_stats`
    # joined by s.year-1; lineup-OPS reads player_batting_rolling_stats per starter).
    # Each target row is self-contained, so NO prior-season rows need to be loaded
    # into the DataFrame. The loaded year follows the model's `year` param, so
    # changing the year changes what is loaded.
    load_seasons = [year]
    # Live inference: load ONLY the current year, projected to just the raw
    # GAME_QUERY columns the models + pick-card need (live_ats ∪ live_ou ∪
    # pick_card=true) plus build_features/context inputs. Restricting the SELECT
    # lets Postgres skip the unselected LATERALs -> inference load is fast.
    infer_cols = await _inference_feature_names(db)
    # Live inference: we ONLY need the target (upcoming) games. The GAME_QUERY
    # LATERALs compute every rolling/prior/lineup feature PER ROW from the rolling
    # tables + prior-season stats, so each target row is self-contained.
    # Loading the full season's FINAL rows here did nothing but feed a
    # 20-minute-statement-timeout GAME_QUERY (games ended up with NO picks).
    # The picks loop below only ever uses df filtered to the target game_ids,
    # so all_historic is dead weight - skip it entirely.
    target_games = dl.load_games(
        seasons=load_seasons, status=None, include_upcoming=True,
        game_ids=game_ids, columns=infer_cols,
    )
    combined = pd.concat(
        [pd.DataFrame(columns=target_games.columns), target_games],
        ignore_index=True,
    )
    _logger.debug(
        f"Seasons loaded for inference: {load_seasons} "
        f"(historic 0 skipped + target {len(target_games)} rows)"
    )
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

            pic_feats = await _load_pick_card_feature_metadata(db)  # lazy-cached

            # SHAP attribution: which features drove this prediction
            shap_info = {}
            try:
                ats_names = _get_features()["ats"]
                ou_names = _get_features()["ou"]
                if ats_model is not None and ats_feats is not None:
                    shap_info["ats"] = compute_attribution(
                        ats_model, ats_feats[np.newaxis, :], ats_names, pic_feats
                    )
                if ou_model is not None and ou_feats is not None:
                    shap_info["ou"] = compute_attribution(
                        ou_model, ou_feats[np.newaxis, :], ou_names, pic_feats
                    )
            except Exception as exc:
                _logger.warning(f"SHAP attribution failed for game {gid}: {exc}")

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
                pick_card_features_meta=pic_feats,
                shap_info=shap_info,
                ats_model_file=ats_model_file,
                ou_model_file=ou_model_file,
            )

            # Commit after EACH game so the DELETE+INSERT row locks on
            # mlb.game_predictions are released immediately instead of being
            # held for the whole batch. Prevents the shared Postgres from being
            # locked (stalling api-box readers) while the batch is still running.
            await db.commit()

            pick_results.append(
                {
                    "game_id": gid,
                    "predicted_margin": round(pred_margin, 2),
                    "predicted_total": round(pred_total, 2),
                    "pred_home_covers": pred_home_covers,
                    "pred_over": pred_over,
                    "pred_home_wins": pred_home_wins,
                    "shap_info": shap_info if shap_info else None,
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
    pick_card_features_meta: Dict[str, Dict[str, str]] | None = None,
    shap_info: Dict[str, Any] | None = None,
    ats_model_file: str | None = None,
    ou_model_file: str | None = None,
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

    # Calibrate confidence against empirical win rate
    # Raw confidence (used by Predictions page)
    rl_conf = round(min(0.5 + abs(pred_margin + spread) * 0.04, 0.90), 4) if spread else 0.5
    ml_conf = round(min(0.5 + abs(pred_margin) * 0.025, 0.92), 4)
    ou_conf_diff = abs(pred_total - total) if pred_total is not None and total else None
    ou_conf = round(min(0.5 + ou_conf_diff * 0.07, 0.92), 4) if ou_conf_diff is not None else 0.5
    # Calibrated confidence (used for EV calculation)
    rl_conf_cal = calibrate(rl_conf, "ats", "mlb")
    ml_conf_cal = calibrate(ml_conf, "ml", "mlb")
    ou_conf_cal = calibrate(ou_conf, "ou", "mlb")

    # EV at $100 stake
    def _ev(conf_: float, odds_: float) -> float:
        profit_if_win = 100.0 * _profit_per_100(odds_)
        return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

    ats_ev = _ev(rl_conf_cal, rl_odds) if rl_odds else 0.0
    ou_ev = _ev(ou_conf_cal, ou_odds) if ou_odds else 0.0
    ml_ev = _ev(ml_conf_cal, ml_odds) if ml_odds else 0.0

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

    _row_dict = dict(row)

    gp = MLBGamePrediction(
        game_id=int(gid),
        predicted_home_runs=predicted_home_score,
        predicted_away_runs=predicted_away_score,
        predicted_total=round(pred_total, 2),
        predicted_margin=round(pred_margin, 2),
        ou_pick="Over" if ou_picked_over else "Under",
        run_line_pick=rl_pick_str,
        ml_pick=home_team if ml_picked_home else away_team,
        rl_conf=round(rl_conf, 4),
        ou_conf=round(ou_conf, 4),
        ml_conf=round(ml_conf, 4),
        rl_conf_cal=round(rl_conf_cal, 4),
        ml_conf_cal=round(ml_conf_cal, 4),
        ou_conf_cal=round(ou_conf_cal, 4),
        ats_ev=ats_ev,
        ou_ev=ou_ev,
        ml_ev=ml_ev,
        ats_odds=int(round(rl_odds)),
        ou_odds=int(round(ou_odds)),
        ml_odds=int(round(ml_odds)),
        home_stats_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_home_stats(_row_dict),
                _HOME_STATS_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        away_stats_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_away_stats(_row_dict),
                _AWAY_STATS_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        situational_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_situational(_row_dict),
                _SITUATIONAL_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        splits_json=json.dumps(_build_mlb_splits(_row_dict)),
        features_json=_extract_pick_card_features(row, pick_card_features_meta) if pick_card_features_meta else None,
        shap_json=json.dumps(shap_info, default=str) if shap_info else None,
        ats_model_file=ats_model_file,
        ou_model_file=ou_model_file,
        source="api",
        created_at=now,
    )
    db.add(gp)
    await db.flush()
    return 1


async def _backtest_single_season(
    db: AsyncSession,
    year: int,
    resume: bool = False,
    num_games: int = 10,
    curve_data: dict = None,
) -> Dict[str, Any]:
    """Backtest MLB models over a single season using year-specific pkl files.

    Called internally by ``_backtest_season_inner`` for each year in the
    multi-year backtest loop.

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

    ats_model_file = _model_file_for_year("ats", year)
    ou_model_file = _model_file_for_year("ou", year)
    logger.info(
        "Backtest models for %s: ats_pkl=%s ou_pkl=%s",
        year, ats_model_file, ou_model_file,
    )

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

        # ── Skip games without a full set of betting lines/odds ──
        # "Full set" for MLB: spread (run line), over_under (total),
        # and moneyline (home + away).  spread/over_under are NaN
        # when the corresponding line is missing; moneyline columns
        # are filled to 0.0 by build_features (0 is invalid American odds).
        _sp = row.get("spread")
        _ou = row.get("over_under")
        _hml = row.get("home_moneyline", 0) or 0
        _aml = row.get("away_moneyline", 0) or 0
        if (
            pd.isna(_sp) or pd.isna(_ou)
            or _hml == 0.0 or _aml == 0.0
        ):
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
        pick_card_feats = await _load_pick_card_feature_metadata(db)

        # SHAP attribution: which features drove this prediction
        shap_info = {}
        try:
            ats_names = _get_features()["ats"]
            ou_names = _get_features()["ou"]
            if ats_model is not None and feats_ats is not None:
                shap_info["ats"] = compute_attribution(
                    ats_model, feats_ats[np.newaxis, :], ats_names, pick_card_feats
                )
            if ou_model is not None and feats_ou is not None:
                shap_info["ou"] = compute_attribution(
                    ou_model, feats_ou[np.newaxis, :], ou_names, pick_card_feats
                )
        except Exception as exc:
            _logger.warning(f"SHAP attribution failed (backtest) for game {row.get('game_id')}: {exc}")

        saved += await _save_backtest_prediction(
            db, row, year,
            home_score, away_score, spread, total,
            pred_margin, pred_total, pred_home_covers, pred_over, pred_home_wins,
            home_covers, actual_over, home_wins,
            pick_card_features_meta=pick_card_feats,
            curve_data=curve_data,
            shap_info=shap_info,
            ats_model_file=ats_model_file,
            ou_model_file=ou_model_file,
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
    pick_card_features_meta: Dict[str, Dict[str, str]] | None = None,
    curve_data: dict = None,
    shap_info: Dict[str, Any] | None = None,
    ats_model_file: str | None = None,
    ou_model_file: str | None = None,
) -> int:
    """Save a single game\'s prediction to ``mlb.game_predictions``.

    Computes profit, confidence, and EV for each pick (run-line, OU,
    moneyline) using real odds from the betting lines, $100 per bet.
    """
    gid = str(row.get("game_id", ""))
    home_team = str(row.get("ha", ""))
    away_team = str(row.get("aa", ""))
    margin = home_score - away_score

    now = datetime.now(timezone.utc)

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
    # Calibrate confidence against empirical win rate
    # Raw confidence (used by Predictions page)
    rl_conf = round(min(0.5 + abs(pred_margin + spread) * 0.04, 0.90), 4) if spread else 0.5
    ml_conf = round(min(0.5 + abs(pred_margin) * 0.025, 0.92), 4)
    ou_conf_diff = abs(pred_total - total) if pred_total is not None and total else None
    ou_conf = round(min(0.5 + ou_conf_diff * 0.07, 0.92), 4) if ou_conf_diff is not None else 0.5
    # Calibrated confidence (used for EV calculation)
    # First backtest year (2021): no prior-season data → calibrated columns are
    # left NULL so the DB has no calibrated values for those games.
    # calibrate() without curve_data falls back to the file cache, so we pass
    # it explicitly for all subsequent years.
    if curve_data is not None:
        rl_conf_cal = calibrate(rl_conf, "ats", "mlb", curve_data=curve_data)
        ml_conf_cal = calibrate(ml_conf, "ml", "mlb", curve_data=curve_data)
        ou_conf_cal = calibrate(ou_conf, "ou", "mlb", curve_data=curve_data)
    else:
        rl_conf_cal = None
        ml_conf_cal = None
        ou_conf_cal = None
    overall_conf = max(rl_conf, ou_conf, ml_conf)

    # EV at $100 stake
    def _ev(conf_: float, odds_: float) -> float:
        profit_if_win = 100.0 * _profit_per_100(odds_)
        return round((conf_ * profit_if_win) - ((1.0 - conf_) * 100.0), 2)

    ats_ev = _ev(rl_conf_cal if rl_conf_cal is not None else rl_conf, rl_odds)
    ou_ev = _ev(ou_conf_cal if ou_conf_cal is not None else ou_conf, ou_odds)
    ml_ev = _ev(ml_conf_cal if ml_conf_cal is not None else ml_conf, ml_odds)

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
    _row_dict = dict(row)

    gp = MLBGamePrediction(
        game_id=int(gid),
        predicted_home_runs=predicted_home_score,
        predicted_away_runs=predicted_away_score,
        predicted_total=round(pred_total, 2),
        predicted_margin=round(pred_margin, 2),
        ou_pick="Over" if ou_picked_over else "Under",
        run_line_pick=rl_pick_str,
        ml_pick=home_team if ml_picked_home else away_team,
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
        rl_conf_cal=round(rl_conf_cal, 4) if rl_conf_cal is not None else None,
        ml_conf_cal=round(ml_conf_cal, 4) if ml_conf_cal is not None else None,
        ou_conf_cal=round(ou_conf_cal, 4) if ou_conf_cal is not None else None,
        ats_ev=ats_ev,
        ou_ev=ou_ev,
        ml_ev=ml_ev,
        home_stats_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_home_stats(_row_dict),
                _HOME_STATS_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        away_stats_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_away_stats(_row_dict),
                _AWAY_STATS_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        situational_json=json.dumps(
            _enrich_dict_with_metadata(
                _build_mlb_situational(_row_dict),
                _SITUATIONAL_FEATURE_MAP, pick_card_features_meta,
            )
        ),
        splits_json=json.dumps(_build_mlb_splits(_row_dict)),
        features_json=_extract_pick_card_features(row, pick_card_features_meta) if pick_card_features_meta else None,
        shap_json=json.dumps(shap_info, default=str) if shap_info else None,
        ats_model_file=ats_model_file,
        ou_model_file=ou_model_file,
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


def _zeros_return() -> Dict[str, Any]:
    """Return an empty results dict matching ``_backtest_single_season`` return shape."""
    return {"run_line": {"pct": 0.0, "w": 0, "l": 0, "push": 0},
            "over_under": {"pct": 0.0, "w": 0, "l": 0, "push": 0},
            "moneyline": {"pct": 0.0, "w": 0, "l": 0, "push": 0}}


def _compat_build_extra_features(df: pd.DataFrame) -> pd.DataFrame:
    """No-op: all feature engineering is in ``build_features()`` from
    ``data_loader``.  Kept for backward API compatibility."""
    return df


async def _backtest_season_inner(
    db: AsyncSession,
    years: Optional[List[int]] = None,
    limit: Optional[int] = None,
    save_results: bool = True,
) -> Dict[str, Any]:
    """Multi-year backtest with cumulative calibration curves.

    Models are year-specific pkl files.  The first year (2021) uses raw
    confidence only (no prior-season data to calibrate with).  From 2022
    onward we build a calibration curve from all prior seasons and
    apply it to the current year's predictions.

    At the end we save the final calibration curve to
    ``{sport}_confidence_calibration.json`` so live API predictions use it.
    """
    from sqlalchemy import text

    if years is None:
        # 2021 uses raw confidence (no prior data). From 2022 onward we
        # build a calibration curve from all prior-season predictions.
        years = [2021, 2022, 2023, 2024, 2025, 2026]

    total_game_preds = 0
    first_test_year = min(years)

    for year in years:
        logger.info("\n========== Backtesting MLB %d ==========", year)

        # Build calibration curve from ALL prior seasons
        if year > first_test_year:
            try:
                logger.info("  Building MLB calibration curve from seasons before %d...", year)
                curve_data = await build_calibration(db, "mlb", max_exclusive_season=year, skip_file_save=True)
            except Exception as e:
                logger.warning("  Could not build MLB calibration curve for %d: %s (using raw conf)", year, e)
                await db.rollback()
                curve_data = None
        else:
            curve_data = None

        result = await _backtest_single_season(
            db, year, resume=False, num_games=limit or 0,
            curve_data=curve_data,
        )

        # Extract total game count from individual result counters
        rl = result.get("run_line", {})
        ou = result.get("over_under", {})
        year_total = rl.get("w", 0) + rl.get("l", 0) + rl.get("push", 0)
        total_game_preds += year_total
        logger.info("  %d predictions saved for %d", year_total, year)

    # Save final calibration curve for live API predictions
    try:
        logger.info("Building final MLB calibration curve from all backtest years...")
        await build_calibration(db, "mlb", skip_file_save=False)
    except Exception as e:
        logger.warning("Could not build final MLB calibration curve: %s", e)

    logger.info("Backtest complete: %d years, %d total predictions",
                len(years), total_game_preds)
    return {"run_line": {}, "over_under": {}, "moneyline": {}, "total": total_game_preds}


async def backtest_season(
    years: Optional[List[int]] = None,
    limit: Optional[int] = None,
    save_results: bool = True,
    db: Optional[async_sessionmaker] = None,
) -> Dict[str, Any]:
    """Backtest MLB models across one or more seasons.

    Wrapper that creates an AsyncSession if none is given, then delegates
    to ``_backtest_season_inner`` which handles cumulative calibration curves.
    """
    if db is None:
        async with async_session() as own_db:
            return await _backtest_season_inner(own_db, years, limit, save_results)
    async with db() as session:
        return await _backtest_season_inner(session, years, limit, save_results)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    import argparse, asyncio
    from app.database import async_session

    parser = argparse.ArgumentParser(description="MLB backtest runner")
    parser.add_argument("--years", nargs="*", default=["2021", "2022", "2023", "2024", "2025", "2026"],
                        help="Year(s) to backtest (e.g. 2021 2022 2023)")
    parser.add_argument("--num-games", type=int, default=None,
                        help="Number of games to evaluate (default: all)")
    args = parser.parse_args()
    years = [int(y) for y in args.years]

    async def _run():
        result = await backtest_season(years=years, limit=args.num_games)
        total = result.get("total", 0)
        print(f"\nBacktest complete: {len(years)} years, {total} total predictions")

    asyncio.run(_run())
