"""
Ingest nflverse team-level weekly stats into a PostgreSQL table.

Downloads stats_team CSV files from nflverse GitHub releases,
creates/updates the nfl.game_stats table with per-team per-game aggregates,
and provides a clean source of truth for all yardage/efficiency features.
"""
from __future__ import annotations

import io
import logging
import os
import time
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
import urllib.request
from sqlalchemy import text
from sqlalchemy.engine.base import Engine

logger = logging.getLogger(__name__)


def _to_float(value, default: float = 0.0) -> float:
    """Safe float coercion. nflverse CSVs carry real NaN (not None), which
    `or 0` does NOT catch - so guard explicitly."""
    try:
        if value is None or (isinstance(value, float) and value != value):  # NaN
            return default
        f = float(value)
        return default if f != f else f
    except (TypeError, ValueError):
        return default


def _to_int(value, default: int = 0) -> int:
    """Safe int coercion (handles NaN/None/float strings like '4.0')."""
    return int(round(_to_float(value, float(default))))

# URL template for nflverse stats_team data
STATS_TEAM_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_team/stats_team_week_{year}.csv"

# Years available (stats_team started in 2016)
AVAILABLE_YEARS = list(range(2016, 2026))

# SQL to create the game_stats table
CREATE_GAME_STATS_TABLE = """
CREATE TABLE IF NOT EXISTS nfl.game_stats (
    id SERIAL PRIMARY KEY,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    season_type VARCHAR(4) DEFAULT 'REG',
    team_abbr VARCHAR(3) NOT NULL,
    opponent_abbr VARCHAR(3) NOT NULL,

    -- Offensive stats
    total_yards FLOAT DEFAULT 0,
    pass_yards FLOAT DEFAULT 0,
    rush_yards FLOAT DEFAULT 0,
    pass_attempts INTEGER DEFAULT 0,
    pass_completions INTEGER DEFAULT 0,
    rush_attempts INTEGER DEFAULT 0,
    yards_per_play FLOAT DEFAULT 0,
    pass_ypa FLOAT DEFAULT 0,
    rush_ypa FLOAT DEFAULT 0,
    pass_tds INTEGER DEFAULT 0,
    rush_tds INTEGER DEFAULT 0,
    pass_interceptions INTEGER DEFAULT 0,
    sacks_suffered INTEGER DEFAULT 0,
    sack_yards_lost INTEGER DEFAULT 0,
    fumbles_lost INTEGER DEFAULT 0,

    -- Defensive stats
    def_yards_allowed FLOAT DEFAULT 0,
    def_pass_yards FLOAT DEFAULT 0,
    def_rush_yards FLOAT DEFAULT 0,
    def_interceptions INTEGER DEFAULT 0,
    def_fumbles_recovered INTEGER DEFAULT 0,
    def_sacks INTEGER DEFAULT 0,

    -- Penalties
    penalties INTEGER DEFAULT 0,
    penalty_yards INTEGER DEFAULT 0,

    -- Efficiency
    passing_epa FLOAT DEFAULT 0,
    rushing_epa FLOAT DEFAULT 0,
    receiving_epa FLOAT DEFAULT 0,
    passing_cpoe FLOAT DEFAULT 0,

    -- Added 2026-08-29 (audit): high-value nflverse fields previously dropped

    -- Air yards / YAC / depth
    passing_air_yards FLOAT DEFAULT 0,
    passing_yards_after_catch FLOAT DEFAULT 0,
    receiving_air_yards FLOAT DEFAULT 0,
    receiving_yards_after_catch FLOAT DEFAULT 0,
    passing_10 FLOAT DEFAULT 0,
    passing_16 FLOAT DEFAULT 0,
    passing_20 FLOAT DEFAULT 0,
    passing_40 FLOAT DEFAULT 0,
    rushing_10 FLOAT DEFAULT 0,
    rushing_12 FLOAT DEFAULT 0,
    rushing_20 FLOAT DEFAULT 0,
    rushing_40 FLOAT DEFAULT 0,
    receiving_10 FLOAT DEFAULT 0,
    receiving_16 FLOAT DEFAULT 0,
    receiving_20 FLOAT DEFAULT 0,
    receiving_40 FLOAT DEFAULT 0,

    -- First downs + conversions
    passing_first_downs INTEGER DEFAULT 0,
    rushing_first_downs INTEGER DEFAULT 0,
    receiving_first_downs INTEGER DEFAULT 0,
    passing_2pt_conversions INTEGER DEFAULT 0,
    rushing_2pt_conversions INTEGER DEFAULT 0,
    receiving_2pt_conversions INTEGER DEFAULT 0,
    total_first_downs INTEGER DEFAULT 0,
    targets INTEGER DEFAULT 0,
    timeouts INTEGER DEFAULT 0,
    misc_yards FLOAT DEFAULT 0,
    special_teams_tds INTEGER DEFAULT 0,

    -- Detailed defense
    def_tackles_solo INTEGER DEFAULT 0,
    def_tackle_assists INTEGER DEFAULT 0,
    def_tackles_with_assist INTEGER DEFAULT 0,
    def_tackles_for_loss INTEGER DEFAULT 0,
    def_tackles_for_loss_yards FLOAT DEFAULT 0,
    def_fumbles_forced INTEGER DEFAULT 0,
    def_sack_yards FLOAT DEFAULT 0,
    def_qb_hits INTEGER DEFAULT 0,
    def_interception_yards INTEGER DEFAULT 0,
    def_pass_defended INTEGER DEFAULT 0,
    def_tds INTEGER DEFAULT 0,
    def_safeties INTEGER DEFAULT 0,
    def_punt_blocks INTEGER DEFAULT 0,
    def_pat_blocks INTEGER DEFAULT 0,
    def_fg_blocks INTEGER DEFAULT 0,
    def_2pt_atts INTEGER DEFAULT 0,
    def_2pt_made INTEGER DEFAULT 0,

    -- Kicking
    fg_made INTEGER DEFAULT 0,
    fg_att INTEGER DEFAULT 0,
    fg_missed INTEGER DEFAULT 0,
    fg_blocked INTEGER DEFAULT 0,
    fg_long INTEGER DEFAULT 0,
    fg_pct FLOAT DEFAULT 0,
    fg_made_0_19 INTEGER DEFAULT 0,
    fg_made_20_29 INTEGER DEFAULT 0,
    fg_made_30_39 INTEGER DEFAULT 0,
    fg_made_40_49 INTEGER DEFAULT 0,
    fg_made_50_59 INTEGER DEFAULT 0,
    fg_made_60_ INTEGER DEFAULT 0,
    gwfg_made INTEGER DEFAULT 0,
    gwfg_att INTEGER DEFAULT 0,
    gwfg_distance INTEGER DEFAULT 0,
    pat_made INTEGER DEFAULT 0,
    pat_att INTEGER DEFAULT 0,
    pat_pct FLOAT DEFAULT 0,

    -- Punting
    pt_att INTEGER DEFAULT 0,
    pt_yards INTEGER DEFAULT 0,
    pt_long INTEGER DEFAULT 0,
    pt_inside_20 INTEGER DEFAULT 0,
    pt_downed INTEGER DEFAULT 0,
    pt_touchback INTEGER DEFAULT 0,
    pt_fair_caught INTEGER DEFAULT 0,
    pt_returned INTEGER DEFAULT 0,
    pt_return_yards INTEGER DEFAULT 0,
    pt_net_yards INTEGER DEFAULT 0,
    pt_blocked INTEGER DEFAULT 0,

    -- Returns
    kickoff_returns INTEGER DEFAULT 0,
    kickoff_return_yards INTEGER DEFAULT 0,
    punt_returns INTEGER DEFAULT 0,
    punt_return_yards INTEGER DEFAULT 0,

    -- Fumble detail
    fumble_recovery_own INTEGER DEFAULT 0,
    fumble_recovery_yards_own INTEGER DEFAULT 0,
    fumble_recovery_opp INTEGER DEFAULT 0,
    fumble_recovery_yards_opp INTEGER DEFAULT 0,
    fumble_recovery_tds INTEGER DEFAULT 0,
    fumbles_forced_by_opp INTEGER DEFAULT 0,
    fumbles_not_forced INTEGER DEFAULT 0,
    fumbles_total INTEGER DEFAULT 0,
    fumbles_lost_total INTEGER DEFAULT 0,

    -- Time of possession (seconds from ESPN core team-stats; ingested separately)
    time_of_possession_secs INTEGER DEFAULT 0,
    possession_time_of_possession_string VARCHAR(16) DEFAULT NULL,
    possession_data_source VARCHAR(50) DEFAULT NULL,

    -- Computed
    turnovers INTEGER DEFAULT 0,
    takeaways INTEGER DEFAULT 0,
    turnover_diff INTEGER DEFAULT 0,

    -- Metadata
    data_source VARCHAR(50) DEFAULT 'nflverse_stats_team',
    loaded_at TIMESTAMP DEFAULT NOW(),

    UNIQUE (season, week, team_abbr, opponent_abbr)
);
"""

