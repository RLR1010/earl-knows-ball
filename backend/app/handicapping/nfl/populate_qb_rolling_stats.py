#!/usr/bin/env python3
"""
Populate nfl.qb_cumulative_stats and nfl.qb_rolling_stats.

Both tables include the current game (CURRENT ROW boundary), matching the
pattern of nfl.cumulative_game_stats. The data loader uses feeds_into_game_id
(lag-1 shift) to find each team's prior-game QB stats.

  nfl.qb_cumulative_stats — YTD cumulative through current game
  nfl.qb_rolling_stats    — 3, 5, 10 game rolling windows through current game

Usage:
    python -m backend.app.handicapping.nfl.populate_qb_rolling_stats

Or call populate_qb_rolling(seasons=[...]) from code.
"""

import logging
import os
import sys
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  QB Cumulative Stats DDL  —  YTD through current game
# ═══════════════════════════════════════════════════════════════════════════════

CREATE_CUMULATIVE_SQL = """
CREATE TABLE IF NOT EXISTS nfl.qb_cumulative_stats (
    player_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    game_id         INTEGER NOT NULL,
    game_type       VARCHAR(10) NOT NULL DEFAULT 'REG',
    week            INTEGER NOT NULL,
    team_abbr       TEXT NOT NULL,
    opponent_abbr   TEXT,
    game_date       DATE,
    starter_flag    BOOLEAN,

    -- Per-game raw events
    pass_attempts      DOUBLE PRECISION DEFAULT 0,
    pass_completions   DOUBLE PRECISION DEFAULT 0,
    pass_yards         DOUBLE PRECISION DEFAULT 0,
    pass_tds           DOUBLE PRECISION DEFAULT 0,
    pass_int           DOUBLE PRECISION DEFAULT 0,
    rush_attempts      DOUBLE PRECISION DEFAULT 0,
    rush_yards         DOUBLE PRECISION DEFAULT 0,
    rush_tds           DOUBLE PRECISION DEFAULT 0,
    sacks              DOUBLE PRECISION DEFAULT 0,
    fumbles            DOUBLE PRECISION DEFAULT 0,

    -- Cumulative through current game  (UNBOUNDED PRECEDING TO CURRENT ROW)
    cum_pass_att      DOUBLE PRECISION,
    cum_pass_comp     DOUBLE PRECISION,
    cum_pass_yds      DOUBLE PRECISION,
    cum_pass_td       DOUBLE PRECISION,
    cum_pass_int      DOUBLE PRECISION,
    comp_pct          DOUBLE PRECISION,
    ypa               DOUBLE PRECISION,
    td_pct            DOUBLE PRECISION,
    int_pct           DOUBLE PRECISION,
    any_a             DOUBLE PRECISION,
    passer_rating_cum DOUBLE PRECISION,
    cum_rush_att      DOUBLE PRECISION,
    cum_rush_yds      DOUBLE PRECISION,
    cum_rush_td       DOUBLE PRECISION,
    cum_sacks         DOUBLE PRECISION,
    sack_rate         DOUBLE PRECISION,
    cum_fumbles       DOUBLE PRECISION,
    games_played      INTEGER,

    PRIMARY KEY (player_id, season, game_id, game_type)
);
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  QB Rolling Stats DDL  —  3, 5, 10 game windows through current game
# ═══════════════════════════════════════════════════════════════════════════════

CREATE_ROLLING_SQL = """
CREATE TABLE IF NOT EXISTS nfl.qb_rolling_stats (
    player_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    game_id         INTEGER NOT NULL,
    game_type       VARCHAR(10) NOT NULL DEFAULT 'REG',
    week            INTEGER NOT NULL,
    team_abbr       TEXT NOT NULL,
    opponent_abbr   TEXT,
    game_date       DATE,
    starter_flag    BOOLEAN,

    -- Per-game raw events
    pass_attempts      DOUBLE PRECISION DEFAULT 0,
    pass_completions   DOUBLE PRECISION DEFAULT 0,
    pass_yards         DOUBLE PRECISION DEFAULT 0,
    pass_tds           DOUBLE PRECISION DEFAULT 0,
    pass_int           DOUBLE PRECISION DEFAULT 0,
    rush_attempts      DOUBLE PRECISION DEFAULT 0,
    rush_yards         DOUBLE PRECISION DEFAULT 0,
    rush_tds           DOUBLE PRECISION DEFAULT 0,
    sacks              DOUBLE PRECISION DEFAULT 0,
    fumbles            DOUBLE PRECISION DEFAULT 0,

    -- Rolling 3 games (including current)
    pass_att_3         DOUBLE PRECISION,
    pass_comp_3        DOUBLE PRECISION,
    pass_yds_3         DOUBLE PRECISION,
    pass_td_3          DOUBLE PRECISION,
    pass_int_3         DOUBLE PRECISION,
    comp_pct_3         DOUBLE PRECISION,
    ypa_3              DOUBLE PRECISION,
    td_pct_3           DOUBLE PRECISION,
    int_pct_3          DOUBLE PRECISION,
    any_a_3            DOUBLE PRECISION,
    passer_rating_3    DOUBLE PRECISION,
    rush_att_3         DOUBLE PRECISION,
    rush_yds_3         DOUBLE PRECISION,
    rush_td_3          DOUBLE PRECISION,
    sacks_3            DOUBLE PRECISION,
    sack_rate_3        DOUBLE PRECISION,
    fumbles_3          DOUBLE PRECISION,
    games_3            INTEGER,

    -- Rolling 5 games
    pass_att_5         DOUBLE PRECISION,
    pass_comp_5        DOUBLE PRECISION,
    pass_yds_5         DOUBLE PRECISION,
    pass_td_5          DOUBLE PRECISION,
    pass_int_5         DOUBLE PRECISION,
    comp_pct_5         DOUBLE PRECISION,
    ypa_5              DOUBLE PRECISION,
    td_pct_5           DOUBLE PRECISION,
    int_pct_5          DOUBLE PRECISION,
    any_a_5            DOUBLE PRECISION,
    passer_rating_5    DOUBLE PRECISION,
    rush_att_5         DOUBLE PRECISION,
    rush_yds_5         DOUBLE PRECISION,
    rush_td_5          DOUBLE PRECISION,
    sacks_5            DOUBLE PRECISION,
    sack_rate_5        DOUBLE PRECISION,
    fumbles_5          DOUBLE PRECISION,
    games_5            INTEGER,

    -- Rolling 10 games
    pass_att_10        DOUBLE PRECISION,
    pass_comp_10       DOUBLE PRECISION,
    pass_yds_10        DOUBLE PRECISION,
    pass_td_10         DOUBLE PRECISION,
    pass_int_10        DOUBLE PRECISION,
    comp_pct_10        DOUBLE PRECISION,
    ypa_10             DOUBLE PRECISION,
    td_pct_10          DOUBLE PRECISION,
    int_pct_10         DOUBLE PRECISION,
    any_a_10           DOUBLE PRECISION,
    passer_rating_10   DOUBLE PRECISION,
    rush_att_10        DOUBLE PRECISION,
    rush_yds_10        DOUBLE PRECISION,
    rush_td_10         DOUBLE PRECISION,
    sacks_10           DOUBLE PRECISION,
    sack_rate_10       DOUBLE PRECISION,
    fumbles_10         DOUBLE PRECISION,
    games_10           INTEGER,

    PRIMARY KEY (player_id, season, game_id, game_type)
);
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Shared QB data source
# ═══════════════════════════════════════════════════════════════════════════════

