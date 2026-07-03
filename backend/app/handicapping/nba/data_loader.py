"""
NBA Data Loader — loads and prepares NBA game data for model training and inference.

Mirror of the NFL data_loader.py with NBA-specific schemas, team locations,
features from nba.features, and NBA-relevant computed features.

Key differences from NFL:
  - Schema: nba.* (not nfl.*)
  - Games have period-based quarter scoring (nba.games)
  - No dome/outdoor distinction (all indoor arenas)
  - No weather data (all indoor)
  - Different betting line columns (spread, over_under)
  - Time zone / travel logic uses NBA team cities
  - Opponent-adjusted scoring uses nba_xgb_model_ats.py's feature set
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from math import asin, cos, radians, sin, sqrt

logger = logging.getLogger(__name__)

# ── Database connection ────────────────────────────────────────────────────────
DEFAULT_DB_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql://earl:earl2025@localhost:5432/earl_knows_football",
)


# ═══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════════


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = (
        sin(dlat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    )
    return float(R * 2 * asin(sqrt(a)))


def rolling_mean_safe(
    s: pd.Series, window: int, min_periods: int = 1
) -> pd.Series:
    """Rolling mean with fallback — ensures float return."""
    return s.rolling(window, min_periods=min_periods).mean()


# ═══════════════════════════════════════════════════════════════════════════════
#  TEAM_LOCATIONS — lat/lng for NBA arenas
# ═══════════════════════════════════════════════════════════════════════════════

TEAM_LOCATIONS: Dict[str, Tuple[float, float]] = {
    "ATL": (33.7575, -84.3963),    # State Farm Arena — Atlanta
    "BOS": (42.3663, -71.0624),    # TD Garden — Boston
    "BKN": (40.6829, -73.9754),    # Barclays Center — Brooklyn
    "CHA": (35.2252, -80.8398),    # Spectrum Center — Charlotte
    "CHI": (41.8809, -87.6742),    # United Center — Chicago
    "CLE": (41.4963, -81.6882),    # Rocket Mortgage FieldHouse — Cleveland
    "DAL": (32.7905, -96.8103),    # American Airlines Center — Dallas
    "DEN": (39.7482, -105.0076),   # Ball Arena — Denver
    "DET": (42.3410, -83.0548),    # Little Caesars Arena — Detroit
    "GSW": (37.7479, -122.3873),   # Chase Center — Golden State
    "HOU": (29.7508, -95.3622),    # Toyota Center — Houston
    "IND": (39.7640, -86.1558),    # Gainbridge Fieldhouse — Indiana
    "LAC": (34.0430, -118.2673),   # Crypto.com Arena — LA Clippers
    "LAL": (34.0430, -118.2673),   # Crypto.com Arena — LA Lakers
    "MEM": (35.1382, -90.0506),    # FedExForum — Memphis
    "MIA": (25.7814, -80.1871),    # Kaseya Center — Miami
    "MIL": (43.0452, -87.9172),    # Fiserv Forum — Milwaukee
    "MIN": (44.9795, -93.2757),    # Target Center — Minnesota
    "NOP": (29.9491, -90.0822),    # Smoothie King Center — New Orleans
    "NYK": (40.7505, -73.9934),    # Madison Square Garden — New York
    "OKC": (35.4634, -97.5151),    # Paycom Center — Oklahoma City
    "ORL": (28.5392, -81.4687),    # Kia Center — Orlando
    "PHI": (39.9013, -75.1719),    # Wells Fargo Center — Philadelphia
    "PHX": (33.4457, -112.0710),   # Footprint Center — Phoenix
    "POR": (45.5316, -122.6668),   # Moda Center — Portland
    "SAC": (38.5803, -121.4996),   # Golden 1 Center — Sacramento
    "SAS": (29.4271, -98.4376),    # Frost Bank Center — San Antonio
    "TOR": (43.6435, -79.3791),    # Scotiabank Arena — Toronto
    "UTA": (40.7683, -111.9011),   # Delta Center — Utah
    "WAS": (38.8982, -77.0211),    # Capital One Arena — Washington
}


# ═══════════════════════════════════════════════════════════════════════════════
#  GAME_QUERY — loads raw per-game NBA data from the database
# ═══════════════════════════════════════════════════════════════════════════════

GAME_QUERY = """
WITH betting_agg AS (
    SELECT
        blc.game_id,
        blc.opening_spread,
        blc.opening_ou,
        blc.closing_spread,
        blc.closing_ou,
        blc.closing_home_ml                   AS home_moneyline,
        blc.closing_away_ml                   AS away_moneyline,
        blc.closing_spread_home_odds          AS spread_home_odds,
        blc.closing_spread_away_odds          AS spread_away_odds,
        blc.closing_over_odds                 AS over_odds,
        blc.closing_under_odds                AS under_odds,
        blc.closing_home_implied_probability  AS home_implied_probability,
        blc.closing_away_implied_probability  AS away_implied_probability
    FROM nba.betting_lines_consolidated blc
),
team_games AS (
    SELECT
        g.id                                                                    AS game_id,
        g.nba_game_id,
        g.season_id,
        s.year                                                                  AS season_year,
        g.date,
        g.home_team_id,
        g.away_team_id,
        g.home_score,
        g.away_score,
        g.status,
        g.game_type,
        g.attendance,
        ht.abbreviation                                                         AS home_abbr,
        ht.name                                                                 AS home_team_name,
        CONCAT(ht.name, ' ', ht.abbreviation)                                   AS home_team,
        at.abbreviation                                                         AS away_abbr,
        at.name                                                                 AS away_team_name,
        CONCAT(at.name, ' ', at.abbreviation)                                   AS away_team,
        ba.opening_spread,
        ba.opening_ou,
        ba.closing_spread,
        ba.closing_ou,
        ba.home_moneyline,
        ba.away_moneyline,
        ba.spread_home_odds,
        ba.spread_away_odds,
        ba.over_odds,
        ba.under_odds,
        ba.home_implied_probability,
        ba.away_implied_probability
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    INNER JOIN betting_agg ba ON ba.game_id = g.id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND g.away_score IS NOT NULL
      AND g.home_score > 0
      AND g.away_score > 0
)
SELECT * FROM team_games
ORDER BY season_id, date ASC
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Feature catalogs — synchronised with nba.features DB table
# ═══════════════════════════════════════════════════════════════════════════════

