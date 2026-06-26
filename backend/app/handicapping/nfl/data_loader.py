"""
NFL data loader — single source of truth for building training/inference
DataFrames for the NFL ATS and OU XGBoost models. Mirrors the MLB data_loader
pattern with NFL-specific queries, teams, and feature catalogs.

The feature catalogs (FEATURES_CATALOG and COMPUTED_FEATURES_CATALOG) serve as
the single source of truth.  They are registered into the nfl.features table
via build_feature_catalog() / ensure_features_in_db().  The ATS_FEATURES and
OU_FEATURES lists used by the model files are derived from these catalogs.

USAGE:
    from handicapping.nfl.data_loader import (
        get_data_loader,
        build_features,
        ATS_FEATURES,
        OU_FEATURES,
    )
"""
import logging
import os
import subprocess
import warnings
from datetime import date, datetime

import numpy as np
import pandas as pd
from math import radians, sin, cos, sqrt, asin
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import create_async_engine

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("earl.nfl.data_loader")
log = logger.info


# ── DB URL ──────────────────────────────────────────────────────────────

DEFAULT_DB_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

# Docker container name for psql fallback
DB_CONTAINER = "earl-knows-football-db-1"


# ── TEAM META — stadium / dome / coordinates / TZ ───────────────────────

DOMES = {"ARI", "ATL", "DAL", "DET", "HOU", "IND", "LAC", "LAR", "LV", "MIN", "NO", "TB"}

TZ = {
    "ARI": -7, "ATL": -5, "BAL": -5, "BUF": -5, "CAR": -5, "CHI": -6,
    "CIN": -5, "CLE": -5, "DAL": -6, "DEN": -7, "DET": -5, "GB": -6,
    "HOU": -6, "IND": -5, "JAX": -5, "KC": -6, "LAC": -8, "LAR": -8,
    "LV": -8, "MIA": -5, "MIN": -6, "NE": -5, "NO": -6, "NYG": -5,
    "NYJ": -5, "PHI": -5, "PIT": -5, "SEA": -8, "SF": -8, "TB": -5,
    "TEN": -6, "WAS": -5,
}

COORDS = {
    "ARI": (33.5, -112.1), "ATL": (33.8, -84.4), "BAL": (39.3, -76.6),
    "BUF": (42.8, -78.9), "CAR": (35.2, -80.9), "CHI": (41.9, -87.6),
    "CIN": (39.1, -84.5), "CLE": (41.5, -81.7), "DAL": (32.8, -96.8),
    "DEN": (39.7, -105.0), "DET": (42.3, -83.0), "GB": (44.5, -88.0),
    "HOU": (29.7, -95.4), "IND": (39.8, -86.2), "JAX": (30.3, -81.7),
    "KC": (39.1, -94.5), "LAC": (32.8, -117.1), "LAR": (34.0, -118.3),
    "LV": (36.1, -115.2), "MIA": (25.8, -80.2), "MIN": (45.0, -93.3),
    "NE": (42.1, -71.3), "NO": (30.0, -90.1), "NYG": (40.8, -74.1),
    "NYJ": (40.8, -74.1), "PHI": (39.9, -75.2), "PIT": (40.4, -80.0),
    "SEA": (47.6, -122.3), "SF": (37.4, -121.9), "TB": (27.8, -82.7),
    "TEN": (36.2, -86.8), "WAS": (38.9, -77.0),
}