# Create index for fast lookups
CREATE_GAME_STATS_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_game_stats_team_season ON nfl.game_stats (team_abbr, season, week);
CREATE INDEX IF NOT EXISTS idx_game_stats_opponent ON nfl.game_stats (opponent_abbr, season, week);
"""


def ensure_table_exists(engine: Engine):
    """Create the game_stats table if it doesn't exist, and ALTER it to add any
    new columns defined in CREATE_GAME_STATS_TABLE that are missing on an
    existing table (idempotent via ADD COLUMN IF NOT EXISTS)."""
    with engine.begin() as conn:
        conn.execute(text(CREATE_GAME_STATS_TABLE))

        # Add any columns from the CREATE TABLE that don't yet exist on the
        # (pre-existing) table. Parse the column definitions out of the SQL.
        col_lines = []
        depth = 0
        for line in CREATE_GAME_STATS_TABLE.splitlines():
            s = line.strip().rstrip(",")
            if not s:
                continue
            if s.startswith("--"):  # skip comment lines
                continue
            if "CREATE TABLE" in s.upper():
                # header line: skip, only its "(" opens the body
                depth += 1
                continue
            # Track paren nesting: the column body is between the CREATE's "("
            # and the closing ")".
            depth += s.count("(") - s.count(")")
            if depth <= 0:
                continue
            if s.upper().startswith("PRIMARY"):
                continue
            # A column line: <name> <TYPE> [constraints]. Skip constraints-only lines.
            if " " not in s:
                continue
            name, _, rest = s.partition(" ")
            rest_stripped = rest.strip().split(" ")[0].upper()
            if not rest_stripped or rest_stripped.startswith("DEFAULT") or rest_stripped == "NOT":
                continue
            if name.upper() in ("CONSTRAINT", "UNIQUE", "CHECK", "FOREIGN", "PRIMARY", "REFERENCES"):
                continue
            # Reconstruct col + type (drop trailing constraints like DEFAULT/REFERENCES)
            data_type = rest.strip()
            for token in (" DEFAULT ", " REFERENCES ", " NOT NULL", " PRIMARY KEY", " UNIQUE"):
                i = data_type.upper().find(token)
                if i != -1:
                    data_type = data_type[:i]
                    break
            data_type = data_type.strip()
            if not data_type:
                continue
            col_lines.append((name, data_type))

        for name, dtype in col_lines:
            conn.execute(text(
                f"ALTER TABLE nfl.game_stats ADD COLUMN IF NOT EXISTS {name} {dtype}"
            ))

        for stmt in CREATE_GAME_STATS_INDEXES.split(";"):
            s = stmt.strip()
            if s:
                try:
                    conn.execute(text(s))
                except Exception:
                    pass
    logger.info("✅ nfl.game_stats table ready (columns reconciled)")


def download_stats_team_year(year: int) -> pd.DataFrame:
    """Download stats_team data for a single year from nflverse."""
    url = STATS_TEAM_URL.format(year=year)
    logger.info("Downloading %s...", url)
    req = urllib.request.Request(url, headers={"User-Agent": "python-nflverse-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        df = pd.read_csv(resp)
    logger.info("  → %d rows, %d columns", len(df), len(df.columns))
    return df


def process_stats_team(df: pd.DataFrame) -> pd.DataFrame:
    """Transform raw stats_team DataFrame into game_stats format.

    The raw data has one row per team per week (2 rows per game).
    We compute both offensive and defensive stats for each team.
    """
    records = []

    for _, row in df.iterrows():
        team = str(row.get("team", "")).strip()
        opp = str(row.get("opponent_team", "")).strip()
        week = int(row.get("week", 0))
        season = int(row.get("season", 0))
        if not season:
            continue

        pass_yds = float(row.get("passing_yards", 0) or 0)
        rush_yds = float(row.get("rushing_yards", 0) or 0)

        pass_att = int(row.get("attempts", 0) or 0)
        rush_att = int(row.get("carries", 0) or 0)

        rush_yds_only = rush_yds
        total_off_yards = pass_yds + rush_yds_only

        total_plays = pass_att + rush_att
        ypp = round(total_off_yards / total_plays, 2) if total_plays > 0 else 0.0
        pass_ypa = round(pass_yds / pass_att, 2) if pass_att > 0 else 0.0
        rush_ypa = round(rush_yds_only / rush_att, 2) if rush_att > 0 else 0.0

        pass_int = int(row.get("passing_interceptions", 0) or 0)
        sack_fumble_lost = int(row.get("sack_fumbles_lost", 0) or 0)
        rush_fumble_lost = int(row.get("rushing_fumbles_lost", 0) or 0)
        turnovers = pass_int + sack_fumble_lost + rush_fumble_lost

        # Defensive takeaways
        def_int = int(row.get("def_interceptions", 0) or 0)
        def_fumble_rec = int(row.get("fumble_recovery_opp", 0) or 0)
        takeaways = def_int + def_fumble_rec

        record = {
            "season": season,
            "week": week,
            "season_type": str(row.get("season_type", "REG")),
            "team_abbr": team,
            "opponent_abbr": opp,
            # Offense
            "total_yards": total_off_yards,
            "pass_yards": pass_yds,
            "rush_yards": rush_yds_only,
            "pass_attempts": pass_att,
            "pass_completions": int(row.get("completions", 0) or 0),
            "rush_attempts": rush_att,
            "yards_per_play": ypp,
            "pass_ypa": pass_ypa,
            "rush_ypa": rush_ypa,
            "pass_tds": int(row.get("passing_tds", 0) or 0),
            "rush_tds": int(row.get("rushing_tds", 0) or 0),
            "pass_interceptions": pass_int,
            "sacks_suffered": int(row.get("sacks_suffered", 0) or 0),
            "sack_yards_lost": int(row.get("sack_yards_lost", 0) or 0),
            "fumbles_lost": sack_fumble_lost + rush_fumble_lost,
            # Defense
            "def_yards_allowed": 0,
            "def_pass_yards": 0,
            "def_rush_yards": 0,
            "def_interceptions": def_int,
            "def_fumbles_recovered": def_fumble_rec,
            "def_sacks": int(row.get("def_sacks", 0) or 0),
            # Penalties
            "penalties": int(row.get("penalties", 0) or 0),
            "penalty_yards": int(row.get("penalty_yards", 0) or 0),
            # Efficiency
            "passing_epa": float(row.get("passing_epa", 0) or 0),
            "rushing_epa": float(row.get("rushing_epa", 0) or 0),
            "receiving_epa": float(row.get("receiving_epa", 0) or 0),
            "passing_cpoe": float(row.get("passing_cpoe", 0) or 0),
            # Added 2026-08-29: air yards / YAC / depth buckets
            "passing_air_yards": _to_float(row.get("passing_air_yards")),
            "passing_yards_after_catch": _to_float(row.get("passing_yards_after_catch")),
            "receiving_air_yards": _to_float(row.get("receiving_air_yards")),
            "receiving_yards_after_catch": _to_float(row.get("receiving_yards_after_catch")),
            "passing_10": _to_float(row.get("passing_10")),
            "passing_16": _to_float(row.get("passing_16")),
            "passing_20": _to_float(row.get("passing_20")),
            "passing_40": _to_float(row.get("passing_40")),
            "rushing_10": _to_float(row.get("rushing_10")),
            "rushing_12": _to_float(row.get("rushing_12")),
            "rushing_20": _to_float(row.get("rushing_20")),
            "rushing_40": _to_float(row.get("rushing_40")),
            "receiving_10": _to_float(row.get("receiving_10")),
            "receiving_16": _to_float(row.get("receiving_16")),
            "receiving_20": _to_float(row.get("receiving_20")),
            "receiving_40": _to_float(row.get("receiving_40")),
            # First downs / conversions / misc
            "passing_first_downs": _to_int(row.get("passing_first_downs")),
            "rushing_first_downs": _to_int(row.get("rushing_first_downs")),
            "receiving_first_downs": _to_int(row.get("receiving_first_downs")),
            "passing_2pt_conversions": _to_int(row.get("passing_2pt_conversions")),
            "rushing_2pt_conversions": _to_int(row.get("rushing_2pt_conversions")),
            "receiving_2pt_conversions": _to_int(row.get("receiving_2pt_conversions")),
            "total_first_downs": _to_int(row.get("passing_first_downs")) + _to_int(
                row.get("rushing_first_downs")
            ) + _to_int(
                row.get("receiving_first_downs")
            ) + _to_int(row.get("penalty_first_downs")),
            "targets": _to_int(row.get("targets")),
            "timeouts": _to_int(row.get("timeouts")),
            "misc_yards": _to_float(row.get("misc_yards")),
            "special_teams_tds": _to_int(row.get("special_teams_tds")),
            # Detailed defense
            "def_tackles_solo": _to_int(row.get("def_tackles_solo")),
            "def_tackle_assists": _to_int(row.get("def_tackle_assists")),
            "def_tackles_with_assist": _to_int(row.get("def_tackles_with_assist")),
            "def_tackles_for_loss": _to_int(row.get("def_tackles_for_loss")),
            "def_tackles_for_loss_yards": _to_float(row.get("def_tackles_for_loss_yards")),
            "def_fumbles_forced": _to_int(row.get("def_fumbles_forced")),
            "def_sack_yards": _to_float(row.get("def_sack_yards")),
            "def_qb_hits": _to_int(row.get("def_qb_hits")),
            "def_interception_yards": _to_int(row.get("def_interception_yards")),
            "def_pass_defended": _to_int(row.get("def_pass_defended")),
            "def_tds": _to_int(row.get("def_tds")),
            "def_safeties": _to_int(row.get("def_safeties")),
            "def_punt_blocks": _to_int(row.get("def_punt_blocks")),
            "def_pat_blocks": _to_int(row.get("def_pat_blocks")),
            "def_fg_blocks": _to_int(row.get("def_fg_blocks")),
            "def_2pt_atts": _to_int(row.get("def_2pt_atts")),
            "def_2pt_made": _to_int(row.get("def_2pt_made")),
            # Kicking
            "fg_made": _to_int(row.get("fg_made")),
            "fg_att": _to_int(row.get("fg_att")),
            "fg_missed": _to_int(row.get("fg_missed")),
            "fg_blocked": _to_int(row.get("fg_blocked")),
            "fg_long": _to_int(row.get("fg_long")),
            "fg_pct": _to_float(row.get("fg_pct")),
            "fg_made_0_19": _to_int(row.get("fg_made_0_19")),
            "fg_made_20_29": _to_int(row.get("fg_made_20_29")),
            "fg_made_30_39": _to_int(row.get("fg_made_30_39")),
            "fg_made_40_49": _to_int(row.get("fg_made_40_49")),
            "fg_made_50_59": _to_int(row.get("fg_made_50_59")),
            "fg_made_60_": _to_int(row.get("fg_made_60_")),
            "gwfg_made": _to_int(row.get("gwfg_made")),
            "gwfg_att": _to_int(row.get("gwfg_att")),
            "gwfg_distance": _to_int(row.get("gwfg_distance")),
            "pat_made": _to_int(row.get("pat_made")),
            "pat_att": _to_int(row.get("pat_att")),
            "pat_pct": _to_float(row.get("pat_pct")),
            # Punting
            "pt_att": _to_int(row.get("pt_att")),
            "pt_yards": _to_int(row.get("pt_yards")),
            "pt_long": _to_int(row.get("pt_long")),
            "pt_inside_20": _to_int(row.get("pt_inside_20")),
            "pt_downed": _to_int(row.get("pt_downed")),
            "pt_touchback": _to_int(row.get("pt_touchback")),
            "pt_fair_caught": _to_int(row.get("pt_fair_caught")),
            "pt_returned": _to_int(row.get("pt_returned")),
            "pt_return_yards": _to_int(row.get("pt_return_yards")),
            "pt_net_yards": _to_int(row.get("pt_net_yards")),
            "pt_blocked": _to_int(row.get("pt_blocked")),
            # Returns
            "kickoff_returns": _to_int(row.get("kickoff_returns")),
            "kickoff_return_yards": _to_int(row.get("kickoff_return_yards")),
            "punt_returns": _to_int(row.get("punt_returns")),
            "punt_return_yards": _to_int(row.get("punt_return_yards")),
            # Fumble detail
            "fumble_recovery_own": _to_int(row.get("fumble_recovery_own")),
            "fumble_recovery_yards_own": _to_int(row.get("fumble_recovery_yards_own")),
            "fumble_recovery_opp": _to_int(row.get("fumble_recovery_opp")),
            "fumble_recovery_yards_opp": _to_int(row.get("fumble_recovery_yards_opp")),
            "fumble_recovery_tds": _to_int(row.get("fumble_recovery_tds")),
            "fumbles_forced_by_opp": _to_int(row.get("fumbles_forced_by_opp")),
            "fumbles_not_forced": _to_int(row.get("fumbles_not_forced")),
            "fumbles_total": _to_int(row.get("fumbles_total")),
            "fumbles_lost_total": _to_int(row.get("fumbles_lost_total")),
            # Computed
            "turnovers": turnovers,
            "takeaways": takeaways,
            "turnover_diff": takeaways - turnovers,
        }
        records.append(record)

    result = pd.DataFrame(records)
    return result


def compute_defensive_stats(df: pd.DataFrame) -> pd.DataFrame:
    """For each team, compute defensive stats from opponent's offensive stats.

    The raw data has each team's offensive stats. A team's defensive stats
    = what their opponent's offense produced. We join team -> opponent.
    """
    # Rename offensive stats to defensive indicators
    off_to_def = {
        "team_abbr": "def_team",
        "opponent_abbr": "off_team",
        "total_yards": "def_yards_allowed",
        "pass_yards": "def_pass_yards",
        "rush_yards": "def_rush_yards",
        "yards_per_play": "def_ypp",
        "pass_ypa": "def_pass_ypa",
        "rush_ypa": "def_rush_ypa",
        "pass_tds": "def_pass_tds",
        "rush_tds": "def_rush_tds",
    }

    # Get opponent's offensive stats (what they produced against this team)
    opp_stats = df[list(off_to_def.keys()) + ["season", "week"]].rename(
        columns=off_to_def
    )

    # Merge back: for each team, find what their opponent produced
    df_with_def = df.merge(
        opp_stats,
        left_on=["team_abbr", "opponent_abbr", "season", "week"],
        right_on=["def_team", "off_team", "season", "week"],
        how="left",
        suffixes=("", "_def"),
    )

    # Update defensive stats columns
    for col in ["def_yards_allowed", "def_pass_yards", "def_rush_yards"]:
        if f"def_yards_allowed_def" in df_with_def.columns:
            df_with_def[col] = df_with_def[f"def_yards_allowed_def"].fillna(0)

    # Clean up duplicate columns
    for col in df_with_def.columns:
        if col.endswith("_def"):
            df_with_def.drop(columns=[col], inplace=True)

    return df_with_def


def insert_batch(engine: Engine, df: pd.DataFrame, batch_size: int = 500):
    """Insert processed stats in batches, upserting on conflict.

    The column list + upsert clause are derived from the DataFrame columns so
    new transform fields are written automatically (no duplicate hardcoded
    list to maintain). Key columns + `data_source`/`loaded_at` are handled
    explicitly.
    """
    if df.empty:
        return 0

    # Ordered columns from the processed DataFrame (excludes metadata keys)
    key_cols = ["season", "week", "season_type", "team_abbr", "opponent_abbr"]
    non_key = [
        c for c in df.columns
        if c not in key_cols and c not in ("data_source", "loaded_at", "game_id")
    ]
    cols = key_cols + non_key

    # Columns to refresh on conflict: everything except the conflict key.
    update_cols = [c for c in cols if c not in key_cols]
    update_set = ",".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    insert_sql = text(f"""
        INSERT INTO nfl.game_stats
            ({', '.join(cols)})
        VALUES
            ({', '.join([f':{c}' for c in cols])})
        ON CONFLICT (season, week, team_abbr, opponent_abbr)
        DO UPDATE SET
            {update_set},
            data_source = 'nflverse_stats_team',
            loaded_at = NOW()
    """)

    total = 0
    with engine.begin() as conn:
        for start in range(0, len(df), batch_size):
            batch = df.iloc[start:start + batch_size]
            conn.execute(insert_sql, batch.to_dict(orient="records"))
            total += len(batch)
    return total


def ingest_all_years(
    engine: Engine,
    years: Optional[List[int]] = None,
    batch_size: int = 500,
) -> Tuple[int, int]:
    """Download, process, and insert stats_team data for multiple years.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    years : list of int, optional
        Years to ingest. Defaults to AVAILABLE_YEARS.
    batch_size : int
        Rows per insert batch.

    Returns
    -------
    tuple of (rows_processed, total_games)
    """
    if years is None:
        years = AVAILABLE_YEARS

    ensure_table_exists(engine)

    total_rows = 0
    total_games = 0

    for year in years:
        logger.info("=== Processing %d ===", year)
        try:
            raw = download_stats_team_year(year)
            processed = process_stats_team(raw)
            logger.info("  Processed: %d rows for %s", len(processed), year)

            inserted = insert_batch(engine, processed, batch_size)
            total_rows += inserted
            total_games += len(processed) // 2  # 2 rows per game
            logger.info("  Inserted: %d rows (%d games)", inserted, len(processed) // 2)

        except Exception as exc:
            logger.error("Failed to ingest %d: %s", year, exc)
            continue

    return total_rows, total_games


def verify_ingestion(engine: Engine) -> pd.DataFrame:
    """Check what data made it into the table."""
    query = """
    SELECT
        season,
        COUNT(DISTINCT week) as weeks,
        COUNT(*) as rows,
        MIN(season) as min_year,
        MAX(season) as max_year
    FROM nfl.game_stats
    GROUP BY season
    ORDER BY season
    """
    return pd.read_sql(query, engine)


def update_team_stats_from_game_stats(engine: Engine) -> pd.DataFrame:
    """Generate rolling team stats from game_stats for use in features.

    Returns a DataFrame formatted the same as team_stats.compute_team_game_aggregates(),
    which can be merged into the build_features pipeline.
    """
    query = """
    WITH team_offense AS (
        SELECT
            gs.season,
            gs.week,
            gs.team_abbr,
            gs.opponent_abbr,
            gs.total_yards,
            gs.pass_yards,
            gs.rush_yards,
            gs.yards_per_play,
            gs.pass_ypa,
            gs.rush_ypa,
            gs.pass_attempts + gs.rush_attempts AS total_plays,
            gs.turnovers,
            gs.takeaways,
            gs.turnover_diff,
            gs.passing_epa,
            gs.def_yards_allowed,
            gs.pass_tds,
            gs.rush_tds
        FROM nfl.game_stats gs
        WHERE gs.season_type = 'REG'
    ),
    team_rolling AS (
        SELECT
            *,
            AVG(total_yards) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS off_ypg_r5,
            AVG(yards_per_play) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS ypp_r5,
            AVG(pass_yards) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS pass_ypg_r5,
            AVG(rush_yards) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS rush_ypg_r5,
            AVG(pass_ypa) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS pass_ypa_r5,
            AVG(rush_ypa) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS rush_ypa_r5,
            AVG(turnover_diff) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS turnover_diff_r5,
            AVG(def_yards_allowed) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING
            ) AS def_ypg_r5
        FROM team_offense
    )
    SELECT
        CONCAT(team_abbr, '_', season, '_', week) AS game_key,
        team_abbr,
        opponent_abbr,
        season,
        week,
        off_ypg_r5,
        ypp_r5,
        pass_ypg_r5,
        rush_ypg_r5,
        pass_ypa_r5,
        rush_ypa_r5,
        turnover_diff_r5,
        def_ypg_r5,
        -- Also grab current game values (for reference)
        total_yards,
        yards_per_play
    FROM team_rolling
    WHERE off_ypg_r5 IS NOT NULL
    ORDER BY season, week, team_abbr
    """
    return pd.read_sql(query, engine)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from sqlalchemy import create_engine
    from app.db_urls import PSYCOPG2_DATABASE_URL

    engine = create_engine(PSYCOPG2_DATABASE_URL.replace(":5432", ""))

    years = [int(a) for a in sys.argv[1:]] if len(sys.argv) > 1 else AVAILABLE_YEARS

    logger.info("Starting nflverse stats_team ingestion for years: %s", years)
    rows, games = ingest_all_years(engine, years)
    logger.info("Done! %d rows ingested (%d games)", rows, games)

    summary = verify_ingestion(engine)
    print("\n=== Ingestion Summary ===")
    print(summary.to_string())
