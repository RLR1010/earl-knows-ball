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
            def_pass_yards       AS def_pass_ypg,
            def_rush_yards       AS def_rush_ypg,
            (SELECT sub.yards_per_play
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS opp_ypp,
            (SELECT sub.first_downs
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_first_downs,
            (SELECT sub.third_down_attempts
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_tda,
            (SELECT sub.third_down_conversions
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_tdc,
            (SELECT sub.fourth_down_attempts
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_fda,
            (SELECT sub.fourth_down_conversions
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_fdc,
            (SELECT sub.red_zone_trips
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_rz_trips,
            (SELECT sub.red_zone_tds
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_rz_tds,
            (SELECT sub.explosive_plays
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_explosive_plays,
            (SELECT sub.three_and_outs
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_three_and_outs,
            (SELECT sub.interceptions_thrown
             FROM nfl.game_stats sub
             WHERE sub.season = gs.season
               AND sub.week = gs.week
               AND sub.team_abbr = gs.opponent_abbr
               AND sub.opponent_abbr = gs.team_abbr) AS def_ints_thrown,
            first_downs          AS first_downs,
            third_down_attempts  AS tda,
            third_down_conversions AS tdc,
            fourth_down_attempts AS fda,
            fourth_down_conversions AS fdc,
            red_zone_trips       AS rz_trips,
            red_zone_tds         AS rz_tds,
            explosive_plays      AS explosive_plays,
            three_and_outs       AS three_and_outs,
            interceptions_thrown AS ints_thrown
        FROM nfl.game_stats gs
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
            ) AS def_ypp_r{window},
            AVG(def_pass_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_pass_ypg_r{window},
            AVG(def_rush_ypg) OVER (
                PARTITION BY team_abbr
                ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_rush_ypg_r{window},
            -- PBP-derived rolling features
            AVG(first_downs) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS first_downs_r{window},
            CASE WHEN SUM(tda) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(tdc) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(tda) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS third_down_pct_r{window},
            CASE WHEN SUM(fda) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(fdc) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(fda) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS fourth_down_pct_r{window},
            AVG(rz_trips) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS rz_trips_r{window},
            CASE WHEN SUM(rz_trips) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(rz_tds) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(rz_trips) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS rz_td_pct_r{window},
            AVG(explosive_plays) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS explosive_plays_r{window},
            AVG(three_and_outs) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS three_and_outs_r{window},
            AVG(ints_thrown) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS ints_thrown_r{window},
            -- Defensive PBP-derived rolling features
            AVG(def_first_downs) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_first_downs_r{window},
            CASE WHEN SUM(def_tda) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(def_tdc) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(def_tda) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS def_third_down_pct_r{window},
            CASE WHEN SUM(def_fda) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(def_fdc) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(def_fda) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS def_fourth_down_pct_r{window},
            AVG(def_rz_trips) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_rz_trips_r{window},
            CASE WHEN SUM(def_rz_trips) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) > 0
                THEN 100.0 * SUM(def_rz_tds) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ) / NULLIF(SUM(def_rz_trips) OVER (
                    PARTITION BY team_abbr ORDER BY season, week
                    ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
                ), 0)
                ELSE 0
            END AS def_rz_td_pct_r{window},
            AVG(def_explosive_plays) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_explosive_plays_r{window},
            AVG(def_three_and_outs) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_three_and_outs_r{window},
            AVG(def_ints_thrown) OVER (
                PARTITION BY team_abbr ORDER BY season, week
                ROWS BETWEEN {window} PRECEDING AND 1 PRECEDING
            ) AS def_ints_thrown_r{window}
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
        COALESCE(def_pass_ypg_r{window}, 0) AS def_pass_ypg,
        COALESCE(def_rush_ypg_r{window}, 0) AS def_rush_ypg,
        COALESCE(first_downs_r{window}, 0) AS first_downs,
        COALESCE(third_down_pct_r{window}, 0) AS third_down_pct,
        COALESCE(fourth_down_pct_r{window}, 0) AS fourth_down_pct,
        COALESCE(rz_trips_r{window}, 0) AS rz_trips,
        COALESCE(rz_td_pct_r{window}, 0) AS rz_td_pct,
        COALESCE(explosive_plays_r{window}, 0) AS explosive_plays,
        COALESCE(three_and_outs_r{window}, 0) AS three_and_outs,
        COALESCE(ints_thrown_r{window}, 0) AS ints_thrown,
        COALESCE(def_first_downs_r{window}, 0) AS def_first_downs,
        COALESCE(def_third_down_pct_r{window}, 0) AS def_third_down_pct,
        COALESCE(def_fourth_down_pct_r{window}, 0) AS def_fourth_down_pct,
        COALESCE(def_rz_trips_r{window}, 0) AS def_rz_trips,
        COALESCE(def_rz_td_pct_r{window}, 0) AS def_rz_td_pct,
        COALESCE(def_explosive_plays_r{window}, 0) AS def_explosive_plays,
        COALESCE(def_three_and_outs_r{window}, 0) AS def_three_and_outs,
        COALESCE(def_ints_thrown_r{window}, 0) AS def_ints_thrown
    FROM team_rolling
    ORDER BY season, week, team_abbr
    """
    df = pd.read_sql(query, engine)

    # Compute defensive pass/rush splits from opponent stats
    if not df.empty:


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
