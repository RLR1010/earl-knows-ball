"""
NFL Data Loader — feature engineering and dataset creation for the NFL prediction engine.

Loads raw NFL game data from the database, builds rolling / derived features
that match the feature names registered in ``nfl.features``, and packages
them into a DataFrame ready for XGBoost training or inference.

Design mirrors ``mlb/data_loader.py`` but adapted for the weekly, team-based
NFL betting environment (no pitchers, no daily splits).
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2


def _compute_streak(values: np.ndarray) -> int:
    """Compute the current streak from a sliding window of values.

    Positive values = winning streak, negative values = losing streak.
    Streak continues until sign changes or we hit a zero/push.
    """
    if len(values) == 0:
        return 0
    vals = np.asarray(values)
    streak = 0
    for v in reversed(vals):
        if pd.isna(v):
            break
        if v > 0:
            if streak >= 0:
                streak += 1
            else:
                break
        elif v < 0:
            if streak <= 0:
                streak -= 1
            else:
                break
        else:
            break
    return streak
import psycopg2.extras
from math import asin, cos, radians, sin, sqrt

logger = logging.getLogger(__name__)

# ── Database connection ────────────────────────────────────────────────────────
# Single source of truth via db_urls — avoids hardcoded passwords and +asyncpg issues.
from app.db_urls import PSYCOPG2_DATABASE_URL

DEFAULT_DB_URL: str = PSYCOPG2_DATABASE_URL


# ── Team home-stadium coordinates (for travel-distance computations) ────────────
# Latitude / longitude of each NFL team's home stadium.
TEAM_LOCATIONS: Dict[str, Tuple[float, float]] = {
    "ARI": (33.5273, -112.2625),  # State Farm Stadium
    "ATL": (33.7551, -84.4018),  # Mercedes-Benz Stadium
    "BAL": (39.2779, -76.6226),  # M&T Bank Stadium
    "BUF": (42.7737, -78.7870),  # Highmark Stadium
    "CAR": (35.2258, -80.8528),  # Bank of America Stadium
    "CHI": (41.8622, -87.6168),  # Soldier Field
    "CIN": (39.0954, -84.5161),  # Paycor Stadium
    "CLE": (41.5061, -81.6995),  # Huntington Bank Field
    "DAL": (32.7473, -97.0924),  # AT&T Stadium
    "DEN": (39.7439, -105.0201),  # Empower Field at Mile High
    "DET": (42.3400, -83.0459),  # Ford Field
    "GB": (44.5014, -88.0622),  # Lambeau Field
    "HOU": (29.6847, -95.4107),  # NRG Stadium
    "IND": (39.7600, -86.1638),  # Lucas Oil Stadium
    "JAX": (30.3239, -81.6373),  # EverBank Stadium
    "KC": (39.0489, -94.4839),  # GEHA Field at Arrowhead Stadium
    "LAC": (33.8635, -118.2611),  # SoFi Stadium
    "LAR": (33.8635, -118.2611),  # SoFi Stadium
    "LV": (36.0907, -115.1833),  # Allegiant Stadium
    "MIA": (25.9580, -80.2389),  # Hard Rock Stadium
    "MIN": (44.9736, -93.2580),  # U.S. Bank Stadium
    "NE": (42.0909, -71.2644),  # Gillette Stadium
    "NO": (29.9509, -90.0812),  # Caesars Superdome
    "NYG": (40.8135, -74.0744),  # MetLife Stadium
    "NYJ": (40.8135, -74.0744),  # MetLife Stadium
    "PHI": (39.9008, -75.1675),  # Lincoln Financial Field
    "PIT": (40.4466, -80.0158),  # Acrisure Stadium
    "SEA": (47.5952, -122.3316),  # Lumen Field
    "SF": (37.4032, -121.9698),  # Levi's Stadium
    "TB": (27.9759, -82.5033),  # Raymond James Stadium
    "TEN": (36.1663, -86.7713),  # Nissan Stadium
    "WAS": (38.9076, -77.0096),  # Northwest Stadium
}

# Cache for preloaded team locations
_location_cache: Dict[str, Tuple[float, float]] = {}


# ── Helpers ─────────────────────────────────────────────────────────────────────
def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles between two latitude/longitude points."""
    R: float = 3958.8  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


def rolling_mean_safe(series: pd.Series, window: int, min_periods: int = 1) -> pd.Series:
    """Rolling mean that gracefully handles short windows at the start of a season."""
    return series.rolling(window=window, min_periods=min_periods).mean()


# ── GAME_QUERY ──────────────────────────────────────────────────────────────────
# Pulls every raw row we need for feature engineering: game metadata,
# consolidated betting lines, score results.
GAME_QUERY: str = """

WITH game_lines AS (
    SELECT
        bl.game_id,
        bl.closing_spread,
        bl.closing_ou,
        bl.closing_home_ml,
        bl.closing_away_ml,
        bl.opening_spread,
        bl.opening_ou,
        bl.opening_home_ml,
        bl.opening_away_ml,
        bl.opening_over_odds,
        bl.opening_under_odds,
        bl.closing_over_odds,
        bl.closing_under_odds,
        bl.closing_spread_home_odds,
        bl.closing_spread_away_odds,
        bl.opening_spread_home_odds,
        bl.opening_spread_away_odds,
        bl.closing_home_implied_probability,
        bl.closing_away_implied_probability,
        bl.has_verified_ou
    FROM nfl.betting_lines_consolidated bl
    WHERE bl.closing_spread IS NOT NULL
      AND bl.closing_ou IS NOT NULL
),
team_games AS (
    SELECT id AS game_id, home_team_id AS team_id, date
    FROM nfl.games WHERE status = 'FINAL'
    UNION ALL
    SELECT id AS game_id, away_team_id AS team_id, date
    FROM nfl.games WHERE status = 'FINAL'
),
team_schedule AS (
    SELECT game_id, team_id, date,
        LAG(date) OVER (
            PARTITION BY team_id ORDER BY date, game_id
        ) AS last_game_date
    FROM team_games
),
game_rest AS (
    SELECT
        g.id AS game_id,
        hlg.last_game_date AS home_last_game,
        alg.last_game_date AS away_last_game
    FROM nfl.games g
    LEFT JOIN team_schedule hlg
        ON hlg.game_id = g.id AND hlg.team_id = g.home_team_id
    LEFT JOIN team_schedule alg
        ON alg.game_id = g.id AND alg.team_id = g.away_team_id
),
-- Each team's primary home venue (most common venue for their games as home team)
team_primary_venue AS (
    SELECT DISTINCT ON (g.home_team_id)
        g.home_team_id AS team_id,
        g.venue_id
    FROM nfl.games g
    WHERE g.venue_id IS NOT NULL
    GROUP BY g.home_team_id, g.venue_id
    ORDER BY g.home_team_id, COUNT(*) DESC
)
SELECT
    g.id                                                   AS game_id,
    g.season_id,
    g.week,
    g.game_type,
    g.status,
    g.date                                                 AS game_date,
    g.home_team_id,
    g.away_team_id,
    ht.abbreviation                                        AS home_abbr,
    at.abbreviation                                        AS away_abbr,
    ht.conference                                          AS home_conf,
    at.conference                                          AS away_conf,
    ht.division                                            AS home_div,
    at.division                                            AS away_div,
    g.home_score,
    g.away_score,
    g.venue,
    g.surface,
    g.roof_type,
    g.temperature,
    g.wind_speed,
    g.weather_condition,
    gl.closing_spread,
    gl.closing_ou,
    gl.closing_home_ml,
    gl.closing_away_ml,
    gl.opening_spread,
    gl.opening_ou,
    gl.opening_home_ml,
    gl.opening_away_ml,
    gl.opening_over_odds,
    gl.opening_under_odds,
    gl.closing_over_odds,
    gl.closing_under_odds,
    gl.closing_spread_home_odds,
    gl.closing_spread_away_odds,
    gl.opening_spread_home_odds,
    gl.opening_spread_away_odds,
    gl.closing_home_implied_probability,
    gl.closing_away_implied_probability,
    s.year                                                 AS season_year,
    gl.has_verified_ou,
    gr.home_last_game,
    gr.away_last_game,
    v_game.latitude                                         AS venue_lat,
    v_game.longitude                                        AS venue_lng,
    v_game.timezone                                         AS venue_tz,
    v_away.latitude                                         AS away_home_lat,
    v_away.longitude                                        AS away_home_lng,
    v_away.timezone                                         AS away_home_tz,
    -- Haversine: game venue to away team's home venue (~miles)
    ROUND(3959 * 2 * ASIN(SQRT(
        POWER(SIN(RADIANS(v_game.latitude - v_away.latitude) / 2), 2)
        + COS(RADIANS(v_game.latitude)) * COS(RADIANS(v_away.latitude))
        * POWER(SIN(RADIANS(v_game.longitude - v_away.longitude) / 2), 2)
    ))::numeric, 1)                                        AS travel_miles,
    -- Timezone diff (absolute hours)
    ABS(COALESCE(v_game.timezone, '0')::int - COALESCE(v_away.timezone, '0')::int) AS tz_diff,
    -- Team weather-aware stats (derived): pick cold if <40F else warm; precip if raining else dry
    CASE WHEN g.temperature < 40 THEN h_tbw.cold_ppg ELSE h_tbw.warm_ppg END       AS home_team_cold_warm_ppg,
    CASE WHEN g.temperature < 40 THEN h_tbw.cold_ypg ELSE h_tbw.warm_ypg END       AS home_team_cold_warm_ypg,
    CASE WHEN g.temperature < 40 THEN h_tbw.cold_win_pct ELSE h_tbw.warm_win_pct END AS home_team_cold_warm_win_pct,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN h_tbw.precip_ppg ELSE h_tbw.dry_ppg END       AS home_team_precip_dry_ppg,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN h_tbw.precip_ypg ELSE h_tbw.dry_ypg END       AS home_team_precip_dry_ypg,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN h_tbw.precip_win_pct ELSE h_tbw.dry_win_pct END AS home_team_precip_dry_win_pct,
    CASE WHEN g.temperature < 40 THEN a_tbw.cold_ppg ELSE a_tbw.warm_ppg END       AS away_team_cold_warm_ppg,
    CASE WHEN g.temperature < 40 THEN a_tbw.cold_ypg ELSE a_tbw.warm_ypg END       AS away_team_cold_warm_ypg,
    CASE WHEN g.temperature < 40 THEN a_tbw.cold_win_pct ELSE a_tbw.warm_win_pct END AS away_team_cold_warm_win_pct,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN a_tbw.precip_ppg ELSE a_tbw.dry_ppg END       AS away_team_precip_dry_ppg,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN a_tbw.precip_ypg ELSE a_tbw.dry_ypg END       AS away_team_precip_dry_ypg,
    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
         THEN a_tbw.precip_win_pct ELSE a_tbw.dry_win_pct END AS away_team_precip_dry_win_pct
FROM nfl.games g
JOIN nfl.teams ht ON ht.id = g.home_team_id
JOIN nfl.teams at ON at.id = g.away_team_id
LEFT JOIN game_lines gl ON gl.game_id = g.id
LEFT JOIN game_rest gr ON gr.game_id = g.id
LEFT JOIN nfl.seasons s ON s.id = g.season_id
LEFT JOIN nfl.venues v_game ON v_game.id = g.venue_id
LEFT JOIN team_primary_venue tpv ON tpv.team_id = g.away_team_id
LEFT JOIN nfl.venues v_away ON v_away.id = tpv.venue_id

-- Team bad-weather situational stats (leak-free, prior games; this game's row
-- holds each team's cold/warm/precip/dry stats from prior games)
LEFT JOIN nfl.team_badweather_stats h_tbw
    ON h_tbw.team_abbr = ht.abbreviation
   AND h_tbw.feeds_into_game_id = g.id
LEFT JOIN nfl.team_badweather_stats a_tbw
    ON a_tbw.team_abbr = at.abbreviation
   AND a_tbw.feeds_into_game_id = g.id
WHERE g.season_id IS NOT NULL
  AND g.week IS NOT NULL
ORDER BY g.season_id, g.week, g.date;
"""


# ── Features Catalog ────────────────────────────────────────────────────────────
# Maps every feature name (as stored in nfl.features) to a human description.
# Populated from the database on first loader use; the static dict below is a
# fallback / documentation cache.

# ── Feature Aliases ─────────────────────────────────────────────────────────────
# Alternative names / synonyms for features, shown in the admin data-loader UI.
FEATURE_ALIASES: Dict[str, List[str]] = {
    "home_score": ["HF"],
    "away_score": ["AF"],
    "opening_spread": ["Open", "Open Line"],
    "closing_spread": ["Close", "Closing Line"],
    "opening_ou": ["Open OU"],
    "closing_ou": ["Close OU"],
    "opening_home_ml": ["Open ML"],
    "closing_home_ml": ["Close ML"],
    "closing_home_implied_probability": ["Close Home Imp%", "Implied %"],
    "closing_away_implied_probability": ["Close Away Imp%"],
    "closing_spread_home_odds": ["Close Sprd H", "CSHO"],
    "closing_spread_away_odds": ["Close Sprd A", "CSAO"],
    "opening_spread_home_odds": ["Open Sprd H", "OSHO"],
    "opening_spread_away_odds": ["Open Sprd A", "OSAO"],
    "opening_over_odds": ["Open O Odds"],
    "opening_under_odds": ["Open U Odds"],
    "closing_over_odds": ["Close O Odds"],
    "closing_under_odds": ["Close U Odds"],
    "is_dome": ["Dome Game", "Indoor"],
    "rest_diff": ["Rest Advantage"],
    "travel_miles": ["Travel Dist"],
    "is_division_game": ["Div Game"],
    "is_primetime": ["Primetime", "SNF/MNF"],
    "is_short": ["Short Week", "Thu Game"],
    "implied_public_bet_pct": ["Public Bet %"],
    "line_movement_spread_pct": ["Sprd MV %"],
    "line_movement_ou_pct": ["OU MV %"],
    "home_rest_days": ["Home Rest"],
    "away_rest_days": ["Away Rest"],
    "season_year": ["Year"],
    "home_abbr": ["Home"],
    "away_abbr": ["Away"],
    "venue": ["Stadium"],
    "surface": ["Field", "Turf"],
    "roof_type": ["Roof"],
    "temperature": ["Temp"],
    "wind_speed": ["Wind"],
    "weather_condition": ["Conditions"],
    "home_score_margin": ["Home Margin", "Point Diff"],
    "over_result": ["Over Hit?"],
    "home_ats_cover": ["ATS Cover H"],
    "away_ats_cover": ["ATS Cover A"],
    "spread_movement": ["Spread MV"],
    "ou_movement": ["OU MV"],
    "has_verified_ou": ["Verified OU"],
    "hpf": ["Points For H"],
    "hpa": ["Points Against H"],
    "apf": ["Points For A"],
    "apa": ["Points Against A"],
}