# CTE used by both cumulative and rolling populates
QB_SOURCE_CTE = """
WITH qb_games AS (
    SELECT
        pws.player_id,
        s.year       AS season,
        g.id         AS game_id,
        g.week,
        t.abbreviation    AS team_abbr,
        ot.abbreviation   AS opponent_abbr,
        g.date       AS game_date,
        TRUE         AS starter_flag,
        COALESCE(pws.pass_attempts::NUMERIC, 0)   AS pass_att,
        COALESCE(pws.pass_completions::NUMERIC, 0) AS pass_comp,
        COALESCE(pws.pass_yards::NUMERIC, 0)       AS pass_yds,
        COALESCE(pws.pass_tds::NUMERIC, 0)         AS pass_td,
        COALESCE(pws.pass_int::NUMERIC, 0)         AS pass_int,
        COALESCE(pws.rush_attempts::NUMERIC, 0)    AS rush_att,
        COALESCE(pws.rush_yards::NUMERIC, 0)       AS rush_yds,
        COALESCE(pws.rush_tds::NUMERIC, 0)         AS rush_td,
        COALESCE(pws.sacks::NUMERIC, 0)            AS sck,
        COALESCE(pws.fumbles::NUMERIC, 0)          AS fmb,
        g.game_type                                 AS game_type
    FROM nfl.player_weekly_stats pws
    JOIN nfl.games g     ON g.id    = pws.game_id
    JOIN nfl.seasons s   ON s.id    = pws.season_id
    JOIN nfl.teams t     ON t.id    = pws.team_id
    JOIN nfl.teams ot    ON ot.id   = pws.opponent_id
    JOIN nfl.players p   ON p.id    = pws.player_id
    WHERE p.position = 'QB'
      AND pws.game_id IS NOT NULL
      AND s.year IS NOT NULL
      AND g.game_type = :game_type
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Populate QB Cumulative  —  UNBOUNDED PRECEDING TO CURRENT ROW
# ═══════════════════════════════════════════════════════════════════════════════

POPULATE_QB_CUMULATIVE_SQL = QB_SOURCE_CTE + """
INSERT INTO nfl.qb_cumulative_stats (
    player_id, season, game_id, game_type, week, team_abbr, opponent_abbr, game_date, starter_flag,
    pass_attempts, pass_completions, pass_yards, pass_tds, pass_int,
    rush_attempts, rush_yards, rush_tds, sacks, fumbles,
    cum_pass_att, cum_pass_comp, cum_pass_yds, cum_pass_td, cum_pass_int,
    comp_pct, ypa, td_pct, int_pct, any_a, passer_rating_cum,
    cum_rush_att, cum_rush_yds, cum_rush_td, cum_sacks, sack_rate, cum_fumbles,
    games_played
)
SELECT
    player_id, season, game_id, game_type, week, team_abbr, opponent_abbr, game_date, starter_flag,
    pass_att, pass_comp, pass_yds, pass_td, pass_int,
    rush_att, rush_yds, rush_td, sck, fmb,

    -- Cumulative through current game (UNBOUNDED PRECEDING TO CURRENT ROW)
    SUM(pass_att) OVER w_cum       AS cum_pass_att,
    SUM(pass_comp) OVER w_cum      AS cum_pass_comp,
    SUM(pass_yds) OVER w_cum       AS cum_pass_yds,
    SUM(pass_td) OVER w_cum        AS cum_pass_td,
    SUM(pass_int) OVER w_cum       AS cum_pass_int,
    ROUND(SUM(pass_comp) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 100, 2) AS comp_pct,
    ROUND(SUM(pass_yds) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0), 2) AS ypa,
    ROUND(SUM(pass_td) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 100, 2) AS td_pct,
    ROUND(SUM(pass_int) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 100, 2) AS int_pct,
    ROUND((SUM(pass_yds) OVER w_cum + 20 * SUM(pass_td) OVER w_cum - 45 * SUM(pass_int) OVER w_cum)
          / NULLIF(SUM(pass_att) OVER w_cum + SUM(sck) OVER w_cum, 0), 2) AS any_a,
    ROUND(
        CASE WHEN SUM(pass_att) OVER w_cum > 0 THEN (
            GREATEST(0, LEAST(2.375, (SUM(pass_comp) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 100 - 30) / 20))
            + GREATEST(0, LEAST(2.375, (SUM(pass_yds) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) - 3) / 4))
            + GREATEST(0, LEAST(2.375, SUM(pass_td) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 20))
            + GREATEST(0, LEAST(2.375, 2.375 - SUM(pass_int) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum, 0) * 25))
        ) / 6 * 100 ELSE NULL END, 2
    ) AS passer_rating_cum,
    SUM(rush_att) OVER w_cum       AS cum_rush_att,
    SUM(rush_yds) OVER w_cum       AS cum_rush_yds,
    SUM(rush_td) OVER w_cum        AS cum_rush_td,
    SUM(sck) OVER w_cum            AS cum_sacks,
    ROUND(SUM(sck) OVER w_cum / NULLIF(SUM(pass_att) OVER w_cum + SUM(sck) OVER w_cum, 0) * 100, 2) AS sack_rate,
    SUM(fmb) OVER w_cum            AS cum_fumbles,
    COUNT(*) OVER w_cum            AS games_played