def _haversine(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles between two lat/lon pairs."""
    R = 3958.8
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return R * 2 * asin(sqrt(a))


# ════════════════════════════════════════════════════════════════════════
# FEATURE CATALOGS
# ════════════════════════════════════════════════════════════════════════

# Every feature used by the ATS or OU XGBoost models lives in one of these
# two catalogs.  The keys are feature column names; the values are short
# description strings.  At DB-registration time the ATS vs OU flags are set
# based on membership in ATS_CATALOG vs OU_CATALOG.

ATS_CATALOG = {
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
    "is_dome": "Home stadium dome/retractable roof",
}

OU_CATALOG = {
    "opening_ou": "Opening over/under line",
    "spread": "Closing / opening spread",
    "ou_movement": "Closing OU minus opening OU",
    "dpf": "Defensive points for (adjusted)",
    "dpa": "Defensive points against (adjusted)",
    "himp": "Home implied scoring",
    "aimp": "Away implied scoring",
    "dimp": "Differential implied (home - away)",
    "home_win_pct_r5": "Home team win % last 5 games",
    "away_win_pct_r5": "Away team win % last 5 games",
    "home_margin_r3": "Home team avg margin last 3 games",
    "away_margin_r3": "Away team avg margin last 3 games",
    "home_margin_r10": "Home team avg margin last 10 games",
    "away_margin_r10": "Away team avg margin last 10 games",
    "rest_diff": "Rest day differential (home - away)",
    "travel_miles": "Away team travel distance in miles",
    "tz_diff": "Timezone difference home - away (hours)",
    "is_short": "Short week indicator",
    "is_dome": "Home stadium dome/retractable roof",
    "temp": "Game-time temperature (F)",
    "wind": "Game-time wind speed (mph)",
}

# DISPLAY_NAMES — prettier aliases for the feature-name labels in the DB.
# If absent, the slug is auto-derived.
DISPLAY_NAMES = {
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
    "home_margin_r10": "Home Margin L10",
    "away_margin_r10": "Away Margin L10",
    "home_cover_pct_r5": "Home Cover% L5",
    "away_cover_pct_r5": "Away Cover% L5",
    "home_embarrassed": "Home Embarrassed",
    "away_embarrassed": "Away Embarrassed",
    "home_season_ats_pct": "Home Season ATS%",
    "away_season_ats_pct": "Away Season ATS%",
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
}

# ── Computed features catalog — same structure ─────────────────────────
COMPUTED_FEATURES_CATALOG = {
    "season_avg_pts": "League average points per team per game for the season",
}

# ── Combined ATS/OU feature column lists ────────────────────────────────
# These are the exact lists used by the model files.  Order matters for
# model input consistency.

ATS_FEATURES = sorted(ATS_CATALOG.keys())
OU_FEATURES = sorted(OU_CATALOG.keys())


# ════════════════════════════════════════════════════════════════════════
# DB FEATURE REGISTRATION
# ════════════════════════════════════════════════════════════════════════

def get_model_features(model_type: str = "ats", live: bool = False) -> list[str]:
    """Query nfl.features for the current set of feature column names.

    Uses a `psql` subprocess (via docker exec) identical to the MLB pattern.

    Parameters
    ----------
    model_type : "ats" or "ou"
    live : bool
        If True, use the `live_ats` / `live_ou` flag instead of `current_ats` / `current_ou`.

    Returns
    -------
    list of feature column name strings sorted alphabetically.
    """
    flag = f"live_{model_type}" if live else f"current_{model_type}"
    try:
        result = subprocess.run(
            [
                "docker", "exec", "-i", DB_CONTAINER,
                "psql", "-U", "earl", "-d", "earl_knows_football",
                "-t", "-A",
                "-c",
                f"SELECT name FROM nfl.features WHERE {flag} = true ORDER BY name;",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            log(f"get_model_features psql error: {result.stderr.strip()}")
            return []
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        return lines
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"get_model_features failed: {e}")
        return []


def build_feature_catalog() -> None:
    """Insert / update all features from ATS_CATALOG, OU_CATALOG, and
    COMPUTED_FEATURES_CATALOG into the nfl.features table.

    This mirrors the pattern used by MLB and should be called after schema
    creation or when the feature set changes.
    """
    # Build a unified map: name -> (description, is_current_ats, is_current_ou,
    #                               is_live_ats, is_live_ou, is_trainable, display_name)
    all_features: dict[str, tuple] = {}

    # ATS-tagged features
    for name, desc in ATS_CATALOG.items():
        if name in OU_CATALOG:
            # Shared — set both flags
            all_features[name] = (
                desc, True, True, True, True,
                True, DISPLAY_NAMES.get(name),
            )
        else:
            all_features[name] = (
                desc, True, False, True, False,
                True, DISPLAY_NAMES.get(name),
            )

    # OU-tagged features not already in ATS
    for name, desc in OU_CATALOG.items():
        if name not in all_features:
            all_features[name] = (
                desc, False, True, False, True,
                True, DISPLAY_NAMES.get(name),
            )

    # Computed (non-trainable)
    for name, desc in COMPUTED_FEATURES_CATALOG.items():
        all_features[name] = (
            desc, False, False, False, False,
            False, DISPLAY_NAMES.get(name),
        )

    # Build and execute the UPSERT SQL
    values_clauses = []
    for name, (desc, c_ats, c_ou, l_ats, l_ou, trainable, dname) in all_features.items():
        desc_escaped = desc.replace("'", "''")
        dname_escaped = (dname or name.replace("_", " ").title()).replace("'", "''")
        values_clauses.append(
            f"('{name}', '{desc_escaped}', '{dname_escaped}', "
            f"{'TRUE' if c_ats else 'FALSE'}, {'TRUE' if c_ou else 'FALSE'}, "
            f"{'TRUE' if l_ats else 'FALSE'}, {'TRUE' if l_ou else 'FALSE'}, "
            f"{'TRUE' if trainable else 'FALSE'})"
        )

    sql = f"""
        INSERT INTO nfl.features
            (name, description, display_name,
             current_ats, current_ou,
             live_ats, live_ou,
             is_trainable)
        VALUES
            {', '.join(values_clauses)}
        ON CONFLICT (name) DO UPDATE SET
            description = EXCLUDED.description,
            display_name = EXCLUDED.display_name,
            current_ats = EXCLUDED.current_ats,
            current_ou = EXCLUDED.current_ou,
            live_ats = EXCLUDED.live_ats,
            live_ou = EXCLUDED.live_ou,
            is_trainable = EXCLUDED.is_trainable;
    """

    try:
        result = subprocess.run(
            [
                "docker", "exec", "-i", DB_CONTAINER,
                "psql", "-U", "earl", "-d", "earl_knows_football",
                "-c", sql,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            log(f"Feature catalog built: {len(all_features)} features")
        else:
            log(f"Feature catalog insert error: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log(f"Feature catalog insert failed: {e}")


# ════════════════════════════════════════════════════════════════════════
# SQL QUERIES
# ════════════════════════════════════════════════════════════════════════

GAME_QUERY = """
    SELECT
        g.id AS game_id,
        g.season_id,
        s.year,
        g.week,
        g.game_type,
        g.date AS gametime,
        g.venue AS stadium,
        g.surface,
        g.roof_type,
        g.temperature,
        g.wind_speed AS wind,
        g.weather_condition,
        ht.abbreviation AS home_abbr,
        ht.name AS home_name,
        at.abbreviation AS away_abbr,
        at.name AS away_name,
        g.home_score,
        g.away_score,
        blc.closing_spread AS spread,
        blc.closing_ou AS over_under,
        blc.opening_spread,
        blc.opening_ou,
        blc.closing_home_ml,
        blc.closing_away_ml,
        blc.opening_home_ml,
        blc.opening_away_ml,
        blc.closing_spread_home_odds,
        blc.closing_spread_away_odds,
        blc.closing_over_odds,
        blc.closing_under_odds,
        blc.closing_home_implied_probability,
        blc.closing_away_implied_probability,
        blc.opening_spread_home_odds,
        blc.opening_spread_away_odds,
        blc.opening_over_odds,
        blc.opening_under_odds,
        blc.opening_home_implied_probability,
        blc.opening_away_implied_probability
    FROM nfl.games g
    JOIN nfl.seasons s ON s.id = g.season_id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    LEFT JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
    WHERE 1=1
"""

TRAINING_WHERE = """
    AND g.game_type = 'REG'
    AND g.home_score IS NOT NULL
    AND g.away_score IS NOT NULL
    AND blc.game_id IS NOT NULL
"""

INFERENCE_WHERE = """
    AND g.game_type = 'REG'
    AND g.home_score IS NULL
    AND blc.game_id IS NOT NULL
"""


# ════════════════════════════════════════════════════════════════════════
# build_features() — core feature engineering
# ════════════════════════════════════════════════════════════════════════

def build_features(df: pd.DataFrame, log_fn: callable = log) -> pd.DataFrame:
    """Build all ATS/OU features for a games DataFrame.

    Parameters
    ----------
    df : pd.DataFrame
        Must have columns matching GAME_QUERY plus rolling-average features
        (pre-computed by _add_computed_features or provided externally).
    log_fn : callable
        Optional logging function (defaults to module-level log).

    Returns
    -------
    pd.DataFrame with additional feature columns filled in.
    """
    df = df.copy()

    # ── Spread (from nfl.betting_lines_consolidated) ──────────────
    # closing_spread is aliased as 'spread' in GAME_QUERY
    df["spread"] = df["spread"].fillna(df["opening_spread"])
    df["spread_movement"] = df["spread"] - df["opening_spread"]
    df["spread_movement"] = df["spread_movement"].fillna(0)

    # ── Over/under ────────────────────────────────────────────────
    # closing_ou is aliased as 'over_under' in GAME_QUERY
    df["ou"] = df["over_under"].fillna(df["opening_ou"])
    df["opening_ou"] = df["opening_ou"].fillna(df["over_under"])
    df["ou_movement"] = df["ou"] - df["opening_ou"]

    # ── Spread odds movement ──────────────────────────────────────
    if "closing_spread_home_odds" in df.columns and "opening_spread_home_odds" in df.columns:
        df["sp_h_odds_mvmt"] = df["closing_spread_home_odds"] - df["opening_spread_home_odds"]
    else:
        df["sp_h_odds_mvmt"] = 0.0
    if "closing_spread_away_odds" in df.columns and "opening_spread_away_odds" in df.columns:
        df["sp_a_odds_mvmt"] = df["closing_spread_away_odds"] - df["opening_spread_away_odds"]
    else:
        df["sp_a_odds_mvmt"] = 0.0

    # ── Rolling averages (set externally via _add_computed_features) ─
    needs_default = [
        "hpf", "hpa", "apf", "apa",
        "dpf", "dpa", "himp", "aimp", "dimp",
        "home_win_pct_r5", "away_win_pct_r5",
        "home_margin_r3", "away_margin_r3",
        "home_margin_r10", "away_margin_r10",
        "home_cover_pct_r5", "away_cover_pct_r5",
        "home_embarrassed", "away_embarrassed",
        "home_season_ats_pct", "away_season_ats_pct",
    ]
    for col in needs_default:
        if col not in df.columns:
            df[col] = 0.0

    # ── Rest days: compute from date column ────────────────────────
    if "rest_diff" not in df.columns and "gametime" in df.columns:
        # For each game, compute rest days as days since each team's
        # previous game.  This is done more accurately in
        # _add_computed_features, but we set defaults here.
        df["rest_diff"] = 0.0
    elif "rest_diff" not in df.columns:
        df["rest_diff"] = 0.0
    df["rest_diff"] = df["rest_diff"].fillna(0)

    # ── Travel distance using haversine ────────────────────────────
    if "travel_miles" not in df.columns:
        df["travel_miles"] = df.apply(
            lambda r: _haversine(
                COORDS.get(r["away_abbr"], (0, 0))[0],
                COORDS.get(r["away_abbr"], (0, 0))[1],
                COORDS.get(r["home_abbr"], (0, 0))[0],
                COORDS.get(r["home_abbr"], (0, 0))[1],
            ),
            axis=1,
        )
    df["travel_miles"] = df["travel_miles"].fillna(0)

    if "tz_diff" not in df.columns:
        df["tz_diff"] = df.apply(
            lambda r: TZ.get(r["home_abbr"], -5) - TZ.get(r["away_abbr"], -5),
            axis=1,
        )

    if "is_short" not in df.columns:
        df["is_short"] = (df["rest_diff"] < -1).astype(int)

    if "is_dome" not in df.columns:
        df["is_dome"] = df["home_abbr"].isin(DOMES).astype(int)

    # ── Weather: temperature and wind ─────────────────────────────
    if "temp" not in df.columns:
        df["temp"] = df.get("temperature", pd.Series(0.0, index=df.index)).fillna(70.0)
    if "wind" not in df.columns:
        df["wind"] = df.get("wind", pd.Series(0.0, index=df.index)).fillna(0.0)

    # ── Fill remaining NaN with 0 ──────────────────────────────────
    df = df.fillna(0)

    return df


# ════════════════════════════════════════════════════════════════════════
# NFLDataLoader class
# ════════════════════════════════════════════════════════════════════════

class NFLDataLoader:
    """NFL-specific DataLoader mirroring MLBDataLoader's interface.

    Handles DB connection, game data loading, feature computation, and
    feature-catalog maintenance in the nfl.features table.
    """

    def __init__(self, db_url: str = None):
        self.db_url = db_url or DEFAULT_DB_URL
        self.engine = None
        self._features: dict[str, dict] = {}

        # Load feature metadata from catalogs (mirrors MLB's _features dict)
        for name, desc in ATS_CATALOG.items():
            self._features[name] = {
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                "current_ats": True,
                "current_ou": name in OU_CATALOG,
                "is_trainable": True,
            }
        for name, desc in OU_CATALOG.items():
            if name not in self._features:
                self._features[name] = {
                    "description": desc,
                    "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                    "current_ats": False,
                    "current_ou": True,
                    "is_trainable": True,
                }
        for name, desc in COMPUTED_FEATURES_CATALOG.items():
            self._features[name] = {
                "description": desc,
                "display_name": DISPLAY_NAMES.get(name, name.replace("_", " ").title()),
                "current_ats": False,
                "current_ou": False,
                "is_trainable": False,
            }

    # ── Connection management ─────────────────────────────────────

    async def get_engine(self):
        if self.engine is None:
            self.engine = create_async_engine(self.db_url, pool_pre_ping=True)
        return self.engine

    async def close(self):
        if self.engine:
            await self.engine.dispose()
            self.engine = None

    # ── Data loading ──────────────────────────────────────────────

    async def load_games(self, where_clause: str = "",
                         engine=None) -> pd.DataFrame:
        """Load game data from the NFL schema."""
        e = engine or await self.get_engine()
        sql = GAME_QUERY + " " + where_clause
        df = await self._fetch(sql, e)
        return df

    async def load_training_data(self, min_year=2021, max_year=None,
                                 engine=None) -> pd.DataFrame:
        """Load completed regular-season games with betting lines."""
        e = engine or await self.get_engine()
        where = TRAINING_WHERE
        if min_year:
            where += f" AND s.year >= {min_year}"
        if max_year:
            where += f" AND s.year <= {max_year}"
        return await self.load_games(where, e)

    async def load_inference_data(self, year: int = None, week: int = None,
                                  engine=None) -> pd.DataFrame:
        """Load upcoming (unscored) regular-season games."""
        e = engine or await self.get_engine()
        where = INFERENCE_WHERE
        if year:
            where += f" AND s.year = {year}"
        if week:
            where += f" AND g.week = {week}"
        return await self.load_games(where, e)

    async def _fetch(self, sql: str, engine=None) -> pd.DataFrame:
        """Execute raw SQL and return a DataFrame."""
        e = engine or await self.get_engine()
        async with e.connect() as conn:
            result = await conn.execute(sa_text(sql))
            rows = result.fetchall()
            cols = result.keys()
        return pd.DataFrame(rows, columns=cols)

    # ── Feature registry ──────────────────────────────────────────

    def register_feature(self, name: str, description: str,
                         display_name: str = None,
                         current_ats: bool = True,
                         current_ou: bool = True,
                         is_trainable: bool = True):
        """Add or update a single feature in the loader's in-memory registry."""
        self._features[name] = {
            "description": description,
            "display_name": display_name or name.replace("_", " ").title(),
            "current_ats": current_ats,
            "current_ou": current_ou,
            "is_trainable": is_trainable,
        }

    def get_features(self, ats_only: bool = False, ou_only: bool = False,
                     trainable_only: bool = False) -> list[str]:
        """Return registered feature names filtered by type."""
        result = []
        for name, meta in self._features.items():
            if trainable_only and not meta.get("is_trainable", True):
                continue
            if ats_only and not meta.get("current_ats", False):
                continue
            if ou_only and not meta.get("current_ou", False):
                continue
            result.append(name)
        return result

    async def ensure_features_in_db(self, conn=None):
        """UPSERT all registered features into nfl.features using psql."""
        # Delegate to the module-level build_feature_catalog
        build_feature_catalog()

    # ── Training data preparation ──────────────────────────────────

    async def build_training_data(self, min_year=2021, max_year=None,
                                  engine=None) -> pd.DataFrame:
        """Return a DataFrame with all feature columns + margin/total labels."""
        df = await self.load_training_data(min_year=min_year,
                                           max_year=max_year,
                                           engine=engine)
        df = build_features(df)
        df = await self._add_computed_features(df, engine)

        # Add label columns
        df["margin"] = df["home_score"] - df["away_score"]
        df["total"] = df["home_score"] + df["away_score"]

        return df

    async def build_inference_data(self, year: int, week: int = None,
                                   engine=None) -> pd.DataFrame:
        """Load upcoming games and compute features for prediction."""
        df = await self.load_inference_data(year=year, week=week, engine=engine)
        df = build_features(df)
        df = await self._add_computed_features(df, engine)
        return df

    # ── Computed / rolling features ────────────────────────────────

    async def _add_computed_features(self, df: pd.DataFrame,
                                     engine=None) -> pd.DataFrame:
        """Add rolling-average features that require DB queries per team.

        This mimics the per-game feature engineering in predict_margin
        and predict_total, but batched for efficiency.
        """
        e = engine or await self.get_engine()
        df = df.copy()

        # Season average points per game
        async with e.connect() as conn:
            result = await conn.execute(sa_text(f"""
                SELECT s.year, AVG((g.home_score + g.away_score) / 2.0) AS avg_pts
                FROM nfl.games g
                JOIN nfl.seasons s ON s.id = g.season_id
                WHERE g.home_score IS NOT NULL AND g.game_type = 'REG'
                GROUP BY s.year
            """))
            rows = result.fetchall()
        season_avg = {r[0]: float(r[1]) for r in rows}
        df["season_avg_pts"] = df["year"].map(season_avg).fillna(21.0)

        # Rolling avgs per team — load all completed games
        async with e.connect() as conn:
            all_raw = await conn.execute(sa_text(f"""
                SELECT g.id, s.year, g.week, g.date, g.home_score, g.away_score,
                       ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr,
                       blc.closing_spread AS spread
                FROM nfl.games g
                JOIN nfl.seasons s ON s.id = g.season_id
                JOIN nfl.teams ht ON ht.id = g.home_team_id
                JOIN nfl.teams at ON at.id = g.away_team_id
                JOIN nfl.betting_lines_consolidated blc ON blc.game_id = g.id
                WHERE g.home_score IS NOT NULL AND g.game_type = 'REG'
                ORDER BY g.date, g.id
            """))
        all_data = all_raw.fetchall()
        cols = all_raw.keys()
        all_games_df = pd.DataFrame(all_data, columns=cols)

        # Build per-team chronological game history
        team_games = {}
        for _, row in all_games_df.iterrows():
            for abbr, side in [(row["home_abbr"], "home"), (row["away_abbr"], "away")]:
                if abbr not in team_games:
                    team_games[abbr] = []
                if side == "home":
                    margin = row["home_score"] - row["away_score"]
                    for_pts = row["home_score"]
                    against_pts = row["away_score"]
                else:
                    margin = row["away_score"] - row["home_score"]
                    for_pts = row["away_score"]
                    against_pts = row["home_score"]
                spread = row["spread"] if row["spread"] else 0
                covered = None
                if spread != 0:
                    covered = 1 if margin > -spread else 0
                team_games[abbr].append({
                    "year": row["year"], "week": row["week"],
                    "date": row["date"],
                    "margin": margin, "for_pts": for_pts,
                    "against_pts": against_pts,
                    "covered": covered, "spread": spread,
                })

        for abbr in team_games:
            team_games[abbr].sort(key=lambda g: (g["year"], g["week"]))

        # Rolling helpers
        def _avg(stats, n, attr):
            recent = [g[attr] for g in stats[-n:] if g[attr] is not None]
            return sum(recent) / len(recent) if recent else 0.0

        def _win_pct(stats, n):
            recent = stats[-n:]
            return sum(1 for g in recent if g["margin"] > 0) / len(recent) if recent else 0.0

        def _cover_pct(stats, n):
            recent = [g for g in stats[-n:] if g["covered"] is not None]
            return sum(g["covered"] for g in recent) / len(recent) if recent else 0.0

        def _embarrassed(stats):
            return 1 if stats and stats[-1]["margin"] <= -14 else 0

        # Pre-allocate
        roll_cols = [
            "hpf", "hpa", "apf", "apa",
            "dpf", "dpa", "himp", "aimp", "dimp",
            "home_win_pct_r5", "away_win_pct_r5",
            "home_margin_r3", "away_margin_r3",
            "home_margin_r10", "away_margin_r10",
            "home_cover_pct_r5", "away_cover_pct_r5",
            "home_embarrassed", "away_embarrassed",
            "home_season_ats_pct", "away_season_ats_pct",
        ]
        rolling = {col: [] for col in roll_cols}

        for _, row in df.iterrows():
            ha, aa = row["home_abbr"], row["away_abbr"]
            yr, wk = int(row["year"]), int(row.get("week", 1))

            h_prior = [g for g in team_games.get(ha, [])
                       if (g["year"], g["week"]) < (yr, wk)]
            a_prior = [g for g in team_games.get(aa, [])
                       if (g["year"], g["week"]) < (yr, wk)]

            rolling["hpf"].append(_avg(h_prior, 10, "for_pts"))
            rolling["hpa"].append(_avg(h_prior, 10, "against_pts"))
            rolling["apf"].append(_avg(a_prior, 10, "for_pts"))
            rolling["apa"].append(_avg(a_prior, 10, "against_pts"))

            rolling["dpf"].append(rolling["hpf"][-1])
            rolling["dpa"].append(rolling["hpa"][-1])
            rolling["himp"].append(rolling["hpf"][-1])
            rolling["aimp"].append(rolling["apf"][-1])
            rolling["dimp"].append(rolling["hpf"][-1] - rolling["apf"][-1])

            # Rest days: days since each team's last game
            h_rest = 7
            if len(h_prior) > 0:
                last_h = h_prior[-1]
                if last_h["date"] is not None and row["gametime"] is not None:
                    h_rest = (row["gametime"] - last_h["date"]).days
            a_rest = 7
            if len(a_prior) > 0:
                last_a = a_prior[-1]
                if last_a["date"] is not None and row["gametime"] is not None:
                    a_rest = (row["gametime"] - last_a["date"]).days

            rolling["home_win_pct_r5"].append(_win_pct(h_prior, 5))
            rolling["away_win_pct_r5"].append(_win_pct(a_prior, 5))
            rolling["home_margin_r3"].append(_avg(h_prior, 3, "margin"))
            rolling["away_margin_r3"].append(_avg(a_prior, 3, "margin"))
            rolling["home_margin_r10"].append(_avg(h_prior, 10, "margin"))
            rolling["away_margin_r10"].append(_avg(a_prior, 10, "margin"))

            rolling["home_cover_pct_r5"].append(_cover_pct(h_prior, 5))
            rolling["away_cover_pct_r5"].append(_cover_pct(a_prior, 5))

            rolling["home_embarrassed"].append(_embarrassed(h_prior))
            rolling["away_embarrassed"].append(_embarrassed(a_prior))

            all_h_cov = [g["covered"] for g in h_prior if g["covered"] is not None]
            all_a_cov = [g["covered"] for g in a_prior if g["covered"] is not None]
            rolling["home_season_ats_pct"].append(
                sum(all_h_cov) / len(all_h_cov) if all_h_cov else 0.0)
            rolling["away_season_ats_pct"].append(
                sum(all_a_cov) / len(all_a_cov) if all_a_cov else 0.0)

        for col, vals in rolling.items():
            df[col] = vals

        # Compute rest_diff from the computed per-team rest days
        # (stored as h_rest / a_rest above; we need to re-derive)
        # Re-derive using the team_games data we already have:
        rest_diffs = []
        for _, row in df.iterrows():
            ha, aa = row["home_abbr"], row["away_abbr"]
            yr, wk = int(row["year"]), int(row.get("week", 1))
            h_prior = [g for g in team_games.get(ha, [])
                       if (g["year"], g["week"]) < (yr, wk)]
            a_prior = [g for g in team_games.get(aa, [])
                       if (g["year"], g["week"]) < (yr, wk)]
            h_rest = 7
            if len(h_prior) > 0 and h_prior[-1]["date"] is not None and row["gametime"] is not None:
                h_rest = (row["gametime"] - h_prior[-1]["date"]).days
            a_rest = 7
            if len(a_prior) > 0 and a_prior[-1]["date"] is not None and row["gametime"] is not None:
                a_rest = (row["gametime"] - a_prior[-1]["date"]).days
            rest_diffs.append(h_rest - a_rest)
        df["rest_diff"] = rest_diffs

        return df

    # ── Live helpers ───────────────────────────────────────────────

    async def build_live_training(self, engine=None) -> pd.DataFrame:
        return await self.build_training_data(engine=engine)

    async def build_live_inference(self, year: int = None, week: int = None,
                                   engine=None) -> pd.DataFrame:
        return await self.build_inference_data(year=year, week=week, engine=engine)


# ════════════════════════════════════════════════════════════════════════
# Top-level convenience
# ════════════════════════════════════════════════════════════════════════

def get_data_loader(db_url: str = None) -> NFLDataLoader:
    """Return an NFLDataLoader instance."""
    return NFLDataLoader(db_url)


async def demo():
    """Quick smoke test."""
    dl = NFLDataLoader()
    df = await dl.load_training_data(min_year=2024, max_year=2024)
    log(f"Loaded {len(df)} training games for 2024")
    df = build_features(df)
    log(f"Feature columns: {len(df.columns)}")
    log(f"Columns: {list(df.columns)}")
    await dl.close()


if __name__ == "__main__":
    asyncio.run(demo())