FEATURES_CATALOG: Dict[str, str] = {
    "spread": "Closing point spread (negative = home favorite)",
    "closing_ou": "Closing over/under total",
    "home_moneyline": "Home team moneyline odds",
    "away_moneyline": "Away team moneyline odds",
    "home_score": "Home team final score",
    "away_score": "Away team final score",
    "season_year": "Calendar year of the season (via nba.seasons join)",
    "season_id": "Season identifier",
    "game_id": "Unique game identifier",
    "date": "Game date",
    "home_team_id": "Home team ID",
    "away_team_id": "Away team ID",
}

COMPUTED_FEATURES_CATALOG: Dict[str, str] = {
    "h_adj_off_10": "Home opponent-adjusted offense, rolling 10",
    "h_adj_def_10": "Home opponent-adjusted defense, rolling 10",
    "a_adj_off_10": "Away opponent-adjusted offense, rolling 10",
    "a_adj_def_10": "Away opponent-adjusted defense, rolling 10",
    "h_adj_off_20": "Home opponent-adjusted offense, rolling 20",
    "h_adj_def_20": "Home opponent-adjusted defense, rolling 20",
    "a_adj_off_20": "Away opponent-adjusted offense, rolling 20",
    "a_adj_def_20": "Away opponent-adjusted defense, rolling 20",
    "rest_h": "Home team rest days since last game",
    "rest_a": "Away team rest days since last game",
    "rest_diff": "Rest days advantage (home - away)",
    "home_b2b": "Binary: 1 if home team on back-to-back",
    "away_b2b": "Binary: 1 if away team on back-to-back",
    "travel_miles": "Away team travel distance in miles (haversine)",
    "h_implied": "Home team implied win probability from moneyline",
    "a_implied": "Away team implied win probability from moneyline",
    "spread_movement": "Spread movement: opening - closing",
    "implied_margin": "Expected point margin from moneyline implied probability",
    "ml_spread_mismatch": "Disagreement between ML-implied margin and closing spread",
    "h_ats_wins_5": "Home team ATS wins in last 5 games",
    "a_ats_wins_5": "Away team ATS wins in last 5 games",
    "h_ats_margin_5": "Home team avg ATS cover margin last 5 games",
    "a_ats_margin_5": "Away team avg ATS cover margin last 5 games",
    "h_wins_5": "Home team straight-up wins in last 5 games",
    "h_wins_10": "Home team straight-up wins in last 10 games",
    "a_wins_5": "Away team straight-up wins in last 5 games",
    "a_wins_10": "Away team straight-up wins in last 10 games",
    "home_ats_cover": "Home team covered the spread (1=yes, 0=no)",
    "away_ats_cover": "Away team covered the spread (1=yes, 0=no)",
    "over_result": "Game went over the total (1=yes, 0=no)",
}

