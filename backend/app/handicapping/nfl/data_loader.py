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
DEFAULT_DB_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)


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
    gr.away_last_game
FROM nfl.games g
JOIN nfl.teams ht ON ht.id = g.home_team_id
JOIN nfl.teams at ON at.id = g.away_team_id
LEFT JOIN game_lines gl ON gl.game_id = g.id
LEFT JOIN game_rest gr ON gr.game_id = g.id
LEFT JOIN nfl.seasons s ON s.id = g.season_id
WHERE g.season_id IS NOT NULL
  AND g.week IS NOT NULL
ORDER BY g.season_id, g.week, g.date;
"""


# ── Features Catalog ────────────────────────────────────────────────────────────
# Maps every feature name (as stored in nfl.features) to a human description.
# Populated from the database on first loader use; the static dict below is a
# fallback / documentation cache.

FEATURES_CATALOG: Dict[str, str] = {
    "hpf": "Home team points for (PPG)",
    "hpa": "Home team points against (PPG)",
    "apf": "Away team points for (PPG)",
    "apa": "Away team points against (PPG)",
    "dpf": "Defensive points for (adjusted)",
    "dpa": "Defensive points against (adjusted)",
    "himp": "Home implied scoring",
    "aimp": "Away implied scoring",
    "dimp": "Differential implied (home - away)",
    "spread_movement": "Spread movement (closing - opening)",
    "sp_h_odds_mvmt": "Home spread odds movement",
    "sp_a_odds_mvmt": "Away spread odds movement",
    "home_win_pct_r5": "Home team win % last 5 games",
    "away_win_pct_r5": "Away team win % last 5 games",
    "home_margin_r3": "Home team avg margin last 3 games",
    "away_margin_r3": "Away team avg margin last 3 games",
    "home_cover_pct_r5": "Home team ATS cover % last 5 games",
    "away_cover_pct_r5": "Away team ATS cover % last 5 games",
    "home_embarrassed": 'Home team "embarrassed" (lost by 14+ last game)',
    "away_embarrassed": 'Away team "embarrassed" (lost by 14+ last game)',
    "home_season_ats_pct": "Home team season-wide ATS cover %",
    "away_season_ats_pct": "Away team season-wide ATS cover %",
    "home_margin_r10": "Home team avg margin last 10 games",
    "away_margin_r10": "Away team avg margin last 10 games",
    "travel_miles": "Away team travel distance in miles",
    "is_dome": "Home stadium dome / retractable roof",
    "opening_ou": "Opening over/under line",
    "spread": "Closing spread",
    "ou_movement": "Closing OU minus opening OU",
    "rest_diff": "Rest day differential (home - away)",
    "tz_diff": "Timezone difference home - away (hours)",
    "is_short": "Short week indicator",
    "temp": "Game-time temperature (F)",
    "wind": "Game-time wind speed (mph)",
    "season_year": "Calendar year this game belongs to",
    "season_avg_pts": "League average points per team per game for the season",
}

# Features that are computed from raw columns rather than read directly.
# These appear in the DataFrame alongside the raw features.
COMPUTED_FEATURES_CATALOG: Dict[str, str] = {
    "home_ats_cover": "Home team covered the spread (1=yes, 0=no, NaN=pick)",
    "away_ats_cover": "Away team covered the spread (1=yes, 0=no, NaN=pick)",
    "over_result": "Game went over the total (1=over, 0=under, NaN=push)",
    "home_score_margin": "Home score - away score",
    "home_pts_differential": "Home PF - Home PA (rolling)",
    "away_pts_differential": "Away PF - Away PA (rolling)",
    "home_strength": "Home team power rating (PF - PA with SOS adjustment)",
    "away_strength": "Away team power rating (PF - PA with SOS adjustment)",
    "home_implied_pts": "Home team implied points from closing spread + OU",
    "away_implied_pts": "Away team implied points from closing spread + OU",
    "home_rest_days": "Home team rest days since last game",
    "away_rest_days": "Away team rest days since last game",

    # ── New features (added 2026-07-06) ──────────────────────────────────
    "is_division_game": "Division matchup flag",
    "is_primetime": "Primetime game flag",
    "venue_elevation_ft": "Stadium elevation (ft)",
    "home_ou_over_pct_r5": "Home OU Over% L5",
    "away_ou_over_pct_r5": "Away OU Over% L5",
    "home_ou_margin_r5": "Home OU Margin L5",
    "away_ou_margin_r5": "Away OU Margin L5",
    "home_ats_home_pct_r5": "Home ATS-Home% L5",
    "away_ats_away_pct_r5": "Away ATS-Away% L5",
    "home_win_streak": "Home Win Streak",
    "away_win_streak": "Away Win Streak",
    "home_ats_streak": "Home ATS Streak",
    "away_ats_streak": "Away ATS Streak",
    "home_weighted_margin_r5": "Home Wt'd Margin L5",
    "away_weighted_margin_r5": "Away Wt'd Margin L5",
    "home_off_ypg": "Home Off YPG",
    "away_off_ypg": "Away Off YPG",
    "home_def_ypg": "Home Def YPG",
    "away_def_ypg": "Away Def YPG",
    "home_ypp": "Home YPP",
    "away_ypp": "Away YPP",
    "home_def_ypp": "Home Def YPP",
    "away_def_ypp": "Away Def YPP",
    "home_pass_ypg": "Home Pass YPG",
    "away_pass_ypg": "Away Pass YPG",
    "home_rush_ypg": "Home Rush YPG",
    "away_rush_ypg": "Away Rush YPG",
    "home_pass_ypa": "Home Pass YPA",
    "away_pass_ypa": "Away Pass YPA",
    "home_rush_ypa": "Home Rush YPA",
    "away_rush_ypa": "Away Rush YPA",
    "home_turnover_diff_r5": "Home TO Diff L5",
    "away_turnover_diff_r5": "Away TO Diff L5",
    "home_def_pass_ypg": "Home Def Pass YPG",
    "away_def_pass_ypg": "Away Def Pass YPG",
    "home_def_rush_ypg": "Home Def Rush YPG",
    "away_def_rush_ypg": "Away Def Rush YPG",
    "home_injury_weight": "Home Injury Weight",
    "away_injury_weight": "Away Injury Weight",
}

# Human-readable short labels for every feature (matches nfl.features.display_name)
DISPLAY_NAMES: Dict[str, str] = {
    "hpf": "Home PF",
    "hpa": "Home PA",
    "apf": "Away PF",
    "apa": "Away PA",
    "dpf": "Def PF",
    "dpa": "Def PA",
    "himp": "Home Implied",
    "aimp": "Away Implied",
    "dimp": "Diff Implied",
    "spread_movement": "Spread Movement",
    "sp_h_odds_mvmt": "SP Home Odds MV",
    "sp_a_odds_mvmt": "SP Away Odds MV",
    "home_win_pct_r5": "Home W% L5",
    "away_win_pct_r5": "Away W% L5",
    "home_margin_r3": "Home Margin L3",
    "away_margin_r3": "Away Margin L3",
    "home_cover_pct_r5": "Home Cover% L5",
    "away_cover_pct_r5": "Away Cover% L5",
    "home_embarrassed": "Home Embarrassed",
    "away_embarrassed": "Away Embarrassed",
    "home_season_ats_pct": "Home Season ATS%",
    "away_season_ats_pct": "Away Season ATS%",
    "home_margin_r10": "Home Margin L10",
    "away_margin_r10": "Away Margin L10",
    "travel_miles": "Travel Miles",
    "is_dome": "Dome",
    "opening_ou": "Opening OU",
    "spread": "Spread",
    "ou_movement": "OU Movement",
    "rest_diff": "Rest Diff",
    "tz_diff": "TZ Diff",
    "is_short": "Short Week",
    "temp": "Temperature",
    "wind": "Wind",
    "season_year": "Season",
    "season_avg_pts": "Season Avg Pts",
    # computed
    "home_ats_cover": "Home ATS Cover",
    "away_ats_cover": "Away ATS Cover",
    "over_result": "Over Result",
    "home_score_margin": "Home Margin",
    "home_pts_differential": "Home Pt Diff",
    "away_pts_differential": "Away Pt Diff",
    "home_strength": "Home Strength",
    "away_strength": "Away Strength",
    "home_implied_pts": "Home Imp Pts",
    "away_implied_pts": "Away Imp Pts",
    "home_rest_days": "Home Rest",
    "away_rest_days": "Away Rest",

    # ── New features (added 2026-07-06) ─────────────────────────────────────
    "is_division_game": "Division",
    "is_primetime": "Primetime",
    "venue_elevation_ft": "Elevation",
    "home_ou_over_pct_r5": "OU Over% L5 H",
    "away_ou_over_pct_r5": "OU Over% L5 A",
    "home_ou_margin_r5": "OU Margin L5 H",
    "away_ou_margin_r5": "OU Margin L5 A",
    "home_ats_home_pct_r5": "ATS@Home L5",
    "away_ats_away_pct_r5": "ATS@Away L5",
    "home_win_streak": "Win Str H",
    "away_win_streak": "Win Str A",
    "home_ats_streak": "ATS Str H",
    "away_ats_streak": "ATS Str A",
    "home_weighted_margin_r5": "Wt Margin H",
    "away_weighted_margin_r5": "Wt Margin A",
    "home_off_ypg": "Off YPG H",
    "away_off_ypg": "Off YPG A",
    "home_def_ypg": "Def YPG H",
    "away_def_ypg": "Def YPG A",
    "home_ypp": "YPP H",
    "away_ypp": "YPP A",
    "home_def_ypp": "Def YPP H",
    "away_def_ypp": "Def YPP A",
    "home_pass_ypg": "Pass YPG H",
    "away_pass_ypg": "Pass YPG A",
    "home_rush_ypg": "Rush YPG H",
    "away_rush_ypg": "Rush YPG A",
    "home_pass_ypa": "Pass YPA H",
    "away_pass_ypa": "Pass YPA A",
    "home_rush_ypa": "Rush YPA H",
    "away_rush_ypa": "Rush YPA A",
    "home_turnover_diff_r5": "TO Dff H",
    "away_turnover_diff_r5": "TO Dff A",
    "home_def_pass_ypg": "D-Pass YPG H",
    "away_def_pass_ypg": "D-Pass YPG A",
    "home_def_rush_ypg": "D-Rush YPG H",
    "away_def_rush_ypg": "D-Rush YPG A",
    "home_injury_weight": "Inj Wt H",
    "away_injury_weight": "Inj Wt A",
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
        local ``earl:earl2025@localhost:5432/earl_knows_football`` fallback.
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
    ):
        self.db_url: str = db_url or DEFAULT_DB_URL
        self.ats_only: bool = ats_only
        self.ou_only: bool = ou_only
        self._engine: Any = None
        logger.info(
            "NFLDataLoader initialized (ats_only=%s, ou_only=%s)",
            ats_only, ou_only,
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

    # ── Feature catalog helpers ────────────────────────────────────────────────

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full features catalog dict (name → description)."""
        return {**FEATURES_CATALOG, **COMPUTED_FEATURES_CATALOG}

    def get_feature_names(self) -> List[str]:
        """Return all known feature names."""
        return list(self.get_features_catalog().keys())

    def get_feature_description(self, name: str) -> Optional[str]:
        """Return the description for a single feature."""
        return self.get_features_catalog().get(name)

    def get_display_name(self, name: str) -> str:
        """Return the human-friendly display label for a feature."""
        return DISPLAY_NAMES.get(name, name)

    def get_all_with_display(self) -> Dict[str, str]:
        """Return all features with their display names."""
        return {name: self.get_display_name(name) for name in self.get_feature_names()}

    # ── Query building ──────────────────────────────────────────────────────────

    def _build_query(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
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
            conditions.append(f"g.season_id IN ({placeholders})")

        if status is not None:
            conditions.append(f"g.status = '{status}'")
        elif include_upcoming and not game_ids:
            conditions.append("g.status IS NOT NULL")

        if game_ids:
            ids_str = ", ".join(str(i) for i in game_ids)
            conditions.append(f"g.id IN ({ids_str})")

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
    ) -> pd.DataFrame:
        """Execute the game query and return raw DataFrame."""
        sql = self._build_query(
            seasons=seasons,
            status=status,
            limit=limit,
            include_upcoming=include_upcoming,
            game_ids=game_ids,
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
        )

        if df.empty:
            logger.warning("No games returned — returning empty DataFrame")
            return df

        # 2. Load team stats from nfl.game_stats (real NFL stats 2016-present)
        team_stats = None
        try:
            from .team_stats import compute_team_game_aggregates

            ts_df = compute_team_game_aggregates(self.engine)
            if not ts_df.empty:
                # Keep columns needed for merge (season+week+team_abbr to match GAME_QUERY)
                team_stats = ts_df[
                    ["season", "week", "team_abbr", "opp_abbr",
                     "off_ypg", "ypp", "pass_ypg", "rush_ypg",
                     "pass_ypa", "rush_ypa", "turnover_diff",
                     "def_ypg", "def_ypp",
                     "def_pass_ypg", "def_rush_ypg"]
                ].copy()
                logger.info(
                    "Loaded %d team-game stat rows from nfl.game_stats (%d-%d)",
                    len(team_stats),
                    int(team_stats["season"].min()),
                    int(team_stats["season"].max()),
                )
        except ImportError:
            logger.debug("team_stats module not available — skipping")
        except Exception as exc:
            logger.warning("Failed to load team stats: %s", exc)

        # 3. Run feature engineering
        fn = build_features_fn if build_features_fn is not None else build_features
        df = fn(df, team_stats=team_stats, **build_kwargs)

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
            "over_result", "home_score_margin",
            "home_score", "away_score", "closing_ou", "closing_spread",
            "opening_ou", "opening_spread",
            "home_abbr", "away_abbr",
            "venue", "surface", "roof_type",
            "week", "game_id", "game_type",
            # Moneyline and odds columns (for handicapper info + PnL)
            "closing_home_ml", "closing_away_ml",
            "closing_spread_home_odds", "closing_spread_away_odds",
            "closing_over_odds", "closing_under_odds",
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
            for col in missing:
                df[col] = float("nan")

        return df[feature_names].copy()

    def load_inference_data(
        self,
        game_ids: List[int],
        feature_names: Optional[List[str]] = None,
    ) -> pd.DataFrame:
        """Load features for specific games (inference without labels).

        Parameters
        ----------
        game_ids :
            Primary keys of the games to load.
        feature_names :
            Feature columns to return (defaults to DB trainable features).
        """
        return self.load_data(
            game_ids=game_ids,
            include_upcoming=True,
            feature_names=feature_names,
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
    # closing_spread can be NaN for seasons without betting data;
    # must use np.where to avoid pandas NaN > 0 → False bug
    _has_line = df["closing_spread"].notna()
    df["home_ats_cover"] = np.where(
        _has_line & home_won.notna(),
        (df["home_score"] - df["away_score"] + df["closing_spread"]) > 0,
        float("nan")
    ).astype(float)
    df["away_ats_cover"] = np.where(
        _has_line & home_won.notna(),
        (df["away_score"] - df["home_score"] - df["closing_spread"]) > 0,
        float("nan")
    ).astype(float)
    df["over_result"] = np.where(
        df["closing_ou"].notna() & df["home_score"].notna() & df["away_score"].notna(),
        (df["home_score"] + df["away_score"]) > df["closing_ou"],
        float("nan")
    ).astype(float)
    df["home_score_margin"] = df["home_score"] - df["away_score"]

    # ── 2. Rest days ─────────────────────────────────────────────────────────
    df["game_date"] = pd.to_datetime(df["game_date"])
    df["home_last_game"] = pd.to_datetime(df["home_last_game"], errors="coerce")
    df["away_last_game"] = pd.to_datetime(df["away_last_game"], errors="coerce")
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
    df["sp_h_odds_mvmt"] = df["closing_spread_home_odds"] - df["opening_spread_home_odds"].fillna(0)
    df["sp_a_odds_mvmt"] = df["closing_spread_away_odds"] - df["opening_spread_away_odds"].fillna(0)

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

    # ── Compute team-overall rolling stats on the long frame ────────────
    for window in [5, 10]:
        tg[f"win_pct_r{window}"] = (
            tg.groupby("team_id")["won"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            .fillna(0.5)
        )
        tg[f"margin_r{window}"] = (
            tg.groupby("team_id")["margin"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            .fillna(0.0)
        )
        tg[f"cover_pct_r{window}"] = (
            tg.groupby("team_id")["cover"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            .fillna(0.5)
        )
        if window == 10:
            tg["pf"] = (
                tg.groupby("team_id")["score"]
                .transform(lambda s: s.shift(1).rolling(10, min_periods=1).mean())
            )
            tg["pa"] = (
                tg.groupby("team_id")["opp_score"]
                .transform(lambda s: s.shift(1).rolling(10, min_periods=1).mean())
            )

    # OU features
    if "ou_margin" in tg.columns:
        tg["cover_as_over"] = (tg["ou_margin"] > 0).astype(float)
        for window in [5, 10]:
            tg[f"ou_over_pct_r{window}"] = (
                tg.groupby("team_id")["cover_as_over"]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
                .fillna(0.5)
            )
            tg[f"ou_margin_r{window}"] = (
                tg.groupby("team_id")["ou_margin"]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
                .fillna(0.0)
            )
        tg["ou_as_over_pct_r10"] = tg["ou_over_pct_r10"]

    # Embarrassed (lost by 14+)
    tg["embarrassed"] = (tg["margin"] <= -14).astype(float)
    for window in [5, 10]:
        tg[f"embarrassed_pct_r{window}"] = (
            tg.groupby("team_id")["embarrassed"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            .fillna(0.0)
        )

    # Season-long ATS (expanding within each team+season)
    tg["season_ats"] = (
        tg.groupby(["team_id", "season_id"])["cover"]
        .transform(lambda s: s.shift(1).expanding().mean())
        .fillna(0.5)
    )
    # Season wins
    tg["season_wins"] = (
        tg.groupby(["team_id", "season_id"])["won"]
        .transform(lambda s: s.shift(1).expanding().sum())
        .fillna(0)
    )

    # Home/away ATS splits — position-specific (kept for situational data)
    homes = tg[tg.position == "home"].copy()
    homes["ats_cover_pct_r5"] = (
        homes.groupby("team_id")["cover"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
        .fillna(0.5)
    )
    aways = tg[tg.position == "away"].copy()
    aways["ats_cover_pct_r5"] = (
        aways.groupby("team_id")["cover"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
        .fillna(0.5)
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

    for stat_name, stat_col in [("win", "won"), ("ats", "cover")]:
        tg[f"{stat_name}_streak"] = (
            tg.groupby("team_id")[stat_col]
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

    tg["weighted_margin_r5"] = (
        tg.groupby("team_id")["margin"]
        .transform(
            lambda s: s.shift(1)
            .rolling(5, min_periods=1)
            .apply(_weighted_avg)
        )
    ).fillna(0.0)

    # ── Join team-overall stats back into wide DataFrame ──────────────────
    home_stats = tg[tg["position"] == "home"].set_index("game_id")
    away_stats = tg[tg["position"] == "away"].set_index("game_id")

    tg_cols = {
        "win_pct_r5", "win_pct_r10",
        "margin_r5", "margin_r10",
        "cover_pct_r5", "cover_pct_r10",
        "pf", "pa",
        "ou_over_pct_r5", "ou_over_pct_r10",
        "ou_margin_r5", "ou_margin_r10",
        "embarrassed_pct_r5", "embarrassed_pct_r10",
        "season_ats", "season_wins",
        "ou_as_over_pct_r10",
        "win_streak", "ats_streak",
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
    df["home_ats_home_pct_r5"] = df["game_id"].map(_homes_ats).fillna(0.5)
    df["away_ats_away_pct_r5"] = df["game_id"].map(_aways_ats).fillna(0.5)
    # ── 16. Division & primetime flags ───────────────────────────────────
    if "home_div" in df.columns and "away_div" in df.columns:
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
        })
        df = df.merge(
            home_off[[season_col, "week", "home_abbr",
                      "home_off_ypg", "home_ypp", "home_pass_ypg",
                      "home_rush_ypg", "home_pass_ypa", "home_rush_ypa",
                      "home_turnover_diff_r5"]],
            left_on=[season_col, "week", "home_abbr"],
            right_on=[season_col, "week", "home_abbr"],
            how="left",
        )

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
        })
        df = df.merge(
            away_off[[season_col, "week", "away_abbr",
                      "away_off_ypg", "away_ypp", "away_pass_ypg",
                      "away_rush_ypg", "away_pass_ypa", "away_rush_ypa",
                      "away_turnover_diff_r5"]],
            left_on=[season_col, "week", "away_abbr"],
            right_on=[season_col, "week", "away_abbr"],
            how="left",
        )

        # ── Home team defensive stats ──
        # Home team's defense = the home team's own def_ypg (yards allowed)
        home_def = _ts.rename(columns={
            "team_abbr": "home_abbr",
            "def_ypg": "home_def_ypg",
            "def_ypp": "home_def_ypp",
            "def_pass_ypg": "home_def_pass_ypg",
            "def_rush_ypg": "home_def_rush_ypg",
        })
        df = df.merge(
            home_def[[season_col, "week", "home_abbr",
                      "home_def_ypg", "home_def_ypp",
                      "home_def_pass_ypg", "home_def_rush_ypg"]],
            left_on=[season_col, "week", "home_abbr"],
            right_on=[season_col, "week", "home_abbr"],
            how="left",
        )

        # ── Away team defensive stats ──
        away_def = _ts.rename(columns={
            "team_abbr": "away_abbr",
            "def_ypg": "away_def_ypg",
            "def_ypp": "away_def_ypp",
            "def_pass_ypg": "away_def_pass_ypg",
            "def_rush_ypg": "away_def_rush_ypg",
        })
        df = df.merge(
            away_def[[season_col, "week", "away_abbr",
                      "away_def_ypg", "away_def_ypp",
                      "away_def_pass_ypg", "away_def_rush_ypg"]],
            left_on=[season_col, "week", "away_abbr"],
            right_on=[season_col, "week", "away_abbr"],
            how="left",
        )

        # ── Fill NAs for games without game_stats data ──
        stat_suffixes = [
            "off_ypg", "ypp", "pass_ypg", "rush_ypg",
            "pass_ypa", "rush_ypa", "turnover_diff_r5",
            "def_ypg", "def_ypp",
            "def_pass_ypg", "def_rush_ypg",
        ]
        for prefix in ["home", "away"]:
            for suffix in stat_suffixes:
                col = f"{prefix}_{suffix}"
                if col in df.columns:
                    df[col] = df[col].fillna(0.0)

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
            ]:
                df[f"{prefix}_{suffix}"] = 0.0

        # Drop temporary merge columns
        for c in ["season", "week"]:
            if c in df.columns and "_x" not in str(c):
                # season/week might exist from GAME_QUERY — don't drop
                pass

    logger.info(
        "build_features complete: %d rows, %d features",
        len(df), len(df.columns),
    )

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
        logger.info("Features listed in catalog: %d", len(FEATURES_CATALOG))
        logger.info("Computed features: %d", len(COMPUTED_FEATURES_CATALOG))