# Columns produced by merge of team_rolling_stats (via _ts DataFrame) in build_features.
# These come from the DB, not from feature engineering logic, so the admin data-loader
# UI should group them as "team_stats" instead of "computed".
TEAM_STATS_OUTPUT_COLUMNS: set[str] = {
    # home offensive (home_off)
    "home_off_ypg", "home_ypp", "home_pass_ypg", "home_rush_ypg",
    "home_pass_ypa", "home_rush_ypa", "home_turnover_diff_r5",
    "home_first_downs", "home_third_down_pct", "home_fourth_down_pct",
    "home_rz_trips", "home_rz_td_pct", "home_explosive_plays",
    "home_three_and_outs", "home_ints_thrown", "home_off_epa_per_play",
    "home_win_streak", "home_off_pts_stddev_5", "home_off_yds_stddev_5",
    "home_rw_off_ppg", "home_rw_off_ypg", "home_adj_off_ppg",
    "home_adj_off_ypg", "home_off_yardage_rank", "home_off_scoring_rank",
    "home_off_rushing_rank", "home_off_passing_rank",
    # home defensive (home_def)
    "home_def_ypg", "home_def_ypp", "home_def_pass_ypg",
    "home_def_rush_ypg", "home_def_first_downs", "home_def_third_down_pct",
    "home_def_fourth_down_pct", "home_def_rz_trips", "home_def_rz_td_pct",
    "home_def_explosive_plays", "home_def_three_and_outs",
    "home_def_ints_thrown", "home_def_epa_per_play",
    "home_def_pts_stddev_5", "home_def_yds_stddev_5",
    "home_rw_def_ppg", "home_rw_def_ypg", "home_adj_def_ppg",
    "home_adj_def_ypg", "home_def_yardage_rank", "home_def_scoring_rank",
    "home_def_rushing_rank", "home_def_passing_rating_rank",
    # away offensive (away_off)
    "away_off_ypg", "away_ypp", "away_pass_ypg", "away_rush_ypg",
    "away_pass_ypa", "away_rush_ypa", "away_turnover_diff_r5",
    "away_first_downs", "away_third_down_pct", "away_fourth_down_pct",
    "away_rz_trips", "away_rz_td_pct", "away_explosive_plays",
    "away_three_and_outs", "away_ints_thrown", "away_off_epa_per_play",
    "away_win_streak", "away_off_pts_stddev_5", "away_off_yds_stddev_5",
    "away_rw_off_ppg", "away_rw_off_ypg", "away_adj_off_ppg",
    "away_adj_off_ypg", "away_off_yardage_rank", "away_off_scoring_rank",
    "away_off_rushing_rank", "away_off_passing_rank",
    # away defensive (away_def)
    "away_def_ypg", "away_def_ypp", "away_def_pass_ypg",
    "away_def_rush_ypg", "away_def_first_downs", "away_def_third_down_pct",
    "away_def_fourth_down_pct", "away_def_rz_trips", "away_def_rz_td_pct",
    "away_def_explosive_plays", "away_def_three_and_outs",
    "away_def_ints_thrown", "away_def_epa_per_play",
    "away_def_pts_stddev_5", "away_def_yds_stddev_5",
    "away_rw_def_ppg", "away_rw_def_ypg", "away_adj_def_ppg",
    "away_adj_def_ypg", "away_def_yardage_rank", "away_def_scoring_rank",
    "away_def_rushing_rank", "away_def_passing_rating_rank",

    # ── Rolling game-result stats (pre-computed in team_rolling_stats) ──
    "home_win_pct_r3", "away_win_pct_r3",
    "home_win_pct_r5", "away_win_pct_r5",
    "home_win_pct_r10", "away_win_pct_r10",
    "home_margin_r3", "away_margin_r3",
    "home_margin_r5", "away_margin_r5",
    "home_margin_r10", "away_margin_r10",
    "home_cover_pct_r3", "away_cover_pct_r3",
    "home_cover_pct_r5", "away_cover_pct_r5",
    "home_cover_pct_r10", "away_cover_pct_r10",
    "home_ats_cover_pct_r5", "away_ats_cover_pct_r5",
    "home_pf", "away_pf", "home_pa", "away_pa",
    "hpf", "apf", "hpa", "apa",
    "home_ou_over_pct_r3", "away_ou_over_pct_r3",
    "home_ou_over_pct_r5", "away_ou_over_pct_r5",
    "home_ou_over_pct_r10", "away_ou_over_pct_r10",
    "home_ou_margin_r3", "away_ou_margin_r3",
    "home_ou_margin_r5", "away_ou_margin_r5",
    "home_ou_margin_r10", "away_ou_margin_r10",
    "home_embarrassed", "away_embarrassed",
    "home_embarrassed_pct_r3", "away_embarrassed_pct_r3",
    "home_embarrassed_pct_r5", "away_embarrassed_pct_r5",
    "home_embarrassed_pct_r10", "away_embarrassed_pct_r10",
    "home_season_ats_pct", "away_season_ats_pct",
    "home_season_wins", "away_season_wins",
    "home_weighted_margin_r5", "away_weighted_margin_r5",
}


# ── Feature name helpers ────────────────────────────────────────────────────────
def get_model_features(cursor: Any, ats_only: bool = False, ou_only: bool = False, live_ats_only: bool = False, live_ou_only: bool = False) -> List[str]:
    """Return feature column names from ``nfl.features``.

    Parameters
    ----------
    cursor : psycopg2 cursor or conn
        Database cursor/connection for querying the features table.
    ats_only : bool
        If True, only return features flagged ``current_ats = True``.
    ou_only : bool
        If True, only return features flagged ``current_ou = True``.
    live_ats_only : bool
        If True, only return features flagged ``live_ats = True``.
    live_ou_only : bool
        If True, only return features flagged ``live_ou = True``.

    Returns
    -------
    List[str]
        Ordered list of feature names.
    """
    conditions = []
    if ats_only:
        conditions.append("current_ats = TRUE")
    if ou_only:
        conditions.append("current_ou = TRUE")
    if live_ats_only:
        conditions.append("live_ats = TRUE")
    if live_ou_only:
        conditions.append("live_ou = TRUE")
    where_clause = " AND ".join(conditions) if conditions else "TRUE"

    sql = f"SELECT name FROM nfl.features WHERE {where_clause} AND is_trainable = TRUE ORDER BY id"
    cursor.execute(sql)
    return [row[0] for row in cursor.fetchall()]