DISPLAY_NAMES: Dict[str, str] = {
    "spread": "Spread",
    "closing_ou": "Closing OU",
    "home_moneyline": "Home ML",
    "away_moneyline": "Away ML",
    "home_score": "Home Score",
    "away_score": "Away Score",
    "season_year": "Season",
    "season_id": "Season",
    "game_id": "Game ID",
    "date": "Date",
    "home_team_id": "Home Team ID",
    "away_team_id": "Away Team ID",
    "h_adj_off_10": "Home Adj Off L10",
    "h_adj_def_10": "Home Adj Def L10",
    "a_adj_off_10": "Away Adj Off L10",
    "a_adj_def_10": "Away Adj Def L10",
    "h_adj_off_20": "Home Adj Off L20",
    "h_adj_def_20": "Home Adj Def L20",
    "a_adj_off_20": "Away Adj Off L20",
    "a_adj_def_20": "Away Adj Def L20",
    "rest_h": "Home Rest",
    "rest_a": "Away Rest",
    "rest_diff": "Rest Diff",
    "home_b2b": "Home B2B",
    "away_b2b": "Away B2B",
    "travel_miles": "Travel Miles",
    "h_implied": "Home Implied",
    "a_implied": "Away Implied",
    "spread_movement": "Spread Movement",
    "implied_margin": "Implied Margin",
    "ml_spread_mismatch": "ML-Spread Mismatch",
    "h_ats_wins_5": "Home ATS Wins L5",
    "a_ats_wins_5": "Away ATS Wins L5",
    "h_ats_margin_5": "Home ATS Margin L5",
    "a_ats_margin_5": "Away ATS Margin L5",
    "h_wins_5": "Home Wins L5",
    "h_wins_10": "Home Wins L10",
    "a_wins_5": "Away Wins L5",
    "a_wins_10": "Away Wins L10",
    "home_ats_cover": "Home team covered the spread (1=yes, 0=no)",
    "away_ats_cover": "Away team covered the spread (1=yes, 0=no)",
    "over_result": "Game went over the total (1=yes, 0=no)",
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Location cache for haversine lookups
# ═══════════════════════════════════════════════════════════════════════════════

_location_cache = {
    abbr: (loc[0], loc[1]) for abbr, loc in TEAM_LOCATIONS.items()
}


# ═══════════════════════════════════════════════════════════════════════════════
#  NBADataLoader
# ═══════════════════════════════════════════════════════════════════════════════


class NBADataLoader:
    """Loads and prepares NBA game data for model training and inference.

    Mirrors the NFLDataLoader pattern.

    Parameters
    ----------
    db_url :
        PostgreSQL connection string.  If ``None`` uses ``DEFAULT_DB_URL``.
    ats_only :
        If True, only load / compute features needed for the ATS model.
    ou_only :
        If True, only load / compute features needed for the OU model.
    """

    def __init__(
        self,
        db_url: Optional[str] = None,
        ats_only: bool = False,
        ou_only: bool = False,
    ) -> None:
        self.db_url: str = db_url or DEFAULT_DB_URL
        self.ats_only: bool = ats_only
        self.ou_only: bool = ou_only
        self._engine: Any = None
        self._catalog = {**FEATURES_CATALOG, **COMPUTED_FEATURES_CATALOG}
        self._feature_cache: Optional[pd.DataFrame] = None
        logger.info(
            "NBADataLoader initialized (ats_only=%s, ou_only=%s)",
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
            f"NBADataLoader(db_url={self.db_url!r}, "
            f"ats_only={self.ats_only}, ou_only={self.ou_only})"
        )

    # ── Feature catalog helpers ──────────────────────────────────────────────

    def get_features_catalog(self) -> Dict[str, str]:
        """Return the full feature catalog (base + computed)."""
        return dict(self._catalog)

    def get_feature_names(self) -> List[str]:
        """Return sorted list of all known feature names."""
        return sorted(self._catalog.keys())

    def get_feature_description(self, name: str) -> str:
        """Return the description for a feature (or empty string)."""
        return self._catalog.get(name, "")

    def get_display_name(self, name: str) -> str:
        """Return the human-readable display name for a feature."""
        return DISPLAY_NAMES.get(name, name)

    def get_feature_columns(self, target: Optional[str] = None) -> List[str]:
        """Return trainable feature column names.

        Parameters
        ----------
        target :
            If ``'ats'``, only return features in ``COMPUTED_FEATURES_CATALOG``
            that correspond to ATS features.  If ``None``, return all
            trainable features (all computed).

        Returns
        -------
        Sorted list of feature column names.
        """
        if target in ("ats", "ou"):
            flag = "current_ats" if target == "ats" else "current_ou"
            try:
                with psycopg2.connect(os.environ.get("DATABASE_URL", "postgresql://earl:earl2025@localhost:5432/earl_knows_football")) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SELECT name FROM nba.features WHERE {flag} = TRUE "
                            "AND is_trainable = TRUE ORDER BY id"
                        )
                        rows = cur.fetchall()
                        db_features = [r[0] for r in rows]
                        known = set(FEATURES_CATALOG.keys()) | set(COMPUTED_FEATURES_CATALOG.keys())
                        return sorted(c for c in db_features if c in known)
            except Exception:
                pass
            # Fallback: return home/away computed features
            return sorted(
                k for k in COMPUTED_FEATURES_CATALOG
                if k.startswith(("h_", "a_"))
            )
        known = set(FEATURES_CATALOG.keys()) | set(COMPUTED_FEATURES_CATALOG.keys())
        return sorted(known)

    def get_all_with_display(self) -> List[Dict[str, str]]:
        """Return a list of dicts with 'name', 'description', 'display_name'."""
        return [
            {
                "name": name,
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name),
            }
            for name, desc in self._catalog.items()
        ]

    # ── Query helpers ────────────────────────────────────────────────────────

    def _build_query(self, base_query: str, **kwargs: Any) -> str:
        """Build a query string from the base query and optional overrides."""
        if not kwargs:
            return base_query
        return base_query.format(**kwargs)

    def _query(self, sql: str) -> pd.DataFrame:
        """Execute raw SQL via the engine and return a DataFrame."""
        t0 = time.time()
        df = pd.read_sql(sql, self.engine)
        elapsed = time.time() - t0
        logger.info("Query returned %d rows in %.2fs", len(df), elapsed)
        return df

    # ── Load methods ─────────────────────────────────────────────────────────

    def load_games(
        self,
        seasons: Optional[List[int]] = None,
        status: Optional[str] = "FINAL",
        limit: Optional[int] = None,
        include_upcoming: bool = False,
        game_ids: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        """Load NBA games from the database.

        Parameters
        ----------
        seasons :
            List of season IDs to load.  If ``None`` loads all.
        status :
            Game status filter (e.g. ``'FINAL'``).
        limit :
            Maximum number of games to return.
        include_upcoming :
            If True, include games that haven't been played yet.
        game_ids :
            Specific game IDs to load.

        Returns
        -------
        DataFrame with raw game data.
        """
        query = GAME_QUERY

        where_parts: List[str] = []
        if status:
            where_parts.append(f"status = '{status}'")
        if seasons:
            season_list = ", ".join(str(s) for s in seasons)
            where_parts.append(f"season_id IN ({season_list})")
        if game_ids:
            id_list = ", ".join(str(g) for g in game_ids)
            where_parts.append(f"game_id IN ({id_list})")

        if where_parts:
            where_clause = " AND ".join(where_parts)
            query = query.replace(
                "SELECT * FROM team_games",
                f"SELECT * FROM team_games WHERE {where_clause}",
            ).replace("ORDER BY season_id, date ASC", "")
            query += " ORDER BY season_id, date ASC"

        if limit:
            query += f" LIMIT {limit}"

        return self._query(query)

    def load_all_games(self) -> pd.DataFrame:
        """Load *all* games (convenience wrapper)."""
        return self.load_games()

    def load_data(
        self,
        seasons: Optional[List[int]] = None,
    ) -> pd.DataFrame:
        """Load game data and apply full feature engineering.

        Main entry point for training pipelines.
        """
        df = self.load_games(seasons=seasons)
        if df.empty:
            logger.warning("No NBA games found for seasons=%s", seasons)
            return df

        df = self._build_features(df)
        return df

    def load_inference_data(
        self,
        game_ids: Optional[List[int]] = None,
        limit: Optional[int] = None,
    ) -> pd.DataFrame:
        """Load data for inference on specific (or recent) games."""
        df = self.load_games(
            seasons=None,
            status=None,
            limit=limit,
            include_upcoming=True,
            game_ids=game_ids,
        )
        if df.empty:
            return df

        df = self._build_features(df)
        return df

    # ── Feature column management ────────────────────────────────────────────

    def extract_features_from_training_run(
        self,
        results_json: Any,
        min_importance: float = 0.0,
    ) -> List[str]:
        """Extract feature names from a training run's results_json."""
        if results_json is None:
            return []

        imp_list: List[Dict[str, Any]] = []

        if isinstance(results_json, dict) and "results" in results_json:
            for res in reversed(results_json["results"]):
                fi = res.get("feature_importance", [])
                if fi:
                    imp_list = fi
                    break
        elif isinstance(results_json, dict) and "feature_importance" in results_json:
            imp_list = results_json["feature_importance"]
        elif isinstance(results_json, list):
            if results_json and isinstance(results_json[0], dict):
                if "feature" in results_json[0]:
                    imp_list = results_json
                elif "feature_importance" in results_json[0]:
                    imp_list = results_json[-1].get("feature_importance", [])

        if not imp_list:
            logger.info("No feature_importance found in results_json")
            return []

        raw: List[Tuple[float, str]] = []
        for item in imp_list:
            if isinstance(item, dict) and "feature" in item:
                imp = float(item.get("importance", 0.0) or 0.0)
                if imp >= min_importance:
                    raw.append((imp, item["feature"]))

        raw.sort(key=lambda x: -x[0])

        seen: set[str] = set()
        result: List[str] = []
        for _imp_val, feat in raw:
            if feat not in seen:
                seen.add(feat)
                result.append(feat)

        logger.info(
            "Extracted %d features (min_importance=%.4f)", len(result), min_importance
        )
        return result

    # ── Internal ─────────────────────────────────────────────────────────────

    def _build_features(self, df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
        """Apply module-level feature engineering and order columns."""
        df = build_features(df, **kwargs)

        known = set(list(FEATURES_CATALOG.keys()) + list(COMPUTED_FEATURES_CATALOG.keys()))
        keep = [c for c in df.columns if c in known]
        return df[keep].copy()


# ── Module-level: feature engineering ─────────────────────────────────────────


def build_features(df: pd.DataFrame, **kwargs: Any) -> pd.DataFrame:
    """NBA feature engineering — computes all features from ``nba.features``.

    Mirrors the NFL ``build_features()`` pattern.  Computes:

    *   Opponent-adjusted scoring (10- and 20-game windows)
    *   Rest days and back-to-back flags
    *   Travel miles (haversine)
    *   Betting market features (implied probability, spread movement, mismatch)
    *   Form & streaks (ATS, straight-up wins, cover margins)
    *   Split-into-home/away halves

    Parameters
    ----------
    df :
        Raw game data from ``load_games()``.
    **kwargs :
        Unused; accepted for API compatibility.

    Returns
    -------
    DataFrame with all computed features.
    """
    df = df.copy()

    # Normalise column names
    df.columns = [c.lower() for c in df.columns]

    # Ensure date is datetime
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])

    # ═══════════════════════════════════════════════════════════════════════════
    #  Split into home / away halves for team-level rolling computations
    # ═══════════════════════════════════════════════════════════════════════════

    # Alias spread column for readability in the feature code
    df["spread"] = df["closing_spread"]

    home_cols = {
        "game_id": "game_id",
        "season_id": "season_id",
        "home_team_id": "team_id",
        "home_abbr": "team_abbr",
        "home_team": "team",
        "away_team_id": "opp_id",
        "away_abbr": "opp_abbr",
        "home_score": "score_for",
        "away_score": "score_against",
        "spread": "spread",
        "home_moneyline": "moneyline",
    }
    away_cols = {
        "game_id": "game_id",
        "season_id": "season_id",
        "away_team_id": "team_id",
        "away_abbr": "team_abbr",
        "away_team": "team",
        "home_team_id": "opp_id",
        "home_abbr": "opp_abbr",
        "away_score": "score_for",
        "home_score": "score_against",
        "spread": "spread",
        "away_moneyline": "moneyline",
    }

    home_half = df[list(home_cols.keys())].rename(columns=home_cols).copy()

    # Build away half — invert spread (spread is from home perspective)
    away_half_raw = df[list(away_cols.keys())].copy()
    away_half_raw["spread"] = -away_half_raw["spread"]
    away_half = away_half_raw.rename(columns=away_cols)

    # Mark is_home
    home_half["is_home"] = 1
    away_half["is_home"] = 0

    # Date for sorting
    home_half["date"] = df["date"].values
    away_half["date"] = df["date"].values

    # Combine and sort
    team_games = pd.concat([home_half, away_half], ignore_index=True)
    team_games.sort_values(["team_id", "date", "game_id"], inplace=True)
    team_games.reset_index(drop=True, inplace=True)

    # Keep a date-ordered per-team index for rolling computations
    team_games.sort_values(["team_id", "date"], inplace=True)

    # ═══════════════════════════════════════════════════════════════════════════
    #  1. Opponent-adjusted scoring
    # ═══════════════════════════════════════════════════════════════════════════

    season_avg = team_games.groupby("season_id")["score_for"].transform("mean")

    for window in (10, 20):
        opp_col = f"opp_avg_{window}"
        team_games[opp_col] = (
            team_games.groupby("opp_abbr")["score_for"]
            .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
        )

        adj_off_h = f"h_adj_off_{window}"
        adj_def_h = f"h_adj_def_{window}"
        adj_off_a = f"a_adj_off_{window}"
        adj_def_a = f"a_adj_def_{window}"

        opp_avg = f"opp_avg_{window}"
        team_games[adj_off_h] = np.where(
            team_games["is_home"] == 1,
            team_games["score_for"] - team_games[opp_avg],
            np.nan,
        )
        team_games[adj_def_h] = np.where(
            team_games["is_home"] == 1,
            season_avg - (team_games["score_against"] - team_games[opp_avg]),
            np.nan,
        )
        team_games[adj_off_a] = np.where(
            team_games["is_home"] == 0,
            team_games["score_for"] - team_games[opp_avg],
            np.nan,
        )
        team_games[adj_def_a] = np.where(
            team_games["is_home"] == 0,
            season_avg - (team_games["score_against"] - team_games[opp_avg]),
            np.nan,
        )

        for col in [adj_off_h, adj_def_h, adj_off_a, adj_def_a]:
            team_games[col] = (
                team_games.groupby("team_abbr")[col]
                .transform(lambda s: s.shift(1).rolling(window, min_periods=1).mean())
            )

    # ═══════════════════════════════════════════════════════════════════════════
    #  2. Rest days & back-to-back
    # ═══════════════════════════════════════════════════════════════════════════

    team_games["prev_date"] = team_games.groupby("team_abbr")["date"].shift(1)
    team_games["rest_days"] = (
        team_games["date"] - team_games["prev_date"]
    ).dt.days
    team_games["b2b"] = (team_games["rest_days"] == 1).astype(int)

    df["rest_h"] = team_games.loc[team_games["is_home"] == 1, "rest_days"].values
    df["rest_a"] = team_games.loc[team_games["is_home"] == 0, "rest_days"].values
    df["home_b2b"] = team_games.loc[team_games["is_home"] == 1, "b2b"].values
    df["away_b2b"] = team_games.loc[team_games["is_home"] == 0, "b2b"].values

    df["rest_diff"] = df["rest_h"] - df["rest_a"]
    df["rest_diff"] = df["rest_diff"].fillna(0)

    # ═══════════════════════════════════════════════════════════════════════════
    #  3. Travel miles (haversine)
    # ═══════════════════════════════════════════════════════════════════════════

    team_games["lat"] = team_games["team_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[0]
    )
    team_games["lon"] = team_games["team_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[1]
    )
    team_games["home_lat"] = team_games["opp_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[0]
    )
    team_games["home_lon"] = team_games["opp_abbr"].map(
        lambda abbr: _location_cache.get(abbr, (0, 0))[1]
    )

    team_games["prev_home_lat"] = team_games.groupby("team_abbr")["home_lat"].shift(1)
    team_games["prev_home_lon"] = team_games.groupby("team_abbr")["home_lon"].shift(1)

    team_games["team_travel"] = team_games.apply(
        lambda r: haversine_miles(
            r["prev_home_lat"], r["prev_home_lon"],
            r["home_lat"], r["home_lon"],
        )
        if pd.notna(r["prev_home_lat"])
        else 0.0,
        axis=1,
    )

    team_games["away_travel"] = np.where(
        team_games["is_home"] == 0, team_games["team_travel"], 0.0
    )

    away_games = team_games[team_games["is_home"] == 0][["game_id", "away_travel"]]
    df = df.merge(away_games, on="game_id", how="left")
    df["travel_miles"] = df["away_travel"].fillna(0.0)
    df.drop(columns=["away_travel"], inplace=True, errors="ignore")

    # ── Surface opponent-adjusted efficiency to df ────────────────────────
    for window in (10, 20):
        home_adj = team_games.loc[
            team_games["is_home"] == 1,
            ["game_id"] + [f"h_{s}_{window}" for s in ("adj_off", "adj_def")],
        ].copy()
        away_adj = team_games.loc[
            team_games["is_home"] == 0,
            ["game_id"] + [f"a_{s}_{window}" for s in ("adj_off", "adj_def")],
        ].copy()
        df = df.merge(home_adj, on="game_id", how="left")
        df = df.merge(away_adj, on="game_id", how="left")

    # ═══════════════════════════════════════════════════════════════════════════
    #  4. Betting market features
    # ═══════════════════════════════════════════════════════════════════════════

    df["spread_movement"] = df["opening_spread"] - df["closing_spread"]

    def _implied_prob(moneyline: pd.Series) -> pd.Series:
        """Convert American moneyline odds to implied probability."""
        moneyline = moneyline.astype(float)
        result = pd.Series(np.nan, index=moneyline.index)
        pos_mask = moneyline > 0
        neg_mask = moneyline < 0
        result.loc[pos_mask] = 100.0 / (moneyline.loc[pos_mask] + 100.0)
        result.loc[neg_mask] = -moneyline.loc[neg_mask] / (
            -moneyline.loc[neg_mask] + 100.0
        )
        return result

    df["h_implied"] = _implied_prob(df["home_moneyline"])
    df["a_implied"] = _implied_prob(df["away_moneyline"])

    df["implied_margin"] = (
        (df["h_implied"] - df["a_implied"]).abs() * 50.0
    ) * np.sign(df["h_implied"] - df["a_implied"])

    df["ml_spread_mismatch"] = df["implied_margin"] - df["closing_spread"].abs()

    # ═══════════════════════════════════════════════════════════════════════════
    #  5. Form & streaks (ATS, win counts, cover margins)
    # ═══════════════════════════════════════════════════════════════════════════

    df["home_actual_margin"] = df["home_score"] - df["away_score"]
    df["home_ats_cover"] = (
        df["home_actual_margin"] > -df["closing_spread"]
    ).astype(int)
    df["home_ats_margin"] = df["home_actual_margin"] - (-df["closing_spread"])

    df["away_ats_cover"] = (
        -df["home_actual_margin"] > df["closing_spread"]
    ).astype(int)
    df["away_ats_margin"] = -df["home_actual_margin"] - df["closing_spread"]

    df.sort_values(["game_id"], inplace=True)

    for team_prefix, abbr_col, cover_col, margin_col in [
        ("h_", "home_abbr", "home_ats_cover", "home_ats_margin"),
        ("a_", "away_abbr", "away_ats_cover", "away_ats_margin"),
    ]:
        df[f"{team_prefix}ats_wins_5"] = (
            df.groupby(abbr_col)[cover_col]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).sum())
        )
        df[f"{team_prefix}ats_margin_5"] = (
            df.groupby(abbr_col)[margin_col]
            .transform(lambda s: s.shift(1).rolling(5, min_periods=0).mean())
        )

    # Straight-up wins (home = positive margin, away = negative margin)
    df["h_wins_5"] = (
        df.groupby("home_abbr")["home_actual_margin"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=0).apply(lambda x: (x > 0).sum(), raw=True))
    )
    df["h_wins_10"] = (
        df.groupby("home_abbr")["home_actual_margin"]
        .transform(lambda s: s.shift(1).rolling(10, min_periods=0).apply(lambda x: (x > 0).sum(), raw=True))
    )
    df["a_wins_5"] = (
        df.groupby("away_abbr")["home_actual_margin"]
        .transform(lambda s: s.shift(1).rolling(5, min_periods=0).apply(lambda x: (x < 0).sum(), raw=True))
    )
    df["a_wins_10"] = (
        df.groupby("away_abbr")["home_actual_margin"]
        .transform(lambda s: s.shift(1).rolling(10, min_periods=0).apply(lambda x: (x < 0).sum(), raw=True))
    )

    # ── Over/under result ────────────────────────────────────────────────
    df["over_result"] = (
        (df["home_score"] + df["away_score"]) > df["closing_ou"]
    ).astype(float)

    # ═══════════════════════════════════════════════════════════════════════════
    #  6. Fill NaN / clean up
    # ═══════════════════════════════════════════════════════════════════════════

    for col in df.columns:
        if col in ("spread", "closing_spread", "closing_ou"):
            continue
        if df[col].dtype in (np.float64, np.int64) and df[col].isna().any():
            df[col] = df[col].fillna(0)

    drop_cols = [
        c for c in df.columns
        if c.startswith("home_actual_") or c.startswith("away_ats_")
    ]
    df.drop(columns=drop_cols, inplace=True, errors="ignore")

    return df


