"""
Team-level game statistics computed from the nfl.game_stats table.

This table is populated from nflverse stats_team data (2016-present)
and provides real NFL team-level per-game aggregates for:
  - Total yards (offense + defense)
  - Yards per play (YPP)
  - Passing/rushing splits
  - Turnovers, takeaways, turnover differential
  - Efficiency metrics (EPA)

Use instead of the old player_weekly_stats path which only had
simulated data for 2 seasons.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine.base import Engine

logger = logging.getLogger(__name__)


def compute_team_game_aggregates(
    engine: Engine, window: int = 5
) -> pd.DataFrame:
    """Compute per-team rolling stats from nfl.game_stats.

    Returns a DataFrame with one row per team per game, including
    rolling window averages for all key stats, designed to be merged
    into the main game DataFrame used by build_features().

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    window : int
        Rolling window size.

    Returns
    -------
    pd.DataFrame
        Columns: game_key, team_abbr, opp_abbr, season, week,
        and rolling stat columns with _r{window} suffix.
    """
    query = f"""
    WITH team_base AS (
        SELECT
            season,
            week,
            team_abbr,
            opponent_abbr AS opp_abbr,
            total_yards          AS off_ypg,
            yards_per_play       AS ypp,
            pass_yards           AS pass_ypg,
            rush_yards           AS rush_ypg,
            pass_ypa             AS pass_ypa,
            rush_ypa             AS rush_ypa,
            turnovers            AS turnovers,
            takeaways            AS takeaways,
            turnover_diff        AS turnover_diff,
            def_yards_allowed    AS def_ypg,
            (SELECT sub.yards_per_play
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS opp_ypp
        FROM nfl.game_stats gs
        WHERE gs.season_type = 'REG'
    ),
    team_rolling AS (
        SELECT
            *,
            AVG(off_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS off_ypg_r{window},
            AVG(ypp) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS ypp_r{window},
            AVG(pass_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS pass_ypg_r{window},
            AVG(rush_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS rush_ypg_r{window},
            AVG(pass_ypa) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS pass_ypa_r{window},
            AVG(rush_ypa) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS rush_ypa_r{window},
            AVG(turnover_diff) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS turnover_diff_r{window},
            AVG(def_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_ypg_r{window},
            AVG(opp_ypp) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_ypp_r{window}
        FROM team_base
    )
    SELECT
        CONCAT(team_abbr, '_', season, '_', week) AS game_key,
        team_abbr,
        opp_abbr,
        season,
        week,
        COALESCE(off_ypg_r{window}, 0) AS off_ypg,
        COALESCE(ypp_r{window}, 0) AS ypp,
        COALESCE(pass_ypg_r{window}, 0) AS pass_ypg,
        COALESCE(rush_ypg_r{window}, 0) AS rush_ypg,
        COALESCE(pass_ypa_r{window}, 0) AS pass_ypa,
        COALESCE(rush_ypa_r{window}, 0) AS rush_ypa,
        COALESCE(turnover_diff_r{window}, 0) AS turnover_diff,
        COALESCE(def_ypg_r{window}, 0) AS def_ypg,
        COALESCE(def_ypp_r{window}, 0) AS def_ypp,
        -- These are filled in from the opponent side post-query
        0 AS def_pass_ypg,
        0 AS def_rush_ypg
    FROM team_rolling
    ORDER BY season, week, team_abbr
    """
    df = pd.read_sql(query, engine)

    # Compute defensive pass/rush splits from opponent stats
    if not df.empty:
        # Self-join: for each row, find opponent's offensive stats in same game
        opp_pass = df[["season", "week", "team_abbr", "pass_ypg"]].rename(
            columns={"team_abbr": "opp_abbr", "pass_ypg": "def_pass_ypg"}
        )
        opp_rush = df[["season", "week", "team_abbr", "rush_ypg"]].rename(
            columns={"team_abbr": "opp_abbr", "rush_ypg": "def_rush_ypg"}
        )
        df = df.merge(opp_pass, on=["season", "week", "opp_abbr"], how="left")
        df = df.merge(opp_rush, on=["season", "week", "opp_abbr"], how="left")

        # Drop placeholder zeros
        df.drop(columns=["def_pass_ypg_x", "def_rush_ypg_x"], inplace=True, errors="ignore")
        df.rename(
            columns={
                "def_pass_ypg_y": "def_pass_ypg",
                "def_rush_ypg_y": "def_rush_ypg",
            },
            inplace=True,
        )

        logger.info(
            "Team stats loaded: %d rows, seasons %d-%d",
            len(df),
            int(df["season"].min()),
            int(df["season"].max()),
        )
    else:
        logger.warning("No team stats found in nfl.game_stats")
    return df


def get_team_stats_before_game(
    engine: Engine,
    team_abbr: str,
    season: int,
    week: int,
    window: int = 5,
) -> Dict[str, float]:
    """Get rolling stats for a team before a specific game (live inference).

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    team_abbr : str
        Team abbreviation (e.g., 'KC', 'SF').
    season : int
        Season year.
    week : int
        Current week number.
    window : int
        Rolling window.

    Returns
    -------
    dict
        Feature name -> value.
    """
    query = f"""
    WITH team_base AS (
        SELECT
            total_yards AS off_ypg,
            yards_per_play AS ypp,
            pass_yards AS pass_ypg,
            rush_yards AS rush_ypg,
            pass_ypa AS pass_ypa,
            rush_ypa AS rush_ypa,
            turnover_diff AS turnover_diff,
            def_yards_allowed AS def_ypg
        FROM nfl.game_stats
        WHERE team_abbr = '{team_abbr}'
          AND (season < {season} OR (season = {season} AND week < {week}))
          AND season_type = 'REG'
        ORDER BY season DESC, week DESC
        LIMIT {window}
    )
    SELECT
        AVG(off_ypg) AS off_ypg,
        AVG(ypp) AS ypp,
        AVG(pass_ypg) AS pass_ypg,
        AVG(rush_ypg) AS rush_ypg,
        AVG(pass_ypa) AS pass_ypa,
        AVG(rush_ypa) AS rush_ypa,
        AVG(turnover_diff) AS turnover_diff,
        AVG(def_ypg) AS def_ypg
    FROM team_base
    """
    df = pd.read_sql(query, engine)
    if df.empty or df.iloc[0, 0] is None:
        return {}

    row = df.iloc[0]
    return {
        "off_ypg": float(row.get("off_ypg", 0) or 0),
        "ypp": float(row.get("ypp", 0) or 0),
        "pass_ypg": float(row.get("pass_ypg", 0) or 0),
        "rush_ypg": float(row.get("rush_ypg", 0) or 0),
        "pass_ypa": float(row.get("pass_ypa", 0) or 0),
        "rush_ypa": float(row.get("rush_ypa", 0) or 0),
        "turnover_diff": float(row.get("turnover_diff", 0) or 0),
        "def_ypg": float(row.get("def_ypg", 0) or 0),
    }


def get_defensive_stats_before_game(
    engine: Engine,
    team_abbr: str,
    season: int,
    week: int,
    window: int = 5,
) -> Dict[str, float]:
    """Get defensive stats for a team before a given game.

    Defensive stats = what the opponent's offense produced.
    These are already stored in nfl.game_stats.def_yards_allowed etc.

    Parameters
    ----------
    engine : Engine
        SQLAlchemy engine.
    team_abbr : str
        Team abbreviation.
    season : int
        Season year.
    week : int
        Current week.

    Returns
    -------
    dict
        Defensive feature values.
    """
    query = f"""
    SELECT
        AVG(def_yards_allowed) AS def_ypg,
        AVG(s.opponent_ypp) AS def_ypp
    FROM (
        SELECT
            def_yards_allowed,
            (
                SELECT yards_per_play FROM nfl.game_stats sub
                WHERE sub.season = gs.season
                  AND sub.week = gs.week
                  AND sub.team_abbr = gs.opponent_abbr
                  AND sub.opponent_abbr = gs.team_abbr
            ) AS opponent_ypp
        FROM nfl.game_stats gs
        WHERE gs.team_abbr = '{team_abbr}'
          AND (gs.season < {season} OR (gs.season = {season} AND gs.week < {week}))
          AND gs.season_type = 'REG'
        ORDER BY gs.season DESC, gs.week DESC
        LIMIT {window}
    ) s
    """
    df = pd.read_sql(query, engine)
    if df.empty or df.iloc[0, 0] is None:
        return {}

    row = df.iloc[0]
    return {
        "def_ypg": float(row.get("def_ypg", 0) or 0),
        "def_ypp": float(row.get("def_ypp", 0) or 0),
    }