FROM qb_games
WINDOW w_cum AS (PARTITION BY player_id, season, game_type ORDER BY game_date, game_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
ORDER BY player_id, season, game_date, game_id
ON CONFLICT (player_id, season, game_id, game_type) DO UPDATE SET
    week            = EXCLUDED.week,
    team_abbr       = EXCLUDED.team_abbr,
    opponent_abbr   = EXCLUDED.opponent_abbr,
    game_date       = EXCLUDED.game_date,
    starter_flag    = EXCLUDED.starter_flag,
    pass_attempts   = EXCLUDED.pass_attempts,
    pass_completions= EXCLUDED.pass_completions,
    pass_yards      = EXCLUDED.pass_yards,
    pass_tds        = EXCLUDED.pass_tds,
    pass_int        = EXCLUDED.pass_int,
    rush_attempts   = EXCLUDED.rush_attempts,
    rush_yards      = EXCLUDED.rush_yards,
    rush_tds        = EXCLUDED.rush_tds,
    sacks           = EXCLUDED.sacks,
    fumbles         = EXCLUDED.fumbles,
    cum_pass_att    = EXCLUDED.cum_pass_att,
    cum_pass_comp   = EXCLUDED.cum_pass_comp,
    cum_pass_yds    = EXCLUDED.cum_pass_yds,
    cum_pass_td     = EXCLUDED.cum_pass_td,
    cum_pass_int    = EXCLUDED.cum_pass_int,
    comp_pct        = EXCLUDED.comp_pct,
    ypa             = EXCLUDED.ypa,
    td_pct          = EXCLUDED.td_pct,
    int_pct         = EXCLUDED.int_pct,
    any_a           = EXCLUDED.any_a,
    passer_rating_cum = EXCLUDED.passer_rating_cum,
    cum_rush_att    = EXCLUDED.cum_rush_att,
    cum_rush_yds    = EXCLUDED.cum_rush_yds,
    cum_rush_td     = EXCLUDED.cum_rush_td,
    cum_sacks       = EXCLUDED.cum_sacks,
    sack_rate       = EXCLUDED.sack_rate,
    cum_fumbles     = EXCLUDED.cum_fumbles,
    games_played    = EXCLUDED.games_played
;
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Populate QB Rolling  —  3, 5, 10 PRECEDING TO CURRENT ROW
# ═══════════════════════════════════════════════════════════════════════════════

POPULATE_QB_ROLLING_SQL = QB_SOURCE_CTE + """
INSERT INTO nfl.qb_rolling_stats (
    player_id, season, game_id, game_type, week, team_abbr, opponent_abbr, game_date, starter_flag,
    pass_attempts, pass_completions, pass_yards, pass_tds, pass_int,
    rush_attempts, rush_yards, rush_tds, sacks, fumbles,
    pass_att_3, pass_comp_3, pass_yds_3, pass_td_3, pass_int_3,
    comp_pct_3, ypa_3, td_pct_3, int_pct_3, any_a_3, passer_rating_3,
    rush_att_3, rush_yds_3, rush_td_3, sacks_3, sack_rate_3, fumbles_3, games_3,
    pass_att_5, pass_comp_5, pass_yds_5, pass_td_5, pass_int_5,
    comp_pct_5, ypa_5, td_pct_5, int_pct_5, any_a_5, passer_rating_5,
    rush_att_5, rush_yds_5, rush_td_5, sacks_5, sack_rate_5, fumbles_5, games_5,
    pass_att_10, pass_comp_10, pass_yds_10, pass_td_10, pass_int_10,
    comp_pct_10, ypa_10, td_pct_10, int_pct_10, any_a_10, passer_rating_10,
    rush_att_10, rush_yds_10, rush_td_10, sacks_10, sack_rate_10, fumbles_10, games_10
)
SELECT
    player_id, season, game_id, game_type, week, team_abbr, opponent_abbr, game_date, starter_flag,
    pass_att, pass_comp, pass_yds, pass_td, pass_int,
    rush_att, rush_yds, rush_td, sck, fmb,

    -- Rolling 3 (including current)
    SUM(pass_att) OVER w3       AS pass_att_3,
    SUM(pass_comp) OVER w3      AS pass_comp_3,
    SUM(pass_yds) OVER w3       AS pass_yds_3,
    SUM(pass_td) OVER w3        AS pass_td_3,
    SUM(pass_int) OVER w3       AS pass_int_3,
    ROUND(SUM(pass_comp) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 100, 2) AS comp_pct_3,
    ROUND(SUM(pass_yds) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0), 2) AS ypa_3,
    ROUND(SUM(pass_td) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 100, 2) AS td_pct_3,
    ROUND(SUM(pass_int) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 100, 2) AS int_pct_3,
    ROUND((SUM(pass_yds) OVER w3 + 20 * SUM(pass_td) OVER w3 - 45 * SUM(pass_int) OVER w3)
          / NULLIF(SUM(pass_att) OVER w3 + SUM(sck) OVER w3, 0), 2) AS any_a_3,
    ROUND(CASE WHEN SUM(pass_att) OVER w3 > 0 THEN (
        GREATEST(0, LEAST(2.375, (SUM(pass_comp) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 100 - 30) / 20))
        + GREATEST(0, LEAST(2.375, (SUM(pass_yds) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) - 3) / 4))
        + GREATEST(0, LEAST(2.375, SUM(pass_td) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 20))
        + GREATEST(0, LEAST(2.375, 2.375 - SUM(pass_int) OVER w3 / NULLIF(SUM(pass_att) OVER w3, 0) * 25))
    ) / 6 * 100 ELSE NULL END, 2) AS passer_rating_3,
    SUM(rush_att) OVER w3       AS rush_att_3,
    SUM(rush_yds) OVER w3       AS rush_yds_3,
    SUM(rush_td) OVER w3        AS rush_td_3,
    SUM(sck) OVER w3            AS sacks_3,
    ROUND(SUM(sck) OVER w3 / NULLIF(SUM(pass_att) OVER w3 + SUM(sck) OVER w3, 0) * 100, 2) AS sack_rate_3,
    SUM(fmb) OVER w3            AS fumbles_3,
    COUNT(*) OVER w3            AS games_3,

    -- Rolling 5
    SUM(pass_att) OVER w5       AS pass_att_5,
    SUM(pass_comp) OVER w5      AS pass_comp_5,
    SUM(pass_yds) OVER w5       AS pass_yds_5,
    SUM(pass_td) OVER w5        AS pass_td_5,
    SUM(pass_int) OVER w5       AS pass_int_5,
    ROUND(SUM(pass_comp) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 100, 2) AS comp_pct_5,
    ROUND(SUM(pass_yds) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0), 2) AS ypa_5,
    ROUND(SUM(pass_td) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 100, 2) AS td_pct_5,
    ROUND(SUM(pass_int) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 100, 2) AS int_pct_5,
    ROUND((SUM(pass_yds) OVER w5 + 20 * SUM(pass_td) OVER w5 - 45 * SUM(pass_int) OVER w5)
          / NULLIF(SUM(pass_att) OVER w5 + SUM(sck) OVER w5, 0), 2) AS any_a_5,
    ROUND(CASE WHEN SUM(pass_att) OVER w5 > 0 THEN (
        GREATEST(0, LEAST(2.375, (SUM(pass_comp) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 100 - 30) / 20))
        + GREATEST(0, LEAST(2.375, (SUM(pass_yds) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) - 3) / 4))
        + GREATEST(0, LEAST(2.375, SUM(pass_td) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 20))
        + GREATEST(0, LEAST(2.375, 2.375 - SUM(pass_int) OVER w5 / NULLIF(SUM(pass_att) OVER w5, 0) * 25))
    ) / 6 * 100 ELSE NULL END, 2) AS passer_rating_5,
    SUM(rush_att) OVER w5       AS rush_att_5,
    SUM(rush_yds) OVER w5       AS rush_yds_5,
    SUM(rush_td) OVER w5        AS rush_td_5,
    SUM(sck) OVER w5            AS sacks_5,
    ROUND(SUM(sck) OVER w5 / NULLIF(SUM(pass_att) OVER w5 + SUM(sck) OVER w5, 0) * 100, 2) AS sack_rate_5,
    SUM(fmb) OVER w5            AS fumbles_5,
    COUNT(*) OVER w5            AS games_5,

    -- Rolling 10
    SUM(pass_att) OVER w10      AS pass_att_10,
    SUM(pass_comp) OVER w10     AS pass_comp_10,
    SUM(pass_yds) OVER w10      AS pass_yds_10,
    SUM(pass_td) OVER w10       AS pass_td_10,
    SUM(pass_int) OVER w10      AS pass_int_10,
    ROUND(SUM(pass_comp) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 100, 2) AS comp_pct_10,
    ROUND(SUM(pass_yds) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0), 2) AS ypa_10,
    ROUND(SUM(pass_td) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 100, 2) AS td_pct_10,
    ROUND(SUM(pass_int) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 100, 2) AS int_pct_10,
    ROUND((SUM(pass_yds) OVER w10 + 20 * SUM(pass_td) OVER w10 - 45 * SUM(pass_int) OVER w10)
          / NULLIF(SUM(pass_att) OVER w10 + SUM(sck) OVER w10, 0), 2) AS any_a_10,
    ROUND(CASE WHEN SUM(pass_att) OVER w10 > 0 THEN (
        GREATEST(0, LEAST(2.375, (SUM(pass_comp) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 100 - 30) / 20))
        + GREATEST(0, LEAST(2.375, (SUM(pass_yds) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) - 3) / 4))
        + GREATEST(0, LEAST(2.375, SUM(pass_td) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 20))
        + GREATEST(0, LEAST(2.375, 2.375 - SUM(pass_int) OVER w10 / NULLIF(SUM(pass_att) OVER w10, 0) * 25))
    ) / 6 * 100 ELSE NULL END, 2) AS passer_rating_10,
    SUM(rush_att) OVER w10      AS rush_att_10,
    SUM(rush_yds) OVER w10      AS rush_yds_10,
    SUM(rush_td) OVER w10       AS rush_td_10,
    SUM(sck) OVER w10           AS sacks_10,
    ROUND(SUM(sck) OVER w10 / NULLIF(SUM(pass_att) OVER w10 + SUM(sck) OVER w10, 0) * 100, 2) AS sack_rate_10,
    SUM(fmb) OVER w10           AS fumbles_10,
    COUNT(*) OVER w10           AS games_10

FROM qb_games
WINDOW
    w3  AS (PARTITION BY player_id, season, game_type ORDER BY game_date, game_id
            ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
    w5  AS (PARTITION BY player_id, season, game_type ORDER BY game_date, game_id
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w10 AS (PARTITION BY player_id, season, game_type ORDER BY game_date, game_id
            ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
ORDER BY player_id, season, game_date, game_id
ON CONFLICT (player_id, season, game_id, game_type) DO UPDATE SET
    week            = EXCLUDED.week,
    team_abbr       = EXCLUDED.team_abbr,
    opponent_abbr   = EXCLUDED.opponent_abbr,
    game_date       = EXCLUDED.game_date,
    starter_flag    = EXCLUDED.starter_flag,
    pass_attempts   = EXCLUDED.pass_attempts,
    pass_completions= EXCLUDED.pass_completions,
    pass_yards      = EXCLUDED.pass_yards,
    pass_tds        = EXCLUDED.pass_tds,
    pass_int        = EXCLUDED.pass_int,
    rush_attempts   = EXCLUDED.rush_attempts,
    rush_yards      = EXCLUDED.rush_yards,
    rush_tds        = EXCLUDED.rush_tds,
    sacks           = EXCLUDED.sacks,
    fumbles         = EXCLUDED.fumbles,
    pass_att_3      = EXCLUDED.pass_att_3,
    pass_comp_3     = EXCLUDED.pass_comp_3,
    pass_yds_3      = EXCLUDED.pass_yds_3,
    pass_td_3       = EXCLUDED.pass_td_3,
    pass_int_3      = EXCLUDED.pass_int_3,
    comp_pct_3      = EXCLUDED.comp_pct_3,
    ypa_3           = EXCLUDED.ypa_3,
    td_pct_3        = EXCLUDED.td_pct_3,
    int_pct_3       = EXCLUDED.int_pct_3,
    any_a_3         = EXCLUDED.any_a_3,
    passer_rating_3 = EXCLUDED.passer_rating_3,
    rush_att_3      = EXCLUDED.rush_att_3,
    rush_yds_3      = EXCLUDED.rush_yds_3,
    rush_td_3       = EXCLUDED.rush_td_3,
    sacks_3         = EXCLUDED.sacks_3,
    sack_rate_3     = EXCLUDED.sack_rate_3,
    fumbles_3       = EXCLUDED.fumbles_3,
    games_3         = EXCLUDED.games_3,
    pass_att_5      = EXCLUDED.pass_att_5,
    pass_comp_5     = EXCLUDED.pass_comp_5,
    pass_yds_5      = EXCLUDED.pass_yds_5,
    pass_td_5       = EXCLUDED.pass_td_5,
    pass_int_5      = EXCLUDED.pass_int_5,
    comp_pct_5      = EXCLUDED.comp_pct_5,
    ypa_5           = EXCLUDED.ypa_5,
    td_pct_5        = EXCLUDED.td_pct_5,
    int_pct_5       = EXCLUDED.int_pct_5,
    any_a_5         = EXCLUDED.any_a_5,
    passer_rating_5 = EXCLUDED.passer_rating_5,
    rush_att_5      = EXCLUDED.rush_att_5,
    rush_yds_5      = EXCLUDED.rush_yds_5,
    rush_td_5       = EXCLUDED.rush_td_5,
    sacks_5         = EXCLUDED.sacks_5,
    sack_rate_5     = EXCLUDED.sack_rate_5,
    fumbles_5       = EXCLUDED.fumbles_5,
    games_5         = EXCLUDED.games_5,
    pass_att_10     = EXCLUDED.pass_att_10,
    pass_comp_10    = EXCLUDED.pass_comp_10,
    pass_yds_10     = EXCLUDED.pass_yds_10,
    pass_td_10      = EXCLUDED.pass_td_10,
    pass_int_10     = EXCLUDED.pass_int_10,
    comp_pct_10     = EXCLUDED.comp_pct_10,
    ypa_10          = EXCLUDED.ypa_10,
    td_pct_10       = EXCLUDED.td_pct_10,
    int_pct_10      = EXCLUDED.int_pct_10,
    any_a_10        = EXCLUDED.any_a_10,
    passer_rating_10= EXCLUDED.passer_rating_10,
    rush_att_10     = EXCLUDED.rush_att_10,
    rush_yds_10     = EXCLUDED.rush_yds_10,
    rush_td_10      = EXCLUDED.rush_td_10,
    sacks_10        = EXCLUDED.sacks_10,
    sack_rate_10    = EXCLUDED.sack_rate_10,
    fumbles_10      = EXCLUDED.fumbles_10,
    games_10        = EXCLUDED.games_10
;
"""

# ═══════════════════════════════════════════════════════════════════════════════


def get_db_url() -> str:
    """Return a sync-style database URL (psycopg2, not asyncpg)."""
    return os.environ.get("SYNC_DATABASE_URL", PSYCOPG2_DATABASE_URL)


def ensure_tables(engine) -> None:
    """Create both QB tables if they don't exist."""
    with engine.begin() as conn:
        conn.execute(text(CREATE_CUMULATIVE_SQL))
        conn.execute(text(CREATE_ROLLING_SQL))
    logger.info("Ensured nfl.qb_cumulative_stats and nfl.qb_rolling_stats tables exist")


def populate_qb_tables(
    engine=None,
    seasons: list[int] | None = None,
    game_type: str = "REG",
) -> dict:
    """Populate both nfl.qb_cumulative_stats and nfl.qb_rolling_stats.

    Args:
        engine: SQLAlchemy sync engine. If None, creates one.
        seasons: List of seasons to process. None = all available.
        game_type: Which game_type to compute (REG|PRE|POST). Default REG.

    Returns:
        dict with row counts for both tables.
    """
    if engine is None:
        url = get_db_url()
        engine = create_engine(url, pool_pre_ping=True)
        _owns_engine = True
    else:
        _owns_engine = False

    result = {"cumulative": 0, "rolling": 0}

    try:
        ensure_tables(engine)

        for table_key, sql in [("cumulative", POPULATE_QB_CUMULATIVE_SQL),
                                ("rolling", POPULATE_QB_ROLLING_SQL)]:
            sql_to_run = sql
            if seasons:
                season_list = ", ".join(str(s) for s in seasons)
                sql_to_run = sql_to_run.replace(
                    "FROM qb_games",
                    f"FROM qb_games WHERE season IN ({season_list})",
                )

            with engine.begin() as conn:
                logger.info("Running QB %s stats population (game_type=%s)...", table_key, game_type)
                r = conn.execute(text(sql_to_run), {"game_type": game_type})
                result[table_key] = r.rowcount
                logger.info("Inserted/updated %d QB %s stat rows", r.rowcount, table_key)

        return result
    finally:
        if _owns_engine:
            engine.dispose()


def main() -> None:
    """CLI entry point."""
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--game-type", default="REG", choices=["REG", "PRE", "POST"],
                     help="Which game_type to compute QB stats for (default REG)")
    _args = _ap.parse_args()

    result = populate_qb_tables(game_type=_args.game_type)
    logger.info("Done — cumulative: %d, rolling: %d", result["cumulative"], result["rolling"])


if __name__ == "__main__":
    main()