# ── Module-level helpers ──────────────────────────────────────────────────────


def get_model_features(target: Optional[str] = None) -> List[str]:
    """Return the list of trainable feature names for NBA models.

    Parameters
    ----------
    target :
        If ``'ats'``, only return features for the ATS model.

    Returns
    -------
    Sorted list of trainable feature names.
    """
    return NBADataLoader().get_feature_columns(target=target)


# ── Singleton / factory ───────────────────────────────────────────────────────

_loader_instance: Optional[NBADataLoader] = None


def get_data_loader(db_url: Optional[str] = None,
                    ats_only: bool = False,
                    ou_only: bool = False) -> NBADataLoader:
    """Return a singleton NBADataLoader instance."""
    global _loader_instance
    if _loader_instance is None:
        _loader_instance = NBADataLoader(db_url=db_url, ats_only=ats_only, ou_only=ou_only)
    return _loader_instance


# ═══════════════════════════════════════════════════════════════════════════════
#  CLI smoke test
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dl = get_data_loader()
    print(f"Feature catalog: {len(dl.get_features_catalog())} entries")
    print(f"ATS features: {dl.get_feature_columns(target='ats')}")
    print(f"All trainable: {dl.get_feature_columns()}")

    df = dl.load_games(limit=10)
    print(f"Games loaded: {len(df)} rows, {len(df.columns)} cols")
    if not df.empty:
        print(f"Columns: {list(df.columns)}")
        print(f"Date range: {df['date'].iloc[0]} to {df['date'].iloc[-1]}")

    df_feats = dl.load_data(limit=200)
    print(f"Featurized: {len(df_feats)} rows, {len(df_feats.columns)} cols")
    if not df_feats.empty:
        print(f"Feature columns: {list(df_feats.columns)}")
        nulls = df_feats.isnull().sum()
        if nulls.any():
            print(f"Nulls:\n{nulls[nulls > 0]}")
        else:
            print("No null values ✅")