# ── NFLDataLoader ──────────────────────────────────────────────────────────────
class NFLDataLoader:
    """Load, build, and serve NFL game data + features.

    Parameters
    ----------
    db_url : str, optional
        PostgreSQL connection URL.  Defaults to ``DATABASE_URL`` or the
        local ``earl:***@localhost:5432/earl_knows_football`` fallback (password from db_urls).
    ats_only : bool
        If True, default feature selection is ATS-only.
    ou_only : bool
        If True, default feature selection is OU-only.
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        ats_only: bool = False,
        ou_only: bool = False,
        game_type: Optional[str] = None,
    ):
        self.db_url: str = db_url or DEFAULT_DB_URL
        self.ats_only: bool = ats_only
        self.ou_only: bool = ou_only
        self.game_type: Optional[str] = game_type
        self._engine: Any = None
        logger.info(
            "NFLDataLoader initialized (ats_only=%s, ou_only=%s, game_type=%s)",
            ats_only, ou_only, game_type,
        )

    @property
    def engine(self):
        """Lazy-initialized SQLAlchemy engine."""
        if self._engine is None:
            from sqlalchemy import create_engine
            self._engine = create_engine(self.db_url, pool_pre_ping=True)
        return self._engine

    def __repr__(self) -> str:
        return (
            f"NFLDataLoader(db_url={self.db_url!r}, "
            f"ats_only={self.ats_only}, ou_only={self.ou_only})"
        )

    # ── Feature catalog helpers (source of truth: DB `features` table) ────────
    _CATALOG_SCHEMA = "nfl"
    _catalog_cache: Optional[Dict[str, Dict[str, str]]] = None
    _catalog_ts: float = 0.0

    def _load_catalog_from_db(self) -> Dict[str, Dict[str, str]]:
        """Load {name: {'description','display_name'}} from nfl.features.

        The DB is the single source of truth for the feature catalog (including
        descriptions and customer-facing display names). Values are cached for a
        short TTL so admin edits propagate without hammering the DB.
        """
        now = time.time()
        if self._catalog_cache is not None and (now - self._catalog_ts) < 60:
            return self._catalog_cache
        from sqlalchemy import text as _text
        try:
            eng = self.engine
            with eng.connect() as conn:
                rows = conn.execute(
                    _text(f"SELECT name, description, display_name FROM {self._CATALOG_SCHEMA}.features")
                ).mappings().all()
            catalog = {}
            for r in rows:
                name = r["name"]
                catalog[name] = {
                    "description": r["description"] or "",
                    "display_name": r["display_name"] or name,
                }
            self._catalog_cache = catalog
            self._catalog_ts = now
            return catalog
        except Exception:
            logger.exception("Failed to load feature catalog from DB")
            self._catalog_cache = {}
            self._catalog_ts = now
            return self._catalog_cache

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full features catalog dict (name → description) from the DB."""
        db = self._load_catalog_from_db()
        return {name: meta["description"] for name, meta in db.items()}

    def get_feature_names(self) -> List[str]:
        """Return all known feature names."""
        return list(self.get_features_catalog().keys())

    def get_feature_description(self, name: str) -> Optional[str]:
        """Return the description for a single feature (None if unknown)."""
        db = self._load_catalog_from_db()
        meta = db.get(name)
        return meta["description"] if meta and meta["description"] else None

    def get_display_name(self, name: str) -> str:
        """Return the human-friendly display label from the DB (falls back to name)."""
        db = self._load_catalog_from_db()
        meta = db.get(name)
        return meta["display_name"] if meta and meta["display_name"] else name

    def get_feature_aliases(self, name: str) -> List[str]:
        """Return known aliases for a feature."""
        return FEATURE_ALIASES.get(name, [])

    def get_all_with_display(self) -> Dict[str, str]:
        """Return all features with their display names."""
        db = self._load_catalog_from_db()
        return {name: meta["display_name"] for name, meta in db.items()}

    # ── Query building ──────────────────────────────────────────────────────────

    def _build_query(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
        game_type: Optional[str] = None,
    ) -> str:
        """Construct the SQL query with optional filters.

        Parameters
        ----------
        seasons :
            Only games from these season years (e.g. ``[2023, 2024]``).
        status :
            Game status filter (``'FINAL'``, ``'Closed'``, etc.).
        limit :
            Maximum number of rows returned.
        include_upcoming :
            If True and no explicit status is given, include all games
            regardless of status (loads non-final games too).
        game_ids :
            Only games with these primary-key IDs.
        """
        conditions: List[str] = []

        if seasons:
            placeholders = ", ".join(str(s) for s in seasons)
            conditions.append(f"s.year IN ({placeholders})")

        if status is not None:
            conditions.append(f"g.status = '{status}'")
        elif include_upcoming and not game_ids:
            conditions.append("g.status IS NOT NULL")

        if game_ids:
            ids_str = ", ".join(str(i) for i in game_ids)
            conditions.append(f"g.id IN ({ids_str})")

        if game_type is None:
            game_type = self.game_type
        if game_type:
            conditions.append(f"g.game_type = '{game_type}'")
        else:
            # NEVER include preseason games. Callers of the default/backtest
            # path don't pass a game_type, and preseason results must never
            # feed rolling stats or training (treat them as practice). This
            # guards against a FINAL preseason game leaking in via the
            # status='FINAL' filter alone.
            conditions.append("g.game_type != 'PRE'")

        sql = GAME_QUERY.strip().rstrip(";")

        if conditions:
            # Replace the fixed WHERE clause already in GAME_QUERY
            clause = f"WHERE {' AND '.join(conditions)}"
            sql = sql.replace(
                "WHERE g.season_id IS NOT NULL\n  AND g.week IS NOT NULL\nORDER BY",
                f"{clause}\nORDER BY",
            )

        if limit:
            sql += f"\nLIMIT {limit}"

        return sql

    # ── Data loading ─────────────────────────────────────────────────────────

    def _query(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
        game_type: Optional[str] = None,
    ) -> pd.DataFrame:
        """Execute the game query and return raw DataFrame."""
        sql = self._build_query(
            seasons=seasons,
            status=status,
            limit=limit,
            include_upcoming=include_upcoming,
            game_ids=game_ids,
            game_type=game_type,
        )
        t0 = time.time()
        df = pd.read_sql(sql, self.engine)
        elapsed = time.time() - t0
        logger.info("Query returned %d rows in %.2fs", len(df), elapsed)
        return df

    def load_games(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
        game_type: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load raw NFL game data from the database.

        Parameters
        ----------
        seasons : list of int, optional
            Filter to these season years.
        status : str, optional
            Game status filter (e.g. ``'FINAL'``).  Defaults to ``'FINAL'``.
        limit : int, optional
            Max rows.
        include_upcoming : bool
            Include non-final games (scheduled, in-progress).
        game_ids : list of int, optional
            Only these specific game IDs.
        """
        if status is None and not include_upcoming:
            status = "FINAL"
        return self._query(
            seasons=seasons,
            status=status,
            limit=limit,
            include_upcoming=include_upcoming,
            game_ids=game_ids,
        )

    def load_all_games(
        self,
        seasons: Optional[List[int]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Load all games regardless of status (for pick cards)."""
        return self.load_games(
            seasons=seasons,
            status=None,
            limit=limit,
            include_upcoming=True,
        )

    def load_data(
        self,
        seasons: Optional[List[int]] = None,
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        feature_names: Optional[List[str]] = None,
        game_ids: Optional[List[int]] = None,
        game_type: Optional[str] = None,
        build_features_fn=None,
        **build_kwargs,
    ) -> pd.DataFrame:
        """Load raw game data **and** build features.

        This is the primary entry point for training and inference.

        Parameters
        ----------
        seasons :
            Season years to include (training) or ``None`` for inference.
        limit :
            Row limit.
        include_upcoming :
            Include non-final games (for upcoming game inference).
        feature_names :
            Columns to keep.  Defaults to DB-listed trainable features
            (ATS or OU filtered by constructor flags).
        game_ids :
            Only these specific game IDs.
        build_features_fn :
            Custom feature engineering callable.
            Defaults to the module-level ``build_features()``.
        **build_kwargs :
            Forwarded to the feature engineering callable.
        """
        # 1. Load raw game data
        df = self.load_games(
            seasons=seasons,
            status=None if include_upcoming else "FINAL",
            limit=limit,
            include_upcoming=include_upcoming,
            game_ids=game_ids,
            game_type=game_type,
        )

        if df.empty:
            logger.warning("No games returned — returning empty DataFrame")
            return df

        # Resolve the stats gate: explicit game_type > constructor > REG default.
        # Defaulting to REG means preseason rows (game_type='PRE') are NEVER mixed
        # into regular-season inference/training unless the caller asks for PRE.
        gt = game_type or self.game_type or "REG"

        # 2. Load team stats from nfl.team_rolling_stats (pre-computed, backward-looking)
        team_stats = None
        try:
            CUM_SQL = """
                SELECT
                    t.season, t.week, t.game_id, t.team_abbr,
                    CASE WHEN t.is_home THEN at.abbreviation ELSE ht.abbreviation END AS opp_abbr,
                    t.off_yds_r5                             AS off_ypg,
                    t.ypp_r5                                 AS ypp,
                    t.pass_yds_r5                            AS pass_ypg,
                    t.rush_yds_r5                            AS rush_ypg,
                    t.pass_ypa_r5                            AS pass_ypa,
                    t.rush_ypa_r5                            AS rush_ypa,
                    t.turnover_margin_r5                     AS turnover_diff,
                    t.def_yds_r5                             AS def_ypg,
                    t.def_ypp_r5                             AS def_ypp,
                    t.def_pass_yds_r5                        AS def_pass_ypg,
                    t.def_rush_yds_r5                        AS def_rush_ypg,
                    t.first_downs_r5                         AS first_downs,
                    t.third_down_pct_r5                      AS third_down_pct,
                    t.fourth_down_pct_r5                     AS fourth_down_pct,
                    t.rz_trips_r5                            AS rz_trips,
                    t.rz_td_pct_r5                           AS rz_td_pct,
                    t.explosive_rate_r5                      AS explosive_plays,
                    t.three_and_out_rate_r5                  AS three_and_outs,
                    t.ints_thrown_r5                         AS ints_thrown,
                    t.def_first_downs_r5                     AS def_first_downs,
                    t.def_third_down_pct_r5                  AS def_third_down_pct,
                    t.def_fourth_down_pct_r5                 AS def_fourth_down_pct,
                    t.def_rz_trips_r5                        AS def_rz_trips,
                    t.def_rz_td_pct_r5                       AS def_rz_td_pct,
                    t.def_explosive_rate_r5                  AS def_explosive_plays,
                    t.def_three_and_outs_r5                  AS def_three_and_outs,
                    t.def_ints_thrown_r5                     AS def_ints_thrown,
                    t.epa_per_play_r5                        AS off_epa_per_play,
                    t.win_streak,
                    t.off_pts_stddev_r5                      AS off_pts_stddev_5,
                    t.off_yds_stddev_r5                      AS off_yds_stddev_5,
                    NULL::REAL                               AS rw_off_ppg,
                    NULL::REAL                               AS rw_off_ypg,
                    NULL::REAL                               AS adj_off_ppg,
                    NULL::REAL                               AS adj_off_ypg,
                    t.def_epa_per_play_r5                    AS def_epa_per_play,
                    t.opp_pts_stddev_r5                      AS def_pts_stddev_5,
                    t.opp_yds_stddev_r5                      AS def_yds_stddev_5,
                    NULL::REAL                               AS rw_def_ppg,
                    NULL::REAL                               AS rw_def_ypg,
                    NULL::REAL                               AS adj_def_ppg,
                    NULL::REAL                               AS adj_def_ypg,
                    t.sacks_r5                               AS def_sacks,
                    t.takeaways_r5                           AS def_takeaways,
                    t.sacks_r5                               AS off_sacks_allowed,
                    t.off_yardage_rank,
                    t.def_yardage_rank,
                    t.off_scoring_rank,
                    t.def_scoring_rank,
                    t.off_rushing_rank,
                    t.def_rushing_rank,
                    t.off_passing_rank,
                    t.def_passing_rating_rank,
                    t.feeds_into_game_id
                FROM nfl.team_rolling_stats t
                LEFT JOIN nfl.games g ON t.game_id = g.id
                LEFT JOIN nfl.teams ht ON g.home_team_id = ht.id
                LEFT JOIN nfl.teams at ON g.away_team_id = at.id
                WHERE t.game_type = %(gt)s
                ORDER BY t.season, t.week, t.team_abbr
            """
            ts_df = pd.read_sql(CUM_SQL, self.engine, params={"gt": gt})
            if not ts_df.empty:
                team_stats = ts_df.dropna(subset=["feeds_into_game_id"])
                team_stats["feeds_into_game_id"] = team_stats["feeds_into_game_id"].astype(int)
                logger.info(
                    "Loaded %d cumulative stat rows from nfl.team_rolling_stats (%d-%d)",
                    len(team_stats),
                    int(team_stats["season"].min()),
                    int(team_stats["season"].max()),
                )
        except Exception as exc:
            logger.warning(
                "Failed to load cumulative stats: %s -- falling back to window-function query",
                exc,
            )
            try:
                from .team_stats import compute_team_game_aggregates
                ts_df = compute_team_game_aggregates(self.engine)
                if not ts_df.empty:
                    team_stats = ts_df[
                        ["season", "week", "team_abbr", "opp_abbr",
                         "off_ypg", "ypp", "pass_ypg", "rush_ypg",
                         "pass_ypa", "rush_ypa", "turnover_diff",
                         "def_ypg", "def_ypp",
                         "def_pass_ypg", "def_rush_ypg",
                         "first_downs", "third_down_pct", "fourth_down_pct",
                         "rz_trips", "rz_td_pct",
                         "explosive_plays", "three_and_outs", "ints_thrown",
                         "def_first_downs", "def_third_down_pct", "def_fourth_down_pct",
                         "def_rz_trips", "def_rz_td_pct",
                         "def_explosive_plays", "def_three_and_outs", "def_ints_thrown",
                         ]
                    ].copy()
                    # Add game_id from the games table so we can compute feeds_into_game_id
                    try:
                        game_id_q = """
                            SELECT g.id AS game_id, g.season, g.week,
                                   g.home_team_abbr AS team_abbr
                            FROM nfl.games g
                            UNION
                            SELECT g.id AS game_id, g.season, g.week,
                                   g.away_team_abbr AS team_abbr
                            FROM nfl.games g
                        """
                        game_ids = pd.read_sql(game_id_q, self.engine)
                        team_stats = team_stats.merge(
                            game_ids, on=["season", "week", "team_abbr"], how="left"
                        )
                        # Rolling-averages already exclude current game,
                        # so stats feed into the SAME game_id
                        team_stats["feeds_into_game_id"] = team_stats["game_id"]
                    except Exception as exc3:
                        logger.warning("Failed to add game_id to fallback stats: %s", exc3)
                    logger.info(
                        "Loaded %d team-game stat rows from nfl.game_stats (%d-%d)",
                        len(team_stats),
                        int(team_stats["season"].min()),
                        int(team_stats["season"].max()),
                    )
            except ImportError:
                logger.debug("team_stats module not available -- skipping")
            except Exception as exc2:
                logger.warning("Failed to load team stats: %s", exc2)

        # 3. Load QB pre-game stats
        qb_stats = None
        try:
            QB_SQL = """
                WITH actual_starters AS (
                    -- Actual game starters from player participation data
                    SELECT DISTINCT ON (pws.game_id, pws.team_id)
                        pws.game_id,
                        pws.team_id,
                        pws.player_id
                    FROM nfl.player_weekly_stats pws
                    JOIN nfl.players pl ON pl.id = pws.player_id
                    WHERE pl.position = 'QB'
                    ORDER BY pws.game_id, pws.team_id, pws.pass_attempts DESC NULLS LAST
                ),
                projected_starter AS (
                    -- Per (game, team): actual starter if available,
                    -- else depth-chart QB1 (always-updated for upcoming games)
                    SELECT
                        g.id AS game_id,
                        g.home_team_id AS team_id,
                        COALESCE(as_.player_id, dc.player_id) AS player_id
                    FROM nfl.games g
                    LEFT JOIN actual_starters as_
                        ON as_.game_id = g.id AND as_.team_id = g.home_team_id
                    LEFT JOIN nfl.depth_charts dc
                        ON dc.team_id = g.home_team_id
                        AND dc.position = 'QB'
                        AND dc.slot = 1
                    UNION ALL
                    SELECT
                        g.id AS game_id,
                        g.away_team_id AS team_id,
                        COALESCE(as_.player_id, dc.player_id) AS player_id
                    FROM nfl.games g
                    LEFT JOIN actual_starters as_
                        ON as_.game_id = g.id AND as_.team_id = g.away_team_id
                    LEFT JOIN nfl.depth_charts dc
                        ON dc.team_id = g.away_team_id
                        AND dc.position = 'QB'
                        AND dc.slot = 1
                )
                SELECT
                    g.id AS game_id,
                    -- Home QB: cumulative pre-game stats
                    h_cum.games_played       AS home_qb_games_season,
                    h_cum.passer_rating_cum   AS home_qb_passer_rating_season,
                    h_cum.any_a               AS home_qb_any_a_season,
                    h_cum.ypa                 AS home_qb_ypa_season,
                    h_cum.td_pct              AS home_qb_td_pct_season,
                    h_cum.int_pct             AS home_qb_int_pct_season,
                    h_cum.sack_rate           AS home_qb_sack_rate_season,
                    CASE WHEN h_cum.games_played > 0
                        THEN GREATEST(0.0, h_cum.cum_rush_yds / h_cum.games_played)
                        ELSE NULL END        AS home_qb_rush_ypg_season,
                    CASE WHEN h_cum.games_played > 0
                        THEN GREATEST(0.0, h_cum.cum_rush_att / h_cum.games_played)
                        ELSE NULL END        AS home_qb_rush_att_pg_season,
                    -- Home QB: rolling 5-game pre-game stats
                    h_roll.games_5            AS home_qb_games_5,
                    h_roll.passer_rating_5    AS home_qb_passer_rating_5,
                    h_roll.any_a_5            AS home_qb_any_a_5,
                    h_roll.ypa_5              AS home_qb_ypa_5,
                    h_roll.td_pct_5           AS home_qb_td_pct_5,
                    h_roll.int_pct_5          AS home_qb_int_pct_5,
                    h_roll.sack_rate_5        AS home_qb_sack_rate_5,
                    CASE WHEN h_roll.games_5 > 0
                        THEN GREATEST(0.0, h_roll.rush_yds_5 / h_roll.games_5)
                        ELSE NULL END        AS home_qb_rush_ypg_5,
                    h_roll.rush_att_5         AS home_qb_rush_att_5,
                    -- Away QB: cumulative pre-game stats
                    a_cum.games_played       AS away_qb_games_season,
                    a_cum.passer_rating_cum   AS away_qb_passer_rating_season,
                    a_cum.any_a               AS away_qb_any_a_season,
                    a_cum.ypa                 AS away_qb_ypa_season,
                    a_cum.td_pct              AS away_qb_td_pct_season,
                    a_cum.int_pct             AS away_qb_int_pct_season,
                    a_cum.sack_rate           AS away_qb_sack_rate_season,
                    CASE WHEN a_cum.games_played > 0
                        THEN GREATEST(0.0, a_cum.cum_rush_yds / a_cum.games_played)
                        ELSE NULL END        AS away_qb_rush_ypg_season,
                    CASE WHEN a_cum.games_played > 0
                        THEN GREATEST(0.0, a_cum.cum_rush_att / a_cum.games_played)
                        ELSE NULL END        AS away_qb_rush_att_pg_season,
                    -- Away QB: rolling 5-game pre-game stats
                    a_roll.games_5            AS away_qb_games_5,
                    a_roll.passer_rating_5    AS away_qb_passer_rating_5,
                    a_roll.any_a_5            AS away_qb_any_a_5,
                    a_roll.ypa_5              AS away_qb_ypa_5,
                    a_roll.td_pct_5           AS away_qb_td_pct_5,
                    a_roll.int_pct_5          AS away_qb_int_pct_5,
                    a_roll.sack_rate_5        AS away_qb_sack_rate_5,
                    CASE WHEN a_roll.games_5 > 0
                        THEN GREATEST(0.0, a_roll.rush_yds_5 / a_roll.games_5)
                        ELSE NULL END        AS away_qb_rush_ypg_5,
                    a_roll.rush_att_5         AS away_qb_rush_att_5,
                    -- Home QB prior-season fallback (cumulative + rolling) — used
                    -- when current-season pre-game stats don't exist yet (early season)
                    h_cum_prev.games_played    AS home_qb_games_season_prev,
                    h_cum_prev.passer_rating_cum AS home_qb_passer_rating_season_prev,
                    h_cum_prev.any_a            AS home_qb_any_a_season_prev,
                    h_cum_prev.ypa              AS home_qb_ypa_season_prev,
                    h_cum_prev.td_pct           AS home_qb_td_pct_season_prev,
                    h_cum_prev.int_pct          AS home_qb_int_pct_season_prev,
                    h_cum_prev.sack_rate        AS home_qb_sack_rate_season_prev,
                    CASE WHEN h_cum_prev.games_played > 0
                        THEN GREATEST(0.0, h_cum_prev.cum_rush_yds / h_cum_prev.games_played)
                        ELSE 0 END              AS home_qb_rush_ypg_season_prev,
                    CASE WHEN h_cum_prev.games_played > 0
                        THEN GREATEST(0.0, h_cum_prev.cum_rush_att / h_cum_prev.games_played)
                        ELSE 0 END              AS home_qb_rush_att_pg_season_prev,
                    h_roll_prev.games_5          AS home_qb_games_5_prev,
                    h_roll_prev.passer_rating_5  AS home_qb_passer_rating_5_prev,
                    h_roll_prev.any_a_5          AS home_qb_any_a_5_prev,
                    h_roll_prev.ypa_5            AS home_qb_ypa_5_prev,
                    h_roll_prev.td_pct_5         AS home_qb_td_pct_5_prev,
                    h_roll_prev.int_pct_5        AS home_qb_int_pct_5_prev,
                    h_roll_prev.sack_rate_5      AS home_qb_sack_rate_5_prev,
                    CASE WHEN h_roll_prev.games_5 > 0
                        THEN GREATEST(0.0, h_roll_prev.rush_yds_5 / h_roll_prev.games_5)
                        ELSE 0 END               AS home_qb_rush_ypg_5_prev,
                    h_roll_prev.rush_att_5       AS home_qb_rush_att_5_prev,
                    -- Away QB prior-season fallback
                    a_cum_prev.games_played    AS away_qb_games_season_prev,
                    a_cum_prev.passer_rating_cum AS away_qb_passer_rating_season_prev,
                    a_cum_prev.any_a            AS away_qb_any_a_season_prev,
                    a_cum_prev.ypa              AS away_qb_ypa_season_prev,
                    a_cum_prev.td_pct           AS away_qb_td_pct_season_prev,
                    a_cum_prev.int_pct          AS away_qb_int_pct_season_prev,
                    a_cum_prev.sack_rate        AS away_qb_sack_rate_season_prev,
                    CASE WHEN a_cum_prev.games_played > 0
                        THEN GREATEST(0.0, a_cum_prev.cum_rush_yds / a_cum_prev.games_played)
                        ELSE 0 END              AS away_qb_rush_ypg_season_prev,
                    CASE WHEN a_cum_prev.games_played > 0
                        THEN GREATEST(0.0, a_cum_prev.cum_rush_att / a_cum_prev.games_played)
                        ELSE 0 END              AS away_qb_rush_att_pg_season_prev,
                    a_roll_prev.games_5          AS away_qb_games_5_prev,
                    a_roll_prev.passer_rating_5  AS away_qb_passer_rating_5_prev,
                    a_roll_prev.any_a_5          AS away_qb_any_a_5_prev,
                    a_roll_prev.ypa_5            AS away_qb_ypa_5_prev,
                    a_roll_prev.td_pct_5         AS away_qb_td_pct_5_prev,
                    a_roll_prev.int_pct_5        AS away_qb_int_pct_5_prev,
                    a_roll_prev.sack_rate_5      AS away_qb_sack_rate_5_prev,
                    CASE WHEN a_roll_prev.games_5 > 0
                        THEN GREATEST(0.0, a_roll_prev.rush_yds_5 / a_roll_prev.games_5)
                        ELSE 0 END               AS away_qb_rush_ypg_5_prev,
                    a_roll_prev.rush_att_5       AS away_qb_rush_att_5_prev,
                    -- Derived weather-aware QB rating: cold rating if game is cold, else warm rating
                    CASE WHEN g.temperature < 40
                        THEN h_qbw.cold_passer_rating
                        ELSE h_qbw.warm_passer_rating END AS home_qb_cold_warm_passer_rating,
                    -- Derived weather-aware QB rating: precip rating if raining, else dry rating
                    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
                        THEN h_qbw.precip_passer_rating
                        ELSE h_qbw.dry_passer_rating END AS home_qb_precip_dry_passer_rating,
                    -- Home/away QB bad-weather passer rating (leak-free, prior starts only)
                    h_qbw.cold_passer_rating     AS home_qb_cold_passer_rating,
                    h_qbw.precip_passer_rating   AS home_qb_precip_passer_rating,
                    h_qbw.cold_starts            AS home_qb_cold_starts,
                    h_qbw.precip_starts          AS home_qb_precip_starts,
                    a_qbw.cold_passer_rating     AS away_qb_cold_passer_rating,
                    a_qbw.precip_passer_rating   AS away_qb_precip_passer_rating,
                    a_qbw.cold_starts            AS away_qb_cold_starts,
                    a_qbw.precip_starts          AS away_qb_precip_starts,
                    -- Away weather-aware QB rating (cold if game cold, else warm)
                    CASE WHEN g.temperature < 40
                        THEN a_qbw.cold_passer_rating
                        ELSE a_qbw.warm_passer_rating END AS away_qb_cold_warm_passer_rating,
                    -- Away weather-aware QB rating (precip if raining, else dry)
                    CASE WHEN g.weather_condition ~* 'rain|snow|drizzle|thunder|shower'
                        THEN a_qbw.precip_passer_rating
                        ELSE a_qbw.dry_passer_rating END AS away_qb_precip_dry_passer_rating
                FROM nfl.games g
                JOIN nfl.seasons s ON s.id = g.season_id
                -- Home starter + their pre-game stats
                LEFT JOIN projected_starter h_st
                    ON h_st.game_id = g.id AND h_st.team_id = g.home_team_id
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_cumulative_stats qc
                    WHERE qc.player_id = h_st.player_id
                      AND qc.season = s.year
                      AND qc.game_date < g.date::date
                      AND qc.game_type = %(gt)s
                    ORDER BY qc.game_date DESC
                    LIMIT 1
                ) h_cum ON true
                -- Home QB prior-season fallback (for early-season games when
                -- the current-season pre-game stat is not yet available)
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_cumulative_stats qc
                    WHERE qc.player_id = h_st.player_id
                      AND qc.season = s.year - 1
                      AND qc.game_type = %(gt)s
                    ORDER BY qc.game_date DESC
                    LIMIT 1
                ) h_cum_prev ON true
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_rolling_stats qr
                    WHERE qr.player_id = h_st.player_id
                      AND qr.season = s.year
                      AND qr.game_date < g.date::date
                      AND qr.game_type = %(gt)s
                    ORDER BY qr.game_date DESC
                    LIMIT 1
                ) h_roll ON true
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_rolling_stats qr
                    WHERE qr.player_id = h_st.player_id
                      AND qr.season = s.year - 1
                      AND qr.game_type = %(gt)s
                    ORDER BY qr.game_date DESC
                    LIMIT 1
                ) h_roll_prev ON true
                -- Home QB bad-weather passer rating (this game's row holds prior-starts rating)
                LEFT JOIN nfl.qb_badweather_stats h_qbw
                    ON h_qbw.player_id = h_st.player_id
                   AND h_qbw.feeds_into_game_id = g.id
                -- Away starter + their pre-game stats
                LEFT JOIN projected_starter a_st
                    ON a_st.game_id = g.id AND a_st.team_id = g.away_team_id
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_cumulative_stats qc
                    WHERE qc.player_id = a_st.player_id
                      AND qc.season = s.year
                      AND qc.game_date < g.date::date
                      AND qc.game_type = %(gt)s
                    ORDER BY qc.game_date DESC
                    LIMIT 1
                ) a_cum ON true
                -- Away QB prior-season fallback
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_cumulative_stats qc
                    WHERE qc.player_id = a_st.player_id
                      AND qc.season = s.year - 1
                      AND qc.game_type = %(gt)s
                    ORDER BY qc.game_date DESC
                    LIMIT 1
                ) a_cum_prev ON true
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_rolling_stats qr
                    WHERE qr.player_id = a_st.player_id
                      AND qr.season = s.year
                      AND qr.game_date < g.date::date
                      AND qr.game_type = %(gt)s
                    ORDER BY qr.game_date DESC
                    LIMIT 1
                ) a_roll ON true
                LEFT JOIN LATERAL (
                    SELECT * FROM nfl.qb_rolling_stats qr
                    WHERE qr.player_id = a_st.player_id
                      AND qr.season = s.year - 1
                      AND qr.game_type = %(gt)s
                    ORDER BY qr.game_date DESC
                    LIMIT 1
                ) a_roll_prev ON true
                -- Away QB bad-weather passer rating
                LEFT JOIN nfl.qb_badweather_stats a_qbw
                    ON a_qbw.player_id = a_st.player_id
                   AND a_qbw.feeds_into_game_id = g.id
                ORDER BY g.date
            """
            with self.engine.connect() as conn:
                qb_stats = pd.read_sql_query(QB_SQL, conn, params={"gt": gt})
            logger.info(
                "Loaded %d QB stat rows (%d-%d)",
                len(qb_stats),
                int(qb_stats["game_id"].min()),
                int(qb_stats["game_id"].max()),
            )
        except Exception as exc:
            logger.warning("Failed to load QB stats: %s", exc)
            qb_stats = pd.DataFrame()

        # 4. Run feature engineering
        fn = build_features_fn if build_features_fn is not None else build_features
        df = fn(df, team_stats=team_stats, qb_stats=qb_stats, **build_kwargs)

        # 3. Determine output columns
        if feature_names is None:
            with self.engine.connect() as conn:
                cur = conn.connection.cursor()
                feature_names = get_model_features(
                    cur,
                    ats_only=self.ats_only,
                    ou_only=self.ou_only,
                )

        # 4. Always include context and target columns needed downstream
        context_cols = {
            "season_year", "home_ats_cover", "away_ats_cover",
            "over_result", "home_score_margin", "ou_margin",
            "home_score", "away_score", "closing_ou", "closing_spread",
            "opening_ou", "opening_spread",
            "home_abbr", "away_abbr",
            "venue", "surface", "roof_type",
            "week", "game_id", "game_type",
            # Moneyline and odds columns (for handicapper info + PnL)
            "closing_home_ml", "closing_away_ml",
            "closing_spread_home_odds", "closing_spread_away_odds",
            "closing_over_odds", "closing_under_odds",
            # Line movement features (kept in loader output so training + engine match)
            "spread_movement", "sp_h_odds_mvmt", "sp_a_odds_mvmt",
        }
        for c in context_cols:
            if c in df.columns and c not in feature_names:
                feature_names.append(c)

        # 5. Select only what was asked for
        existing = [c for c in feature_names if c in df.columns]
        missing = [c for c in feature_names if c not in df.columns]
        if missing:
            logger.warning(
                "%d feature(s) not found — filling with NaN: %s",
                len(missing), missing,
            )
            missing_df = pd.DataFrame(
                {col: [float("nan")] * len(df) for col in missing},
                index=df.index,
            )
            df = pd.concat([df, missing_df], axis=1)

        return df[feature_names].copy()

    def load_inference_data(
        self,
        game_ids: List[int],
        feature_names: Optional[List[str]] = None,
        game_type: Optional[str] = None,
    ) -> pd.DataFrame:
        """Load features for specific games (inference without labels).

        Parameters
        ----------
        game_ids :
            Primary keys of the games to load.
        feature_names :
            Feature columns to return (defaults to DB trainable features).
        game_type :
            If given (e.g. ``'PRE'``), only load features for games of that
            type and pull matching game_type stats. Falls back to the
            constructor-level game_type when ``None``.
        """
        return self.load_data(
            game_ids=game_ids,
            include_upcoming=True,
            feature_names=feature_names,
            game_type=game_type,
        )

    def get_feature_columns(
        self,
        ats_only: Optional[bool] = None,
        ou_only: Optional[bool] = None,
    ) -> List[str]:
        """Return feature columns from the ``nfl.features`` table.

        Uses ``ats_only`` / ``ou_only`` from the constructor when not
        explicitly overridden.
        """
        if ats_only is None:
            ats_only = self.ats_only
        if ou_only is None:
            ou_only = self.ou_only
        with self.engine.connect() as conn:
            cur = conn.connection.cursor()
            return get_model_features(cur, ats_only=ats_only, ou_only=ou_only)

    @staticmethod
    def extract_features_from_training_run(
        results_json: Any,
        min_importance: float = 0.0,
    ) -> List[str]:
        """Extract feature names from a training run's ``results_json``.

        Parameters
        ----------
        results_json :
            Parsed ``results_json`` column from ``nfl.training_runs``.
            Expected to contain ``{"feature_importance": [...]}`` where
            each entry is ``{"feature": "...", "importance": ...}``.
        min_importance :
            Minimum importance threshold (0.0 = all).

        Returns
        -------
        List of feature names ordered by importance descending.
        """
        if results_json is None:
            return []

        imp_list = []

        # Case A: dict with "results" array (training_runs.results_json)
        if isinstance(results_json, dict) and "results" in results_json:
            for res in reversed(results_json["results"]):
                fi = res.get("feature_importance", [])
                if fi:
                    imp_list = fi
                    break

        # Case B: flat dict with "feature_importance"
        elif isinstance(results_json, dict) and "feature_importance" in results_json:
            imp_list = results_json["feature_importance"]

        # Case C: list of feature dicts directly
        elif isinstance(results_json, list):
            if results_json and isinstance(results_json[0], dict):
                if "feature" in results_json[0]:
                    imp_list = results_json
                elif "feature_importance" in results_json[0]:
                    imp_list = results_json[-1].get("feature_importance", [])

        if not imp_list:
            logger.info("No feature_importance found in results_json")
            return []

        raw: List[tuple[float, str]] = []
        for item in imp_list:
            if isinstance(item, dict) and "feature" in item:
                imp = float(item.get("importance", 0.0) or 0.0)
                if imp >= min_importance:
                    raw.append((imp, item["feature"]))

        raw.sort(key=lambda x: -x[0])

        # De-duplicate preserving highest-importance occurrence
        seen: set[str] = set()
        result: list[str] = []
        for imp_val, feat in raw:
            if feat not in seen:
                seen.add(feat)
                result.append(feat)

        logger.info("Extracted %d features (min_importance=%.4f)", len(result), min_importance)
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_features(
        self,
        df: pd.DataFrame,
        **kwargs,
    ) -> pd.DataFrame:
        """Apply module-level feature engineering and order columns.

        Parameters
        ----------
        df :
            Raw game data from ``load_games()``.
        **kwargs :
            Forwarded to the module-level ``build_features()``.

        Returns
        -------
        DataFrame with only the registered feature columns that exist
        in the built data.
        """
        df = build_features(df, **kwargs)

        # Keep only known columns
        known = set(self.get_feature_names())
        keep = [c for c in df.columns if c in known]
        return df[keep].copy()


# ── Module-level: feature engineering ─────────────────────────────────────────

def build_features(df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
    """Build NFL game features from raw game data.

    Parameters
    ----------
    df :
        Raw DataFrame from :meth:`NFLDataLoader.load_games`.
    **kwargs :
        Placeholder for future param overrides.

    Returns
    -------
    DataFrame with all features from the nfl.features catalog populated.
    """
    if df.empty:
        return df

    df = df.copy()

    # ── Team abbreviation cache ──────────────────────────────────────────────
    global _location_cache
    _location_cache = {**TEAM_LOCATIONS}

    # ── 1. Outcome targets ───────────────────────────────────────────────────
    home_won = df["home_score"] > df["away_score"]
    # closing_spread/closing_ou can be None or NaN for scheduled games / seasons
    # without betting data; coerce to float NaN so the eager >/comparisons below
    # never throw (None > int → TypeError) and missing lines stay "no result".
    _ou = pd.to_numeric(df["closing_ou"], errors="coerce")
    _spread = pd.to_numeric(df["closing_spread"], errors="coerce")
    _has_line = _spread.notna()
    df["home_ats_cover"] = np.where(
        _has_line & home_won.notna(),
        (df["home_score"] - df["away_score"] + _spread) > 0,
        float("nan")
    ).astype(float)
    df["away_ats_cover"] = np.where(
        _has_line & home_won.notna(),
        (df["away_score"] - df["home_score"] - _spread) > 0,
        float("nan")
    ).astype(float)
    df["over_result"] = np.where(
        _ou.notna() & df["home_score"].notna() & df["away_score"].notna(),
        (pd.to_numeric(df["home_score"], errors="coerce") + pd.to_numeric(df["away_score"], errors="coerce")) > _ou,
        float("nan")
    ).astype(float)
    df["home_score_margin"] = pd.to_numeric(df["home_score"], errors="coerce") - pd.to_numeric(df["away_score"], errors="coerce")

    # ── 2. Rest days ─────────────────────────────────────────────────────────
    df["game_date"] = pd.to_datetime(df["game_date"])
    df["home_last_game"] = pd.to_datetime(df["home_last_game"], errors="coerce")
    df["away_last_game"] = pd.to_datetime(df["away_last_game"], errors="coerce")
    # Normalize tz-awareness (live games come in tz-aware; historical are naive)
    for _col in ("game_date", "home_last_game", "away_last_game"):
        if getattr(df[_col].dtype, "tz", None) is not None:
            df[_col] = df[_col].dt.tz_localize(None)
    df["home_rest_days"] = (df["game_date"] - df["home_last_game"]).dt.days.fillna(7)
    df["away_rest_days"] = (df["game_date"] - df["away_last_game"]).dt.days.fillna(7)
    df["rest_diff"] = df["home_rest_days"] - df["away_rest_days"]
    df["is_short"] = (df["home_rest_days"] < 6).astype(float)

    # ── 3. Implied scoring ───────────────────────────────────────────────────
    df["home_implied_pts"] = (df["closing_ou"] - df["closing_spread"]) / 2.0
    df["away_implied_pts"] = (df["closing_ou"] + df["closing_spread"]) / 2.0
    df["himp"] = df["home_implied_pts"]
    df["aimp"] = df["away_implied_pts"]
    df["dimp"] = df["himp"] - df["aimp"]

    # ── 4. Spread & OU movement ─────────────────────────────────────────────
    df["spread"] = df["closing_spread"]
    df["opening_ou"] = df["opening_ou"]
    df["spread_movement"] = df["closing_spread"] - df["opening_spread"]
    df["ou_movement"] = df["closing_ou"] - df["opening_ou"]
    # Odds movement needs BOTH an opening and closing moneyline. If the opening
    # odds are missing, movement is UNKNOWN -> leave NaN (pick card blanks) and
    # let _impute_feature give the model a neutral 0. Do NOT fillna(0) the
    # opening line here (that fabricates full movement as if the line opened at 0).
    df["sp_h_odds_mvmt"] = df["closing_spread_home_odds"] - df["opening_spread_home_odds"]
    df["sp_a_odds_mvmt"] = df["closing_spread_away_odds"] - df["opening_spread_away_odds"]

    # ── 5. Rolling team stats (per team across ALL games, home & away) ───
    df = df.sort_values(["season_id", "week", "game_date"]).reset_index(drop=True)

    # Compute ou_margin if missing (needed for OU rolling features)
    if "ou_margin" not in df.columns:
        df["ou_margin"] = (df["home_score"] + df["away_score"]) - df["closing_ou"]

    # Build team-game pairs: each game appears twice (once per team)
    _base = ["game_id", "game_date", "week", "season_id", "season_year",
             "home_ats_cover", "ou_margin", "over_result",
             "closing_spread", "closing_ou"]
    for side, id_col, abbr_col, score_col, opp_col in [
        ("home", "home_team_id", "home_abbr", "home_score", "away_score"),
        ("away", "away_team_id", "away_abbr", "away_score", "home_score"),
    ]:
        cols = _base + [id_col, abbr_col, score_col, opp_col,
                        ("away_abbr" if side == "home" else "home_abbr")]
        rows = df[[c for c in cols if c in df.columns]].copy()
        rows = rows.rename(columns={
            id_col: "team_id",
            abbr_col: "team_abbr",
        })
        # Rename opp abbreviation
        opp_abbr_col = "away_abbr" if side == "home" else "home_abbr"
        if opp_abbr_col in rows.columns:
            rows = rows.rename(columns={opp_abbr_col: "opp_abbr"})
        rows["position"] = side
        rows["score"] = rows[score_col]
        rows["opp_score"] = rows[opp_col]
        rows["won"] = (rows[score_col] > rows[opp_col]).astype(float)
        rows["margin"] = rows[score_col] - rows[opp_col]
        rows["cover"] = (
            rows["home_ats_cover"] if side == "home"
            else (1 - rows["home_ats_cover"])
        ).fillna(0.5)
        rows = rows.rename(columns={"game_date": "date"})
        if side == "home":
            home_long = rows
        else:
            away_long = rows

    tg = pd.concat([home_long, away_long], ignore_index=True)
    tg = tg.sort_values(["team_id", "date", "game_id"]).reset_index(drop=True)

    # ── Load prior-season team stats for first-game NaN seeding ─────
    # Replaces hardcoded fillna(0.5) / fillna(0.0) with prior-season averages
    # for the first game of each team each season.
    prior_map = {}
    try:
        from app.core.config import settings as _s
        p_url = str(_s.database_url).replace("+asyncpg", "")
        from sqlalchemy import create_engine as _ce
        from sqlalchemy import text as _qt
        _pe = _ce(p_url)
        with _pe.connect() as _cx:
            _db = _cx.execute(_qt("SELECT * FROM nfl.prior_team_stats WHERE game_type = 'REG'")).fetchall()
        for _r in _db:
            prior_map[(_r.team_abbr, _r.season)] = {
                "win_pct": _r.win_pct or 0.5,
                "margin": _r.point_differential or 0.0,
                "off_ppg": _r.off_ppg or 0.0,
                "def_ppg": _r.def_ppg or 0.0,
                # Offensive stats
                "off_ypg": _r.off_ypg or 0.0,
                "off_pass_ypg": _r.off_pass_ypg or 0.0,
                "off_rush_ypg": _r.off_rush_ypg or 0.0,
                "off_ypa": _r.off_ypa or 0.0,
                "off_cmp_pct": _r.off_cmp_pct or 0.0,
                "off_third_down_pct": _r.off_third_down_pct or 0.5,
                "off_rz_td_pct": _r.off_rz_td_pct or 0.5,
                "off_explosive_rate": _r.off_explosive_rate or 0.1,
                "off_three_and_out_rate": _r.off_three_and_out_rate or 0.1,
                "off_epa_per_play": _r.off_epa_per_play or 0.0,
                # Defensive stats
                "def_ypg": _r.def_ypg or 0.0,
                "def_pass_ypg": _r.def_pass_ypg or 0.0,
                "def_rush_ypg": _r.def_rush_ypg or 0.0,
                "def_ypa_allowed": _r.def_ypa_allowed or 0.0,
                "def_cmp_pct_allowed": _r.def_cmp_pct_allowed or 0.0,
                "def_third_down_pct": _r.def_third_down_pct or 0.5,
                "def_sack_rate": _r.def_sack_rate or 0.0,
                "def_explosive_rate": _r.def_explosive_rate or 0.1,
                "def_three_and_out_rate": _r.def_three_and_out_rate or 0.1,
                "def_epa_per_play": _r.def_epa_per_play or 0.0,
                # Rolling-weighted
                "rw_off_ppg": _r.rw_off_ppg or _r.off_ppg or 0.0,
                "rw_off_ypg": _r.rw_off_ypg or _r.off_ypg or 0.0,
                "rw_def_ppg": _r.rw_def_ppg or _r.def_ppg or 0.0,
                "rw_def_ypg": _r.rw_def_ypg or _r.def_ypg or 0.0,
                # Adjusted stats
                "adj_off_ppg": _r.adj_off_ppg or _r.rw_off_ppg or _r.off_ppg or 0.0,
                "adj_off_ypg": _r.adj_off_ypg or _r.rw_off_ypg or _r.off_ypg or 0.0,
                "adj_def_ppg": _r.adj_def_ppg or _r.rw_def_ppg or _r.def_ppg or 0.0,
                "adj_def_ypg": _r.adj_def_ypg or _r.rw_def_ypg or _r.def_ypg or 0.0,
                # Streak
                "win_streak": _r.win_streak or 0,
                # Efficiency / drive stats (added 2026-08-04) — used to seed
                # early-season games from previous-season data (MLB-style COALESCE)
                "off_ypp": _r.off_ypp or 0.0,
                "def_ypp": _r.def_ypp or 0.0,
                "off_first_downs": _r.off_first_downs or 0.0,
                "def_first_downs": _r.def_first_downs or 0.0,
                "off_fourth_down_pct": _r.off_fourth_down_pct or 0.5,
                "def_fourth_down_pct": _r.def_fourth_down_pct or 0.5,
                "off_rz_trips": _r.off_rz_trips or 0.0,
                "def_rz_trips": _r.def_rz_trips or 0.0,
                "off_ints_thrown": _r.off_ints_thrown or 0.0,
                "def_ints_thrown": _r.def_ints_thrown or 0.0,
                "turnover_diff_r5": _r.turnover_diff_r5 or 0.0,
                "off_pts_stddev_5": _r.off_pts_stddev_5 or 0.0,
                "off_yds_stddev_5": _r.off_yds_stddev_5 or 0.0,
                "def_pts_stddev_5": _r.def_pts_stddev_5 or 0.0,
                "def_yds_stddev_5": _r.def_yds_stddev_5 or 0.0,
            }
        _pe.dispose()
    except Exception:
        pass

    # ── Prior-season rolling stats (team_rolling_stats) for week-1 backfill ──
    # `prior_team_stats` above is a season-aggregate and lacks per-attempt
    # columns like rush_ypa. The team_rolling_stats table holds the true
    # prior-season cumulative/rolling values (incl. *_r5 and rush_ypa), so we
    # also load each team's LAST REG row of the prior season and use it to fill
    # week-1 gaps for any rolling stat (esp. rush_ypa, which has no
    # prior_team_stats key).
    prior_rolling_map = {}
    try:
        from app.core.config import settings as _pcs
        from sqlalchemy import create_engine as _pce
        _proll_engine = _pce(str(_pcs.database_url).replace("+asyncpg", ""))
        _PROLL_SQL = """
            SELECT DISTINCT ON (t.team_abbr)
                   t.team_abbr, t.season, t.*
            FROM nfl.team_rolling_stats t
            WHERE t.game_type = 'REG'
              AND t.season = (SELECT COALESCE(MAX(season),0)-1 FROM nfl.team_rolling_stats)
            ORDER BY t.team_abbr, t.week DESC, t.game_id DESC
        """
        _proll = pd.read_sql(_PROLL_SQL, _proll_engine)
        for rec in _proll.to_dict("records"):
            prior_rolling_map[(rec["team_abbr"], rec["season"])] = rec
        _proll_engine.dispose()
        logger.info("Loaded %d prior-season rolling stat rows (week-1 backfill)", len(_proll))
    except Exception as _proll_exc:
        logger.warning("Failed to load prior-season rolling stats for week-1 backfill: %s", _proll_exc)

    # ── Compute team-overall rolling stats on the long frame ────────────
    def _first_fill(series: pd.Series, prior_key: str,
                    default_val: float = 0.5) -> pd.Series:
        """Rolling mean; first-game NaN gets prior-season value."""
        r = series.copy()
        m = r.isna()
        if m.any():
            for i in r[m].index:
                tm = tg.loc[i, "team_abbr"]
                sy = tg.loc[i, "season_year"]
                pv = prior_map.get((tm, sy - 1), {}).get(prior_key, default_val)
                r.loc[i] = pv
            r = r.fillna(default_val)
        return r

    for window in [3, 5, 10]:
        tg[f"win_pct_r{window}"] = _first_fill(
            tg.groupby(["team_id", "season_year"])["won"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean()),
            "win_pct", 0.5
        )
        tg[f"margin_r{window}"] = _first_fill(
            tg.groupby(["team_id", "season_year"])["margin"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean()),
            "margin", 0.0
        )
        tg[f"cover_pct_r{window}"] = _first_fill(
            tg.groupby(["team_id", "season_year"])["cover"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean()),
            "win_pct", 0.5
        )
        if window == 10:
            tg["pf"] = _first_fill(
                tg.groupby(["team_id", "season_year"])["score"]
                .transform(lambda s: s.shift(1).rolling(10, min_periods=1).mean()),
                "off_ppg", 0.0
            )
            tg["pa"] = _first_fill(
                tg.groupby(["team_id", "season_year"])["opp_score"]
                .transform(lambda s: s.shift(1).rolling(10, min_periods=1).mean()),
                "def_ppg", 0.0
            )

    # OU features
    if "ou_margin" in tg.columns:
        tg["cover_as_over"] = (tg["ou_margin"] > 0).astype(float)
        for window in [3, 5, 10]:
            tg[f"ou_over_pct_r{window}"] = _first_fill(
                tg.groupby(["team_id", "season_year"])["cover_as_over"]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean()),
                "win_pct", 0.5
            )
            tg[f"ou_margin_r{window}"] = _first_fill(
                tg.groupby("team_id")["ou_margin"]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean()),
                "margin", 0.0
            )
        tg["ou_as_over_pct_r10"] = tg["ou_over_pct_r10"]

    # Embarrassed (lost by 14+)
    tg["embarrassed"] = tg.groupby("team_id")["margin"].transform(
        lambda s: (s.shift(1) <= -14).astype(float)
    ).fillna(0)  # 0 = false if no prior game
    for window in [3, 5, 10]:
        tg[f"embarrassed_pct_r{window}"] = (
            tg.groupby("team_id")["embarrassed"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            .fillna(0.0)
        )

    # Season-long ATS (expanding within each team+season). Leave NaN when a team
    # has no graded games yet (first game / no betting data) so the PICK CARD
    # blanks it; _impute_feature gives the model a neutral 0.5. Do not fillna(0.5)
    # in-place (that fabricates a cover rate for display).
    tg["season_ats_pct"] = (
        tg.groupby(["team_id", "season_id"])["cover"]
        .transform(lambda s: s.shift(1).expanding().mean())
    )
    # Carry prior-season final ATS cover% into a team's first game of a new
    # season (week 1), where shift(1).expanding() is NaN. This stops run_backtest's
    # dropna(subset=features) from discarding every week-1 game, while keeping
    # real data (no fabricated 0.5) on the pick card.
    _prior_ats = tg.assign(_sy=tg["season_year"]).dropna(subset=["season_ats_pct"])
    _last = (
        _prior_ats.sort_values(["team_abbr", "season_year", "week", "date"])
        .groupby(["team_abbr", "season_year"])["season_ats_pct"]
        .last()
        .to_dict()
    )
    def _carry_prior_ats(row):
        if pd.notna(row["season_ats_pct"]):
            return row["season_ats_pct"]
        return _last.get((row["team_abbr"], row["season_year"] - 1))
    tg["season_ats_pct"] = tg.apply(_carry_prior_ats, axis=1)
    # Season wins
    tg["season_wins"] = (
        tg.groupby(["team_id", "season_id"])["won"]
        .transform(lambda s: s.shift(1).expanding().sum())
        .fillna(0)
    )

    # Home/away ATS splits — position-specific (kept for situational data).
    # Same principle: leave NaN (blank on card) when no graded games, model gets 0.5.
    homes = tg[tg.position == "home"].copy()
    homes["ats_cover_pct_r5"] = (
        homes.groupby("team_id")["cover"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    )
    aways = tg[tg.position == "away"].copy()
    aways["ats_cover_pct_r5"] = (
        aways.groupby("team_id")["cover"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    )

    # Streaks
    def _compute_streak(arr):
        if len(arr) == 0:
            return 0
        streak = 0
        for v in reversed(arr):
            if v > 0:
                streak += 1
            else:
                break
        return streak

    # Win streak resets per season (week 1 = 0)
    tg["win_streak"] = (
        tg.groupby(["team_id", "season_year"])["won"]
        .transform(
            lambda s: s.shift(1)
            .rolling(5, min_periods=1)
            .apply(lambda x: _compute_streak(x.values) if len(x) > 0 else 0)
        )
    ).fillna(0).astype(int)

    # ATS streak carries across seasons (no season_year partition)
    tg["ats_streak"] = (
        tg.groupby("team_id")["cover"]
        .transform(
            lambda s: s.shift(1)
            .rolling(5, min_periods=1)
            .apply(lambda x: _compute_streak(x.values) if len(x) > 0 else 0)
        )
    ).fillna(0).astype(int)

    # Weighted (decayed) margin
    decay_weights = np.array([0.0625, 0.125, 0.25, 0.5, 1.0])
    decay_weights = decay_weights / decay_weights.sum()

    def _weighted_avg(series):
        vals = series.values
        if len(vals) == 0:
            return 0.0
        w = decay_weights[-len(vals):]
        return float(np.average(vals, weights=w))

    tg["weighted_margin_r5"] = _first_fill(
        tg.groupby("team_id")["margin"]
        .transform(
            lambda s: s.shift(1)
            .rolling(5, min_periods=1)
            .apply(_weighted_avg)
        ),
        "margin", 0.0
    )

    # ── Join team-overall stats back into wide DataFrame ──────────────────
    home_stats = tg[tg["position"] == "home"].set_index("game_id")
    away_stats = tg[tg["position"] == "away"].set_index("game_id")

    tg_cols = {
        "win_pct_r3", "win_pct_r5", "win_pct_r10",
        "margin_r3", "margin_r5", "margin_r10",
        "cover_pct_r3", "cover_pct_r5", "cover_pct_r10",
        "pf", "pa",
        "ou_over_pct_r3", "ou_over_pct_r5", "ou_over_pct_r10",
        "ou_margin_r3", "ou_margin_r5", "ou_margin_r10",
        "embarrassed", "embarrassed_pct_r3", "embarrassed_pct_r5", "embarrassed_pct_r10",
        "season_ats_pct", "season_wins",
        "ou_as_over_pct_r10",
        "weighted_margin_r5",
        "ats_cover_pct_r5",
    }
    existing = {c for c in tg_cols if c in home_stats.columns}

    for col in existing:
        df[f"home_{col}"] = df["game_id"].map(home_stats[col])
        df[f"away_{col}"] = df["game_id"].map(away_stats[col])

    # Populate PF/PA with legacy names for backward compat
    if "pf" in existing:
        df["hpf"] = df["home_pf"]
        df["apf"] = df["away_pf"]
    if "pa" in existing:
        df["hpa"] = df["home_pa"]
        df["apa"] = df["away_pa"]

    # Implied scoring features
    if "hpf" in df.columns and "apf" in df.columns:
        df["hpf_vs_aimp"] = df["hpf"] - df["aimp"]
        df["apf_vs_himp"] = df["apf"] - df["himp"]
        df["hhpa_vs_imp"] = df["hpf"] - df["aimp"]
        df["aapa_vs_imp"] = df["apf"] - df["himp"]

    # Home/away ATS split features from position-specific computation
    _homes_ats = homes.set_index("game_id")["ats_cover_pct_r5"]
    _aways_ats = aways.set_index("game_id")["ats_cover_pct_r5"]
    # leave NaN (blank on pick card) when no graded games; model gets 0.5 via
    # _impute_feature. Do not fillna(0.5) — that fabricates a cover % for display.
    df["home_ats_home_pct_r5"] = df["game_id"].map(_homes_ats)
    df["away_ats_away_pct_r5"] = df["game_id"].map(_aways_ats)
    # ── 16. Division & primetime flags ───────────────────────────────────
    # NFL division names (North/East/South/West) repeat across conferences, so a
    # same-division game REQUIRES matching conference too (e.g. GB NFC North vs
    # PIT AFC North is NOT a division game even though both division == 'North').
    if "home_div" in df.columns and "away_div" in df.columns:
        if "home_conf" in df.columns and "away_conf" in df.columns:
            df["is_division_game"] = (
                (df["home_conf"] == df["away_conf"]) & (df["home_div"] == df["away_div"])
            ).astype(float)
        else:
            df["is_division_game"] = (
                (df["home_div"] == df["away_div"]).astype(float)
            )
    else:
        df["is_division_game"] = 0.0

    date_col = "game_date" if "game_date" in df.columns else "date"
    if date_col in df.columns:
        try:
            # Convert game_date to US Eastern to determine primetime
            _dt = pd.to_datetime(df[date_col])
            if _dt.dt.tz is not None:
                _et = _dt.dt.tz_convert("America/New_York")
            else:
                _et = _dt.dt.tz_localize("UTC").dt.tz_convert("America/New_York")
            df["game_hour"] = _et.dt.hour
            df["is_primetime"] = (
                df["game_hour"].isin([20, 21, 22, 23]).astype(float)
            )
        except Exception:
            df["is_primetime"] = 0.0
    else:
        df["is_primetime"] = 0.0

    # ── 17. Venue elevation ──────────────────────────────────────────────
    # Known NFL stadium elevations (ft) — Denver is the big altitude edge
    _venue_elev = {
        "DEN": 5280, "ARI": 1086, "LV": 2010, "WAS": 200, "PIT": 819,
        "PHI": 55, "BAL": 143, "BUF": 584, "SEA": 377, "ATL": 1050,
        "KC": 807, "CIN": 541, "DAL": 600, "HOU": 96, "GB": 640,
        "MIN": 870, "CAR": 653, "JAX": 16, "TEN": 504, "NE": 116,
        "MIA": 7, "CLE": 591, "NO": 6, "CHI": 610, "TB": 50,
        "IND": 712, "SF": 10, "DET": 636, "LA": 305,
    }
    if "home_abbr" in df.columns:
        df["venue_elevation_ft"] = (
            df["home_abbr"].map(_venue_elev).fillna(0.0)
        )
    else:
        df["venue_elevation_ft"] = 0.0

    # ── 18. Injury weight ────────────────────────────────────────────────
    # Placeholder — populated by engine for live predictions
    df["home_injury_weight"] = 0.0
    df["away_injury_weight"] = 0.0

    # ── 19. Team stats from nfl.game_stats (real data 2016-present) ──┐
    team_stats = kwargs.get("team_stats")
    if team_stats is not None and not team_stats.empty:
        _ts = team_stats
        logger.info(
            "Merging team stats for %d rows (need season_year + week columns)",
            len(df),
        )

        # Determine season column name (GAME_QUERY uses "season_year")
        season_col = "season_year" if "season_year" in df.columns else "season"

        # Rename _ts.season to match season_col so left_on/right_on names are
        # identical — avoids accumulating duplicate season/season_x/season_y
        # columns across multiple sequential merges.
        if season_col != "season" and "season" in _ts.columns:
            _ts = _ts.rename(columns={"season": season_col})

        # ── Home team offensive stats ──
        home_off = _ts.rename(columns={
            "team_abbr": "home_abbr",
            "off_ypg": "home_off_ypg",
            "ypp": "home_ypp",
            "pass_ypg": "home_pass_ypg",
            "rush_ypg": "home_rush_ypg",
            "pass_ypa": "home_pass_ypa",
            "rush_ypa": "home_rush_ypa",
            "turnover_diff": "home_turnover_diff_r5",
            "first_downs": "home_first_downs",
            "third_down_pct": "home_third_down_pct",
            "fourth_down_pct": "home_fourth_down_pct",
            "rz_trips": "home_rz_trips",
            "rz_td_pct": "home_rz_td_pct",
            "explosive_plays": "home_explosive_plays",
            "three_and_outs": "home_three_and_outs",
            "ints_thrown": "home_ints_thrown",
            "off_epa_per_play": "home_off_epa_per_play",
            "win_streak": "home_win_streak",
            "off_pts_stddev_5": "home_off_pts_stddev_5",
            "off_yds_stddev_5": "home_off_yds_stddev_5",
            "rw_off_ppg": "home_rw_off_ppg",
            "rw_off_ypg": "home_rw_off_ypg",
            "adj_off_ppg": "home_adj_off_ppg",
            "adj_off_ypg": "home_adj_off_ypg",
            "off_yardage_rank": "home_off_yardage_rank",
            "off_scoring_rank": "home_off_scoring_rank",
            "off_rushing_rank": "home_off_rushing_rank",
            "off_passing_rank": "home_off_passing_rank",
            "cold_ppg": "home_cold_ppg",
            "cold_ypg": "home_cold_ypg",
            "cold_win_pct": "home_cold_win_pct",
            "precip_ppg": "home_precip_ppg",
            "precip_ypg": "home_precip_ypg",
            "precip_win_pct": "home_precip_win_pct",
        })
        _ho_cols = [season_col, "feeds_into_game_id", "home_abbr",
                    "home_off_ypg", "home_ypp", "home_pass_ypg",
                    "home_rush_ypg", "home_pass_ypa", "home_rush_ypa",
                    "home_turnover_diff_r5",
                    "home_first_downs", "home_third_down_pct",
                    "home_fourth_down_pct", "home_rz_trips",
                    "home_rz_td_pct", "home_explosive_plays",
                    "home_three_and_outs", "home_ints_thrown",
                    "home_off_epa_per_play", "home_win_streak",
                    "home_off_pts_stddev_5", "home_off_yds_stddev_5",
                    "home_rw_off_ppg", "home_rw_off_ypg",
                    "home_adj_off_ppg", "home_adj_off_ypg",
                    "home_off_yardage_rank", "home_off_scoring_rank",
                    "home_off_rushing_rank", "home_off_passing_rank",
                    "home_cold_ppg", "home_cold_ypg", "home_cold_win_pct",
                    "home_precip_ppg", "home_precip_ypg", "home_precip_win_pct"]
        _ho_cols = [c for c in _ho_cols if c in home_off.columns]
        df = df.merge(
            home_off[_ho_cols],
            left_on=[season_col, "game_id", "home_abbr"],
            right_on=[season_col, "feeds_into_game_id", "home_abbr"],
            how="left",
        )
        df = df.drop(columns=["feeds_into_game_id"], errors="ignore")

        # ── Away team offensive stats ──
        away_off = _ts.rename(columns={
            "team_abbr": "away_abbr",
            "off_ypg": "away_off_ypg",
            "ypp": "away_ypp",
            "pass_ypg": "away_pass_ypg",
            "rush_ypg": "away_rush_ypg",
            "pass_ypa": "away_pass_ypa",
            "rush_ypa": "away_rush_ypa",
            "turnover_diff": "away_turnover_diff_r5",
            "first_downs": "away_first_downs",
            "third_down_pct": "away_third_down_pct",
            "fourth_down_pct": "away_fourth_down_pct",
            "rz_trips": "away_rz_trips",
            "rz_td_pct": "away_rz_td_pct",
            "explosive_plays": "away_explosive_plays",
            "three_and_outs": "away_three_and_outs",
            "ints_thrown": "away_ints_thrown",
            "off_epa_per_play": "away_off_epa_per_play",
            "win_streak": "away_win_streak",
            "off_pts_stddev_5": "away_off_pts_stddev_5",
            "off_yds_stddev_5": "away_off_yds_stddev_5",
            "rw_off_ppg": "away_rw_off_ppg",
            "rw_off_ypg": "away_rw_off_ypg",
            "adj_off_ppg": "away_adj_off_ppg",
            "adj_off_ypg": "away_adj_off_ypg",
            "off_yardage_rank": "away_off_yardage_rank",
            "off_scoring_rank": "away_off_scoring_rank",
            "off_rushing_rank": "away_off_rushing_rank",
            "off_passing_rank": "away_off_passing_rank",
            "cold_ppg": "away_cold_ppg",
            "cold_ypg": "away_cold_ypg",
            "cold_win_pct": "away_cold_win_pct",
            "precip_ppg": "away_precip_ppg",
            "precip_ypg": "away_precip_ypg",
            "precip_win_pct": "away_precip_win_pct",
        })
        _ao_cols = [season_col, "feeds_into_game_id", "away_abbr",
                    "away_off_ypg", "away_ypp", "away_pass_ypg",
                    "away_rush_ypg", "away_pass_ypa", "away_rush_ypa",
                    "away_turnover_diff_r5",
                    "away_first_downs", "away_third_down_pct",
                    "away_fourth_down_pct", "away_rz_trips",
                    "away_rz_td_pct", "away_explosive_plays",
                    "away_three_and_outs", "away_ints_thrown",
                    "away_off_epa_per_play", "away_win_streak",
                    "away_off_pts_stddev_5", "away_off_yds_stddev_5",
                    "away_rw_off_ppg", "away_rw_off_ypg",
                    "away_adj_off_ppg", "away_adj_off_ypg",
                    "away_off_yardage_rank", "away_off_scoring_rank",
                    "away_off_rushing_rank", "away_off_passing_rank",
                    "away_cold_ppg", "away_cold_ypg", "away_cold_win_pct",
                    "away_precip_ppg", "away_precip_ypg", "away_precip_win_pct"]
        _ao_cols = [c for c in _ao_cols if c in away_off.columns]
        df = df.merge(
            away_off[_ao_cols],
            left_on=[season_col, "game_id", "away_abbr"],
            right_on=[season_col, "feeds_into_game_id", "away_abbr"],
            how="left",
        )
        df = df.drop(columns=["feeds_into_game_id"], errors="ignore")

        # ── Home team defensive stats ──
        # Home team's defense = the home team's own def_ypg (yards allowed)
        home_def = _ts.rename(columns={
            "team_abbr": "home_abbr",
            "def_ypg": "home_def_ypg",
            "def_ypp": "home_def_ypp",
            "def_pass_ypg": "home_def_pass_ypg",
            "def_rush_ypg": "home_def_rush_ypg",
            "def_first_downs": "home_def_first_downs",
            "def_third_down_pct": "home_def_third_down_pct",
            "def_fourth_down_pct": "home_def_fourth_down_pct",
            "def_rz_trips": "home_def_rz_trips",
            "def_rz_td_pct": "home_def_rz_td_pct",
            "def_explosive_plays": "home_def_explosive_plays",
            "def_three_and_outs": "home_def_three_and_outs",
            "def_ints_thrown": "home_def_ints_thrown",
            "def_epa_per_play": "home_def_epa_per_play",
            "def_pts_stddev_5": "home_def_pts_stddev_5",
            "def_yds_stddev_5": "home_def_yds_stddev_5",
            "rw_def_ppg": "home_rw_def_ppg",
            "rw_def_ypg": "home_rw_def_ypg",
            "adj_def_ppg": "home_adj_def_ppg",
            "adj_def_ypg": "home_adj_def_ypg",
            "def_yardage_rank": "home_def_yardage_rank",
            "def_scoring_rank": "home_def_scoring_rank",
            "def_rushing_rank": "home_def_rushing_rank",
            "def_passing_rating_rank": "home_def_passing_rating_rank",
        })
        _hd_cols = [season_col, "feeds_into_game_id", "home_abbr",
                    "home_def_ypg", "home_def_ypp",
                    "home_def_pass_ypg", "home_def_rush_ypg",
                    "home_def_first_downs", "home_def_third_down_pct",
                    "home_def_fourth_down_pct", "home_def_rz_trips",
                    "home_def_rz_td_pct", "home_def_explosive_plays",
                    "home_def_three_and_outs", "home_def_ints_thrown",
                    "home_def_epa_per_play", "home_def_pts_stddev_5",
                    "home_def_yds_stddev_5", "home_rw_def_ppg",
                    "home_rw_def_ypg", "home_adj_def_ppg",
                    "home_adj_def_ypg",
                    "home_def_yardage_rank", "home_def_scoring_rank",
                    "home_def_rushing_rank", "home_def_passing_rating_rank"]
        _hd_cols = [c for c in _hd_cols if c in home_def.columns]
        df = df.merge(
            home_def[_hd_cols],
            left_on=[season_col, "game_id", "home_abbr"],
            right_on=[season_col, "feeds_into_game_id", "home_abbr"],
            how="left",
        )
        df = df.drop(columns=["feeds_into_game_id"], errors="ignore")

        # ── Away team defensive stats ──
        away_def = _ts.rename(columns={
            "team_abbr": "away_abbr",
            "def_ypg": "away_def_ypg",
            "def_ypp": "away_def_ypp",
            "def_pass_ypg": "away_def_pass_ypg",
            "def_rush_ypg": "away_def_rush_ypg",
            "def_first_downs": "away_def_first_downs",
            "def_third_down_pct": "away_def_third_down_pct",
            "def_fourth_down_pct": "away_def_fourth_down_pct",
            "def_rz_trips": "away_def_rz_trips",
            "def_rz_td_pct": "away_def_rz_td_pct",
            "def_explosive_plays": "away_def_explosive_plays",
            "def_three_and_outs": "away_def_three_and_outs",
            "def_ints_thrown": "away_def_ints_thrown",
            "def_epa_per_play": "away_def_epa_per_play",
            "def_pts_stddev_5": "away_def_pts_stddev_5",
            "def_yds_stddev_5": "away_def_yds_stddev_5",
            "rw_def_ppg": "away_rw_def_ppg",
            "rw_def_ypg": "away_rw_def_ypg",
            "adj_def_ppg": "away_adj_def_ppg",
            "adj_def_ypg": "away_adj_def_ypg",
            "def_yardage_rank": "away_def_yardage_rank",
            "def_scoring_rank": "away_def_scoring_rank",
            "def_rushing_rank": "away_def_rushing_rank",
            "def_passing_rating_rank": "away_def_passing_rating_rank",
        })
        _ad_cols = [season_col, "feeds_into_game_id", "away_abbr",
                    "away_def_ypg", "away_def_ypp",
                    "away_def_pass_ypg", "away_def_rush_ypg",
                    "away_def_first_downs", "away_def_third_down_pct",
                    "away_def_fourth_down_pct", "away_def_rz_trips",
                    "away_def_rz_td_pct", "away_def_explosive_plays",
                    "away_def_three_and_outs", "away_def_ints_thrown",
                    "away_def_epa_per_play", "away_def_pts_stddev_5",
                    "away_def_yds_stddev_5", "away_rw_def_ppg",
                    "away_rw_def_ypg", "away_adj_def_ppg",
                    "away_adj_def_ypg",
                    "away_def_yardage_rank", "away_def_scoring_rank",
                    "away_def_rushing_rank", "away_def_passing_rating_rank"]
        _ad_cols = [c for c in _ad_cols if c in away_def.columns]
        df = df.merge(
            away_def[_ad_cols],
            left_on=[season_col, "game_id", "away_abbr"],
            right_on=[season_col, "feeds_into_game_id", "away_abbr"],
            how="left",
        )

        # ── Fill NAs for games without game_stats data ──
        stat_suffixes = [
            "off_ypg", "ypp", "pass_ypg", "rush_ypg",
            "pass_ypa", "rush_ypa", "turnover_diff_r5",
            "def_ypg", "def_ypp",
            "def_pass_ypg", "def_rush_ypg",
            # PBP-derived offensive features
            "first_downs", "third_down_pct", "fourth_down_pct",
            "rz_trips", "rz_td_pct",
            "explosive_plays", "three_and_outs", "ints_thrown",
            # PBP-derived defensive features
            "def_first_downs", "def_third_down_pct", "def_fourth_down_pct",
            "def_rz_trips", "def_rz_td_pct",
            "def_explosive_plays", "def_three_and_outs", "def_ints_thrown",
            "off_epa_per_play", "win_streak",
            "off_pts_stddev_5", "off_yds_stddev_5",
            "rw_off_ppg", "rw_off_ypg",
            "adj_off_ppg", "adj_off_ypg",
            "def_epa_per_play",
            "def_pts_stddev_5", "def_yds_stddev_5",
            "rw_def_ppg", "rw_def_ypg",
            "adj_def_ppg", "adj_def_ypg",
            # Rankings (display only, not trainable)
            "off_yardage_rank",
            "def_yardage_rank",
            "off_scoring_rank",
            "def_scoring_rank",
            "off_rushing_rank",
            "def_rushing_rank",
            "off_passing_rank",
            "def_passing_rating_rank",
        ]
        # Mapping from stat_suffix (team_stats alias) to prior_map key
        # Mapping from team_stats suffix (after alias) to prior_team_stats column
        _suffix_to_prior = {
            "off_ypg": "off_ypg",
            "ypp": "off_ypp",
            "pass_ypg": "off_pass_ypg",
            "rush_ypg": "off_rush_ypg",
            "pass_ypa": "off_ypa",
            "rush_ypa": None,  # filled via prior_rolling_map fallback below
            "turnover_diff_r5": "turnover_diff_r5",
            "def_ypg": "def_ypg",
            "def_ypp": "def_ypp",
            "def_pass_ypg": "def_pass_ypg",
            "def_rush_ypg": "def_rush_ypg",
            "first_downs": "off_first_downs",
            "third_down_pct": "off_third_down_pct",
            "fourth_down_pct": "off_fourth_down_pct",
            "rz_trips": "off_rz_trips",
            "rz_td_pct": "off_rz_td_pct",
            "explosive_plays": "off_explosive_rate",
            "three_and_outs": "off_three_and_out_rate",
            "ints_thrown": "off_ints_thrown",
            "def_first_downs": "def_first_downs",
            "def_third_down_pct": "def_third_down_pct",
            "def_fourth_down_pct": "def_fourth_down_pct",
            "def_rz_trips": "def_rz_trips",
            "def_rz_td_pct": "off_rz_td_pct",  # proxy: opponent's RZ TD rate ≈ def_rz_td_pct
            "def_explosive_plays": "def_explosive_rate",
            "def_three_and_outs": "def_three_and_out_rate",
            "def_ints_thrown": "def_ints_thrown",
            "off_epa_per_play": "off_epa_per_play",
            "off_pts_stddev_5": "off_pts_stddev_5",
            "off_yds_stddev_5": "off_yds_stddev_5",
            "rw_off_ppg": "rw_off_ppg",
            "rw_off_ypg": "rw_off_ypg",
            "adj_off_ppg": "adj_off_ppg",
            "adj_off_ypg": "adj_off_ypg",
            "def_epa_per_play": "def_epa_per_play",
            "def_pts_stddev_5": "def_pts_stddev_5",
            "def_yds_stddev_5": "def_yds_stddev_5",
            "rw_def_ppg": "rw_def_ppg",
            "rw_def_ypg": "rw_def_ypg",
            "adj_def_ppg": "adj_def_ppg",
            "adj_def_ypg": "adj_def_ypg",
            "win_streak": "win_streak",
        }
        # Rolling-table source columns for stats that lack a prior_team_stats
        # key (e.g. rush_ypa). team_rolling_stats keeps the true prior-season
        # rolling values; used to seed week-1 holes.
        _suffix_to_rolling_src = {
            "rush_ypa": "rush_ypa_r5",
        }
        for prefix in ["home", "away"]:
            for suffix in stat_suffixes:
                col = f"{prefix}_{suffix}"
                if col not in df.columns:
                    continue
                # week 1 games: fill from prior_team_stats (cumulative data is empty)
                prior_key = _suffix_to_prior.get(suffix)
                if prior_key is not None:
                    abbr_col = f"{prefix}_abbr"
                    if abbr_col in df.columns:
                        # MLB-style COALESCE across the whole season: fill any
                        # missing/zero pre-game stat with previous-season value.
                        # This seeds early-season games (Week 1+ before rolling
                        # windows fill) instead of leaving 0s that distort the model.
                        def _prior_fill(r):
                            cur = r.get(col)
                            if cur is not None and not pd.isna(cur) and cur != 0:
                                return cur
                            return prior_map.get((r[abbr_col], r["season_year"] - 1), {}).get(prior_key, 0.0)
                        df[col] = df.apply(_prior_fill, axis=1)
                else:
                    # No prior_team_stats key (e.g. rush_ypa) — fall back to the
                    # prior-season team_rolling_stats (last REG row) so week-1
                    # games get a real prior value instead of NaN.
                    rolling_col = _suffix_to_rolling_src.get(suffix)
                    if rolling_col:
                        def _prior_fill(r, _s=rolling_col, _p=prefix):
                            cur = r.get(col)
                            if cur is not None and not pd.isna(cur) and cur != 0:
                                return cur
                            rec = prior_rolling_map.get((r[abbr_col], r["season_year"] - 1))
                            if not rec:
                                return cur
                            pv = rec.get(_s)
                            if pv is None or (isinstance(pv, float) and pv != pv):
                                return cur
                            return float(pv)
                        df[col] = df.apply(_prior_fill, axis=1)
                # NOTE: leave any column still missing here as NaN instead of
                # blind-0 filling. A team stat with NO prior-season and NO current
                # data (brand-new team / stat absent from prior_team_stats) stays
                # NaN so the PICK CARD blanks it (a missing home_off_ypg must not
                # read as 0 yds/gm). The MODEL imputes a league-average via
                # _impute_feature — never a hard 0.

        logger.info(
            "Team stats merged: home_off_ypg non-null count = %d",
            int(df["home_off_ypg"].notna().sum()),
        )

    else:
        # No team_stats available — use zeros as safe defaults
        for prefix in ["home", "away"]:
            for suffix in [
                "off_ypg", "ypp", "pass_ypg", "rush_ypg",
                "pass_ypa", "rush_ypa", "turnover_diff_r5",
                "def_ypg", "def_ypp",
                "first_downs", "third_down_pct", "fourth_down_pct",
                "rz_trips", "rz_td_pct",
                "explosive_plays", "three_and_outs", "ints_thrown",
                # Rankings
                "off_yardage_rank",
                "def_yardage_rank",
                "off_scoring_rank",
                "def_scoring_rank",
                "off_rushing_rank",
                "def_rushing_rank",
                "off_passing_rank",
                "def_passing_rating_rank",
            ]:
                df[f"{prefix}_{suffix}"] = 0.0

        # Drop temporary merge columns
        for c in ["season", "week"]:
            if c in df.columns and "_x" not in str(c):
                # season/week might exist from GAME_QUERY — don't drop
                pass

    # ── 20. QB pre-game stats (cumulative + rolling) ──
    qb_stats = kwargs.get("qb_stats")
    if qb_stats is not None and not qb_stats.empty:
        qb_merge = qb_stats.drop(columns=["team_id"], errors="ignore")
        df = df.merge(qb_merge, on="game_id", how="left")

        # MLB-style prior-season COALESCE for QB pre-game stats: when the
        # current-season value is missing/0 (e.g. Week 1 before any starts this
        # season), fall back to the QB's final game of the previous season
        # (`*_prev` columns). Then drop the `_prev` columns so they don't
        # become standalone features.
        _qb_prev_pairs = {
            # (current-season column, prior-season column)
            ("home_qb_games_season", "home_qb_games_season_prev"),
            ("home_qb_passer_rating_season", "home_qb_passer_rating_season_prev"),
            ("home_qb_any_a_season", "home_qb_any_a_season_prev"),
            ("home_qb_ypa_season", "home_qb_ypa_season_prev"),
            ("home_qb_td_pct_season", "home_qb_td_pct_season_prev"),
            ("home_qb_int_pct_season", "home_qb_int_pct_season_prev"),
            ("home_qb_sack_rate_season", "home_qb_sack_rate_season_prev"),
            ("home_qb_rush_ypg_season", "home_qb_rush_ypg_season_prev"),
            ("home_qb_rush_att_pg_season", "home_qb_rush_att_pg_season_prev"),
            ("home_qb_games_5", "home_qb_games_5_prev"),
            ("home_qb_passer_rating_5", "home_qb_passer_rating_5_prev"),
            ("home_qb_any_a_5", "home_qb_any_a_5_prev"),
            ("home_qb_ypa_5", "home_qb_ypa_5_prev"),
            ("home_qb_td_pct_5", "home_qb_td_pct_5_prev"),
            ("home_qb_int_pct_5", "home_qb_int_pct_5_prev"),
            ("home_qb_sack_rate_5", "home_qb_sack_rate_5_prev"),
            ("home_qb_rush_ypg_5", "home_qb_rush_ypg_5_prev"),
            ("home_qb_rush_att_5", "home_qb_rush_att_5_prev"),
            ("away_qb_games_season", "away_qb_games_season_prev"),
            ("away_qb_passer_rating_season", "away_qb_passer_rating_season_prev"),
            ("away_qb_any_a_season", "away_qb_any_a_season_prev"),
            ("away_qb_ypa_season", "away_qb_ypa_season_prev"),
            ("away_qb_td_pct_season", "away_qb_td_pct_season_prev"),
            ("away_qb_int_pct_season", "away_qb_int_pct_season_prev"),
            ("away_qb_sack_rate_season", "away_qb_sack_rate_season_prev"),
            ("away_qb_rush_ypg_season", "away_qb_rush_ypg_season_prev"),
            ("away_qb_rush_att_pg_season", "away_qb_rush_att_pg_season_prev"),
            ("away_qb_games_5", "away_qb_games_5_prev"),
            ("away_qb_passer_rating_5", "away_qb_passer_rating_5_prev"),
            ("away_qb_any_a_5", "away_qb_any_a_5_prev"),
            ("away_qb_ypa_5", "away_qb_ypa_5_prev"),
            ("away_qb_td_pct_5", "away_qb_td_pct_5_prev"),
            ("away_qb_int_pct_5", "away_qb_int_pct_5_prev"),
            ("away_qb_sack_rate_5", "away_qb_sack_rate_5_prev"),
            ("away_qb_rush_ypg_5", "away_qb_rush_ypg_5_prev"),
            ("away_qb_rush_att_5", "away_qb_rush_att_5_prev"),
        }
        _qb_prev_cols = set()
        for _cur, _prev in _qb_prev_pairs:
            if _cur in df.columns and _prev in df.columns:
                # Only backfill a genuinely MISSING current-season value. Do NOT
                # treat a legitimate 0.0 (e.g. 0 INTs / 0 sacks in a 5-game
                # window) as if it were absent — overwriting it with the
                # prior-season figure destroys a real, meaningful signal.
                mask = df[_cur].isna()
                df.loc[mask, _cur] = df.loc[mask, _prev]
                _qb_prev_cols.add(_prev)
        if _qb_prev_cols:
            df = df.drop(columns=list(_qb_prev_cols), errors="ignore")

        # QB differentials (home minus away)
        computed_qb = pd.DataFrame({
            "qb_passer_rating_5_diff": (
                df["home_qb_passer_rating_5"] - df["away_qb_passer_rating_5"]
            ),
            "qb_any_a_5_diff": (
                df["home_qb_any_a_5"] - df["away_qb_any_a_5"]
            ),
            "qb_passer_rating_season_diff": (
                df["home_qb_passer_rating_season"] - df["away_qb_passer_rating_season"]
            ),
            "qb_any_a_season_diff": (
                df["home_qb_any_a_season"] - df["away_qb_any_a_season"]
            ),
            "home_qb_passer_rating_trend": (
                df["home_qb_passer_rating_5"] - df["home_qb_passer_rating_season"]
            ),
            "away_qb_passer_rating_trend": (
                df["away_qb_passer_rating_5"] - df["away_qb_passer_rating_season"]
            ),
            "home_qb_ypa_trend": (
                df["home_qb_ypa_5"] - df["home_qb_ypa_season"]
            ),
            "away_qb_ypa_trend": (
                df["away_qb_ypa_5"] - df["away_qb_ypa_season"]
            ),
        })
        df = pd.concat([df, computed_qb], axis=1)

        # Fill NaN (Week 1 or no prior QB data)
        qb_feat_cols = [
            c for c in qb_merge.columns if c != "game_id"
        ] + [
            "qb_passer_rating_5_diff", "qb_any_a_5_diff",
            "qb_passer_rating_season_diff", "qb_any_a_season_diff",
            "home_qb_passer_rating_trend", "away_qb_passer_rating_trend",
            "home_qb_ypa_trend", "away_qb_ypa_trend",
        ]
        qb_present = [c for c in qb_feat_cols if c in df.columns]
        # NOTE: intentionally do NOT fillna(0.0) here. A missing QB stat (rookie /
        # backup QB with <5 games, or no prior-season) must stay NaN so the PICK
        # CARD blanks it (never shows a fake 0.0 rating). The MODEL fills a
        # reasoned prior via engine._impute_feature (season -> prior-season ->
        # league-neutral), never blind-0.

        logger.info("Merged %d QB feature columns", len(qb_present))
    else:
        # No QB stats available — build the QB columns as NaN (NOT zero-filled).
        # Same principle as above: pick card blanks, model imputes a reasoned prior.
        qb_feat_names = [
            "home_qb_passer_rating_5", "away_qb_passer_rating_5",
            "home_qb_any_a_5", "away_qb_any_a_5",
            "home_qb_ypa_5", "away_qb_ypa_5",
            "home_qb_td_pct_5", "away_qb_td_pct_5",
            "home_qb_int_pct_5", "away_qb_int_pct_5",
            "home_qb_sack_rate_5", "away_qb_sack_rate_5",
            "home_qb_rush_ypg_5", "away_qb_rush_ypg_5",
            "home_qb_rush_att_5", "away_qb_rush_att_5",
            "home_qb_games_5", "away_qb_games_5",
            "home_qb_passer_rating_season", "away_qb_passer_rating_season",
            "home_qb_any_a_season", "away_qb_any_a_season",
            "home_qb_ypa_season", "away_qb_ypa_season",
            "home_qb_td_pct_season", "away_qb_td_pct_season",
            "home_qb_int_pct_season", "away_qb_int_pct_season",
            "home_qb_sack_rate_season", "away_qb_sack_rate_season",
            "home_qb_rush_ypg_season", "away_qb_rush_ypg_season",
            "home_qb_rush_att_pg_season", "away_qb_rush_att_pg_season",
            "home_qb_games_season", "away_qb_games_season",
            "qb_passer_rating_5_diff", "qb_any_a_5_diff",
            "qb_passer_rating_season_diff", "qb_any_a_season_diff",
            "home_qb_passer_rating_trend", "away_qb_passer_rating_trend",
            "home_qb_ypa_trend", "away_qb_ypa_trend",
        ]
        nan_fill = {col: float("nan") for col in qb_feat_names}
        df = pd.concat([df, pd.DataFrame(nan_fill, index=df.index)], axis=1)

        logger.debug("No QB stats available — zero-filled %d QB features", len(qb_feat_names))

    logger.info(
        "build_features complete: %d rows, %d features",
        len(df), len(df.columns),
    )

    # ── Venue / weather / league-context features ──────────────────────────
    # These catalog features (is_dome, temp, wind, surface, season_avg_pts) are
    # derived from the raw games row that load_games already carries, not from
    # team/QB stats. Populate them so the admin page + pick cards show real
    # values (and blanks honestly when the underlying weather rows are NULL).
    try:
        _roof = df["roof_type"] if "roof_type" in df.columns else None
        if "is_dome" not in df.columns and _roof is not None:
            df["is_dome"] = _roof.str.lower().isin({"dome", "retractable", "indoor"}).astype(int)
        elif "is_dome" not in df.columns:
            df["is_dome"] = 0
        if "is_dome" in df.columns and _roof is not None:
            df["is_dome"] = _roof.str.lower().isin({"dome", "retractable", "indoor"}).astype(int)

        if "temp" not in df.columns and "temperature" in df.columns:
            df["temp"] = df["temperature"]
        if "wind" not in df.columns and "wind_speed" in df.columns:
            df["wind"] = df["wind_speed"]
        if "surface" not in df.columns and "surface" in df.columns:
            pass  # already present via load_games context col

        # Season-average points: league-wide mean of team points per game, added
        # as a constant column (matches "League average points per team").
        if "season_avg_pts" not in df.columns:
            _pts_cols = [c for c in df.columns if c.endswith("_pts") and not c.startswith("season")]
            df["season_avg_pts"] = float("nan")
            if _pts_cols:
                _pts = df[_pts_cols].replace([float("inf"), float("-inf")], float("nan"))
                _med = _pts.notna().sum(axis=1)
                _sum = _pts.sum(axis=1, skipna=True)
                df["season_avg_pts"] = (_sum / _med.replace(0, 1)).where(_med > 0)
        logger.debug("Venue/weather/league features derived")
    except Exception as _vw_exc:
        logger.warning("Venue/weather/league feature derivation failed: %s", _vw_exc)

    return df


# ── Factory / singleton ────────────────────────────────────────────────────────

def get_data_loader(
    db_url: Optional[str] = None,
    ats_only: bool = False,
    ou_only: bool = False,
) -> NFLDataLoader:
    """Create (or return cached) NFLDataLoader singleton.

    Parameters
    ----------
    db_url : str, optional
        Database URL.
    ats_only : bool
        Default to ATS-only features.
    ou_only : bool
        Default to OU-only features.
    """
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = NFLDataLoader(
            db_url=db_url,
            ats_only=ats_only,
            ou_only=ou_only,
        )
    return _loader_instance


_loader_instance: Optional[NFLDataLoader] = None


# ── CLI / smoke test ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    loader = get_data_loader()
    logger.info("Loader: %s", loader)

    # Smoke test: load a small batch
    df = loader.load_data(seasons=[2024], limit=10)
    logger.info("Got %d rows x %d cols", *df.shape)

    if not df.empty:
        print(df.head(3).to_string())
        print()
        logger.info("Features used: %s", list(df.columns))
        logger.info("Features listed in catalog: %d", len(loader.get_feature_names()))
