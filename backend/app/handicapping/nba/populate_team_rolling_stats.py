"""
populate_team_rolling_stats.py
==============================

Populates the NEW table ``nba.team_rolling_stats`` -- the SINGLE SOURCE OF
TRUTH for all ROLLING team stats consumed by the NBA data loader.

Two rows per game (home + away).  Column names exactly match ``nba.features``
rolling feature names with the leading ``a_``/``h_`` prefix stripped; the row's
``team_side`` disambiguates home vs away.

CRITICAL -- INCLUSIVE windows
-----------------------------
Every rolling window INCLUDES the current game (CURRENT ROW), i.e. for a team's
games ordered by ``(game_date, game_id)`` within a season, ``net_rtg_r5`` is the
average of that team's per-game net rating over its last 5 games *including the
current one*.  The data_loader fetches the *previous completed game's* row, so
publishing the current row's value is the correct contract with the loader.

Per-game base values (no cumulative carry-over):
    * per_game_net       = net rating per 100 possessions = ortg - drtg
    * per_game_efg       = (fgm + 0.5*fgm3) / fga
    * per_game_ast_ratio = ast / fgm                       (matches "AST/FGM")
    * per_game_ft_rate   = fta / fga                       (matches "FTA/FGA")
    * per_game_threep_rate = fga3 / fga
    * per_game_ortg / drtg / pace = derived from ESTIMATED possessions.
      nba.games carries no real possessions / offensive-rebound columns, so the
      exact estimation used is:
          oreb_est    = reb        * 0.245
          opp_oreb_est= opp_reb    * 0.245
          poss        = fga - oreb_est    + 0.44*fta
          opp_poss    = opp_fga - opp_oreb_est + 0.44*opp_fta
          ortg = 100*points/poss ; drtg = 100*points_allowed/opp_poss
          pace = (poss + opp_poss)/2
      (TOV intentionally omitted so the derivation matches the rest of the tooling
       that has historically excluded turnovers from possessions.)

Weighted rw3/rw5 semantics (INCLUSIVE, current row gets the largest weight):
    rw3: weights 0.5 / 0.3 / 0.2   over the last 3 games (current = 0.5)
    rw5: weights 0.3 / 0.25 /0.2 /0.15 /0.1  over the last 5 games (current = 0.3)
    COALESCE fallback => simple AVG over the partial window when fewer games exist.

Volatility is population stddev over the last N games inclusive:
    cv10_net_rtg, cv10_ppg over 10; cv20_ppg over 20.

Recency == the rw3-style 0.5/0.3/0.2 weighted value over the last 3 games inclusive
    (recency_net_rtg, recency_ppg).

Opponent-adjusted:
    adj_off_10 = team per-game ORTG L10  - league rolling mean per-game ORTG L10
    adj_def_10 = team per-game DRTG L10  - league rolling mean per-game DRTG L10
    (league means are computed over ALL team-games ordered globally by
     ``(game_date, game_id)``, window of the most recent 10 team-games.)

Star:
    star_ppg_5   = sum over the team's top-3 season scorers of each player's
                   per-player rolling 5-game PPG (inclusive)
    star1_ppg_5  = the rank-1 scorer's per-player rolling 5-game PPG (inclusive)
    Top-3 scorers per (team, season): rank player_season_stats by points_per_game
    DESC with games_played >= 10; source per-game points in player_game_stats.

Table is keyed uniquely on ``(game_id, team_side)`` -- the script UPSERTs on that
key, so it is idempotent and safe to re-run.

Run:
    python -m app.handicapping.nba.populate_team_rolling_stats        # incremental
    python -m app.handicapping.nba.populate_team_rolling_stats --full # full rebuild
"""

from __future__ import annotations

import argparse
import logging
import os

from sqlalchemy import create_engine, text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB / engine helpers (mirrors populate_rolling_stats.py)
# ---------------------------------------------------------------------------
def get_db_url() -> str:
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    user = os.environ.get("DB_USER", "earl")
    pw = os.environ.get("DB_PASS", "earl_dev_pass")
    name = os.environ.get("DB_NAME", "earl_knows_football")
    return f"postgresql+psycopg2://{user}:{pw}@{host}:{port}/{name}"


# ---------------------------------------------------------------------------
# Table DDL (created if missing)
# ---------------------------------------------------------------------------
ENSURE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS nba.team_rolling_stats (
    game_id                       INTEGER,
    team_id                       INTEGER,
    team_side                     TEXT,
    season_id                     INTEGER,
    game_date                     DATE,
    -- simple rolling r5/r10 (avg incl current)
    net_rtg_r5                    DOUBLE PRECISION,
    net_rtg_r10                   DOUBLE PRECISION,
    ortg_r5                       DOUBLE PRECISION,
    ortg_r10                      DOUBLE PRECISION,
    drtg_r5                       DOUBLE PRECISION,
    drtg_r10                      DOUBLE PRECISION,
    efg_r5                        DOUBLE PRECISION,
    efg_r10                       DOUBLE PRECISION,
    pace_r5                       DOUBLE PRECISION,
    pace_r10                      DOUBLE PRECISION,
    ast_ratio_r5                  DOUBLE PRECISION,
    ast_ratio_r10                 DOUBLE PRECISION,
    ft_rate_r5                    DOUBLE PRECISION,
    ft_rate_r10                   DOUBLE PRECISION,
    threep_rate_r5                DOUBLE PRECISION,
    threep_rate_r10               DOUBLE PRECISION,
    -- form
    ats_margin_5                  DOUBLE PRECISION,
    ats_margin_10                 DOUBLE PRECISION,
    ats_wins_5                    DOUBLE PRECISION,
    ats_wins_10                   DOUBLE PRECISION,
    ou_wins_5                     DOUBLE PRECISION,
    ou_wins_10                    DOUBLE PRECISION,
    ou_margin_5                   DOUBLE PRECISION,
    wins_5                        DOUBLE PRECISION,
    wins_10                       DOUBLE PRECISION,
    -- weighted
    rw3_ppg                       DOUBLE PRECISION,
    rw3_net_rtg                   DOUBLE PRECISION,
    rw3_drtg                      DOUBLE PRECISION,
    rw3_efg_pct                   DOUBLE PRECISION,
    rw5_ppg                       DOUBLE PRECISION,
    rw5_net_rtg                   DOUBLE PRECISION,
    rw5_drtg                      DOUBLE PRECISION,
    rw5_efg_pct                   DOUBLE PRECISION,
    -- volatility
    cv10_net_rtg                  DOUBLE PRECISION,
    cv10_ppg                      DOUBLE PRECISION,
    cv20_ppg                      DOUBLE PRECISION,
    -- recency
    recency_net_rtg               DOUBLE PRECISION,
    recency_ppg                   DOUBLE PRECISION,
    -- opponent-adjusted
    adj_off_10                    DOUBLE PRECISION,
    adj_def_10                    DOUBLE PRECISION,
    -- star
    star_ppg_5                    DOUBLE PRECISION,
    star1_ppg_5                   DOUBLE PRECISION,
    PRIMARY KEY (game_id, team_side)
);
CREATE INDEX IF NOT EXISTS idx_team_rolling_stats_game
    ON nba.team_rolling_stats (game_id);
CREATE INDEX IF NOT EXISTS idx_team_rolling_stats_team
    ON nba.team_rolling_stats (team_id, season_id, game_date);
"""


# ---------------------------------------------------------------------------
# Main population query (per-game base -> inclusive rolling -> star join)
# ---------------------------------------------------------------------------
POPULATE_TEAM_ROLLING_SQL = """
WITH

-- Per-game table of raw values, unpivoted to one row per (game_id, team, side).
base AS (
    SELECT
        g.id                               AS game_id,
        g.season_id                        AS season_id,
        g.date                             AS game_date,
        'home'                             AS team_side,
        g.home_team_id                     AS team_id,
        g.home_score                       AS points,
        g.away_score                       AS points_allowed,
        g.home_field_goals_made            AS fgm,
        g.home_field_goals_attempted       AS fga,
        g.home_three_points_made           AS fgm3,
        g.home_three_points_attempted      AS fga3,
        g.home_free_throws_made            AS ftm,
        g.home_free_throws_attempted       AS fta,
        g.home_rebounds                    AS reb,
        g.home_assists                     AS ast,
        g.home_steals                      AS stl,
        g.home_blocks                      AS blk,
        g.home_turnovers                   AS tov,
        g.home_fouls                       AS pf,
        g.away_field_goals_attempted       AS opp_fga,
        g.away_field_goals_made            AS opp_fgm,
        g.away_three_points_attempted      AS opp_fga3,
        g.away_free_throws_attempted       AS opp_fta,
        g.away_rebounds                   AS opp_reb,
        g.away_turnovers                   AS opp_tov,
        g.away_score                       AS opp_score,
        COALESCE(blc.closing_spread, blc.opening_spread) AS closing_spread,
        COALESCE(blc.closing_ou, blc.opening_ou)         AS closing_ou,
        -- won
        CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END AS won
    FROM nba.games g
    LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
    WHERE g.home_team_id IS NOT NULL AND g.away_team_id IS NOT NULL

    UNION ALL

    SELECT
        g.id                               AS game_id,
        g.season_id                        AS season_id,
        g.date                             AS game_date,
        'away'                             AS team_side,
        g.away_team_id                     AS team_id,
        g.away_score                       AS points,
        g.home_score                       AS points_allowed,
        g.away_field_goals_made            AS fgm,
        g.away_field_goals_attempted       AS fga,
        g.away_three_points_made           AS fgm3,
        g.away_three_points_attempted      AS fga3,
        g.away_free_throws_made            AS ftm,
        g.away_free_throws_attempted       AS fta,
        g.away_rebounds                    AS reb,
        g.away_assists                     AS ast,
        g.away_steals                      AS stl,
        g.away_blocks                      AS blk,
        g.away_turnovers                   AS tov,
        g.away_fouls                       AS pf,
        g.home_field_goals_attempted       AS opp_fga,
        g.home_field_goals_made            AS opp_fgm,
        g.home_three_points_attempted      AS opp_fga3,
        g.home_free_throws_attempted       AS opp_fta,
        g.home_rebounds                   AS opp_reb,
        g.home_turnovers                   AS opp_tov,
        g.home_score                       AS opp_score,
        COALESCE(blc.closing_spread, blc.opening_spread) AS closing_spread,
        COALESCE(blc.closing_ou, blc.opening_ou)         AS closing_ou,
        -- won
        CASE WHEN g.away_score > g.home_score THEN 1 ELSE 0 END AS won
    FROM nba.games g
    LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
    WHERE g.home_team_id IS NOT NULL AND g.away_team_id IS NOT NULL
),

pg_poss AS (
    SELECT
        b.game_id, b.team_id, b.team_side, b.season_id, b.game_date,
        b.points, b.points_allowed,
        b.fgm, b.fga, b.fgm3, b.fga3, b.ftm, b.fta, b.ast,
        b.stl, b.blk, b.tov, b.pf,
        b.won,
        -- ATS / OU (derived from box result + closing line, per team_side)
        CASE WHEN b.closing_spread IS NULL THEN NULL
             WHEN b.team_side = 'home'
                  THEN (b.points - b.points_allowed) + b.closing_spread
             ELSE (b.points - b.points_allowed) - b.closing_spread
        END AS ats_margin,
        CASE WHEN b.closing_spread IS NULL THEN NULL
             WHEN b.team_side = 'home'
                  THEN CASE WHEN (b.points - b.points_allowed) + b.closing_spread > 0 THEN 1
                            WHEN (b.points - b.points_allowed) + b.closing_spread < 0 THEN 0
                            ELSE NULL END
             ELSE CASE WHEN (b.points - b.points_allowed) - b.closing_spread > 0 THEN 1
                       WHEN (b.points - b.points_allowed) - b.closing_spread < 0 THEN 0
                       ELSE NULL END
        END AS ats_won,
        CASE WHEN b.closing_ou IS NULL THEN NULL
             ELSE (b.points + b.points_allowed) - b.closing_ou
        END AS ou_margin,
        CASE WHEN b.closing_ou IS NULL THEN NULL
             WHEN b.points + b.points_allowed > b.closing_ou THEN 1
             WHEN b.points + b.points_allowed < b.closing_ou THEN 0
             ELSE NULL END AS ou_won,
        -- per-game possessions (exact formula):
        --   poss   = fga - oreb_est + 0.44*fta   (oreb_est = reb*0.245; TOV omitted)
        --   opp_pos = opp_fga - opp_oreb_est + 0.44*opp_fta
        CASE WHEN (b.fga IS NULL OR b.fta IS NULL OR b.reb IS NULL) THEN NULL
             ELSE b.fga - b.reb * 0.245 + 0.44 * b.fta END              AS poss,
        CASE WHEN (b.opp_fga IS NULL OR b.opp_fta IS NULL OR b.opp_reb IS NULL) THEN NULL
             ELSE b.opp_fga - b.opp_reb * 0.245 + 0.44 * b.opp_fta END  AS opp_poss
    FROM base b
),
pg AS (
    SELECT
        q.game_id, q.team_id, q.team_side, q.season_id, q.game_date,
        q.points, q.points_allowed,
        q.fgm, q.fga, q.fgm3, q.fga3, q.ftm, q.fta, q.ast,
        q.stl, q.blk, q.tov, q.pf,
        q.ats_margin, q.ats_won, q.ou_won, q.ou_margin, q.won,
        q.poss, q.opp_poss,
        -- per-game base rates (guarded against divide-by-zero)
        -- per_game_net = NET RATING per 100 possessions (ortg - drtg)
        CASE WHEN (q.poss IS NULL OR q.poss <= 0 OR q.opp_poss IS NULL OR q.opp_poss <= 0)
             THEN NULL
             ELSE 100.0 * q.points / q.poss - 100.0 * q.points_allowed / q.opp_poss END AS per_game_net,
        CASE WHEN NULLIF(q.fga, 0) IS NULL OR q.fga = 0 THEN NULL
             ELSE (q.fgm + 0.5 * q.fgm3) / NULLIF(q.fga, 0) END              AS per_game_efg,
        CASE WHEN q.fgm = 0 THEN NULL
             ELSE q.ast::float / NULLIF(q.fgm, 0) END                        AS per_game_ast_ratio,
        CASE WHEN NULLIF(q.fga, 0) IS NULL OR q.fga = 0 THEN NULL
             ELSE q.fta::float / NULLIF(q.fga, 0) END                        AS per_game_ft_rate,
        CASE WHEN q.fga3 = 0 THEN NULL
             ELSE q.fga3::float / NULLIF(q.fga, 0) END                       AS per_game_threep_rate,
        -- ORTG / DRTG (points per 100 own / opponent possessions)
        CASE WHEN q.poss IS NULL OR q.poss <= 0 THEN NULL
             ELSE 100.0 * q.points / q.poss END                          AS per_game_ortg,
        CASE WHEN q.opp_poss IS NULL OR q.opp_poss <= 0 THEN NULL
             ELSE 100.0 * q.points_allowed / q.opp_poss END              AS per_game_drtg
    FROM pg_poss q
),

rolling AS (
    SELECT
        p.game_id, p.team_id, p.team_side, p.season_id, p.game_date,
        -- simple rolling r5/r10 (avg incl current)
        AVG(p.per_game_net)    OVER w5  AS net_rtg_r5,
        AVG(p.per_game_net)    OVER w10 AS net_rtg_r10,
        AVG(p.per_game_ortg)   OVER w5  AS ortg_r5,
        AVG(p.per_game_ortg)   OVER w10 AS ortg_r10,
        AVG(p.per_game_drtg)   OVER w5  AS drtg_r5,
        AVG(p.per_game_drtg)   OVER w10 AS drtg_r10,
        AVG(p.per_game_efg)    OVER w5  AS efg_r5,
        AVG(p.per_game_efg)    OVER w10 AS efg_r10,
        AVG((p.poss + p.opp_poss) / 2.0) OVER w5  AS pace_r5,
        AVG((p.poss + p.opp_poss) / 2.0) OVER w10 AS pace_r10,
        AVG(p.per_game_ast_ratio) OVER w5  AS ast_ratio_r5,
        AVG(p.per_game_ast_ratio) OVER w10 AS ast_ratio_r10,
        AVG(p.per_game_ft_rate)   OVER w5  AS ft_rate_r5,
        AVG(p.per_game_ft_rate)   OVER w10 AS ft_rate_r10,
        AVG(p.per_game_threep_rate) OVER w5  AS threep_rate_r5,
        AVG(p.per_game_threep_rate) OVER w10 AS threep_rate_r10,
        -- form (avg/count incl current)
        AVG(p.ats_margin) OVER w5  AS ats_margin_5,
        AVG(p.ats_margin) OVER w10 AS ats_margin_10,
        COALESCE(SUM(p.ats_won) OVER w5,  0) AS ats_wins_5,
        COALESCE(SUM(p.ats_won) OVER w10, 0) AS ats_wins_10,
        COALESCE(SUM(p.ou_won)  OVER w5,  0) AS ou_wins_5,
        COALESCE(SUM(p.ou_won)  OVER w10, 0) AS ou_wins_10,
        AVG(p.ou_margin) OVER w5  AS ou_margin_5,
        COALESCE(SUM(p.won) OVER w5)  AS wins_5,
        COALESCE(SUM(p.won) OVER w10) AS wins_10,
        -- weighted rw3 (incl current = 0.5) w/ AVG fallback on partial windows
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.points
                + 0.3 * LAG(p.points, 1) OVER w3ord
                + 0.2 * LAG(p.points, 2) OVER w3ord
             ELSE AVG(p.points) OVER w3 END              AS rw3_ppg,
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.per_game_net
                + 0.3 * LAG(p.per_game_net, 1) OVER w3ord
                + 0.2 * LAG(p.per_game_net, 2) OVER w3ord
             ELSE AVG(p.per_game_net) OVER w3 END        AS rw3_net_rtg,
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.per_game_drtg
                + 0.3 * LAG(p.per_game_drtg, 1) OVER w3ord
                + 0.2 * LAG(p.per_game_drtg, 2) OVER w3ord
             ELSE AVG(p.per_game_drtg) OVER w3 END       AS rw3_drtg,
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.per_game_efg
                + 0.3 * LAG(p.per_game_efg, 1) OVER w3ord
                + 0.2 * LAG(p.per_game_efg, 2) OVER w3ord
             ELSE AVG(p.per_game_efg) OVER w3 END        AS rw3_efg_pct,
        -- weighted rw5 (incl current = 0.3) w/ AVG fallback
        CASE WHEN COUNT(*) OVER w5 >= 5
             THEN 0.3 * p.points
                + 0.25 * LAG(p.points, 1) OVER w5ord
                + 0.2  * LAG(p.points, 2) OVER w5ord
                + 0.15 * LAG(p.points, 3) OVER w5ord
                + 0.1  * LAG(p.points, 4) OVER w5ord
             ELSE AVG(p.points) OVER w5 END              AS rw5_ppg,
        CASE WHEN COUNT(*) OVER w5 >= 5
             THEN 0.3 * p.per_game_net
                + 0.25 * LAG(p.per_game_net, 1) OVER w5ord
                + 0.2  * LAG(p.per_game_net, 2) OVER w5ord
                + 0.15 * LAG(p.per_game_net, 3) OVER w5ord
                + 0.1  * LAG(p.per_game_net, 4) OVER w5ord
             ELSE AVG(p.per_game_net) OVER w5 END        AS rw5_net_rtg,
        CASE WHEN COUNT(*) OVER w5 >= 5
             THEN 0.3 * p.per_game_drtg
                + 0.25 * LAG(p.per_game_drtg, 1) OVER w5ord
                + 0.2  * LAG(p.per_game_drtg, 2) OVER w5ord
                + 0.15 * LAG(p.per_game_drtg, 3) OVER w5ord
                + 0.1  * LAG(p.per_game_drtg, 4) OVER w5ord
             ELSE AVG(p.per_game_drtg) OVER w5 END       AS rw5_drtg,
        CASE WHEN COUNT(*) OVER w5 >= 5
             THEN 0.3 * p.per_game_efg
                + 0.25 * LAG(p.per_game_efg, 1) OVER w5ord
                + 0.2  * LAG(p.per_game_efg, 2) OVER w5ord
                + 0.15 * LAG(p.per_game_efg, 3) OVER w5ord
                + 0.1  * LAG(p.per_game_efg, 4) OVER w5ord
             ELSE AVG(p.per_game_efg) OVER w5 END        AS rw5_efg_pct,
        -- volatility (population stddev over last N incl current)
        STDDEV_POP(p.per_game_net) OVER w10 AS cv10_net_rtg,
        STDDEV_POP(p.points)       OVER w10 AS cv10_ppg,
        STDDEV_POP(p.points)       OVER w20 AS cv20_ppg,
        -- recency = rw3-style 0.5/0.3/0.2 over last 3 incl current
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.per_game_net
                + 0.3 * LAG(p.per_game_net, 1) OVER w3ord
                + 0.2 * LAG(p.per_game_net, 2) OVER w3ord
             ELSE AVG(p.per_game_net) OVER w3 END        AS recency_net_rtg,
        CASE WHEN COUNT(*) OVER w3 >= 3
             THEN 0.5 * p.points
                + 0.3 * LAG(p.points, 1) OVER w3ord
                + 0.2 * LAG(p.points, 2) OVER w3ord
             ELSE AVG(p.points) OVER w3 END              AS recency_ppg,
        -- opponent-adjusted L10 (vs league rolling mean of per-game ortg/drtg)
        AVG(p.per_game_ortg) OVER w10 - AVG(p.per_game_ortg) OVER wlg10 AS adj_off_10,
        AVG(p.per_game_drtg) OVER w10 - AVG(p.per_game_drtg) OVER wlg10 AS adj_def_10,
        -- row index for star-join matching
        p.points AS points
    FROM pg p
    WINDOW
        w5   AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id
                 ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
        w10  AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id
                 ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
        w20  AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id
                 ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
        w3   AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id
                 ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
        w3ord  AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id),
        w5ord  AS (PARTITION BY p.team_id, p.season_id ORDER BY p.game_date, p.game_id),
        wlg10  AS (ORDER BY p.game_date, p.game_id
                   ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
),

-- Top-3 scorers per (team, season) by season PPG (games_played >= 10)
star_prep AS (
    SELECT player_id, team_id, season_id, rk
    FROM (
        SELECT player_id, team_id, season_id,
               ROW_NUMBER() OVER (
                   PARTITION BY team_id, season_id
                   ORDER BY points_per_game DESC, player_id
               ) AS rk
        FROM nba.player_season_stats
        WHERE games_played >= 10
    ) s
    WHERE rk <= 3
),
-- per-player rolling 5-game PPG (incl current) BUT only the rank-1 scorer's
-- independent rolling value, and the SUM of top-3 rolling values per game.
star_rolling AS (
    SELECT
        sp.team_id, sp.season_id, sp.rk,
        pgs.game_id, g.date AS game_date,
        AVG(pgs.points) OVER (
            PARTITION BY sp.player_id, sp.season_id
            ORDER BY g.date, pgs.game_id
            ROWS BETWEEN 4 PRECEDING AND CURRENT ROW
        ) AS ppg_r5
    FROM star_prep sp
    JOIN nba.player_game_stats pgs
        ON pgs.player_id = sp.player_id
        AND pgs.team_id   = sp.team_id
    JOIN nba.games g ON g.id = pgs.game_id
    WHERE g.season_id = sp.season_id
),
star_agg AS (
    SELECT game_id, team_id, season_id,
           MAX(CASE WHEN rk = 1 THEN ppg_r5 END) AS star1_ppg_5,
           SUM(ppg_r5)                           AS star_ppg_5
    FROM star_rolling
    GROUP BY game_id, team_id, season_id
)

-- Final upsert
INSERT INTO nba.team_rolling_stats (
    game_id, team_id, team_side, season_id, game_date,
    net_rtg_r5, net_rtg_r10, ortg_r5, ortg_r10, drtg_r5, drtg_r10,
    efg_r5, efg_r10, pace_r5, pace_r10, ast_ratio_r5, ast_ratio_r10,
    ft_rate_r5, ft_rate_r10, threep_rate_r5, threep_rate_r10,
    ats_margin_5, ats_margin_10, ats_wins_5, ats_wins_10, ou_wins_5, ou_wins_10,
    ou_margin_5, wins_5, wins_10,
    rw3_ppg, rw3_net_rtg, rw3_drtg, rw3_efg_pct,
    rw5_ppg, rw5_net_rtg, rw5_drtg, rw5_efg_pct,
    cv10_net_rtg, cv10_ppg, cv20_ppg,
    recency_net_rtg, recency_ppg,
    adj_off_10, adj_def_10,
    star_ppg_5, star1_ppg_5
)
SELECT
    r.game_id, r.team_id, r.team_side, r.season_id, r.game_date,
    r.net_rtg_r5, r.net_rtg_r10, r.ortg_r5, r.ortg_r10, r.drtg_r5, r.drtg_r10,
    r.efg_r5, r.efg_r10, r.pace_r5, r.pace_r10, r.ast_ratio_r5, r.ast_ratio_r10,
    r.ft_rate_r5, r.ft_rate_r10, r.threep_rate_r5, r.threep_rate_r10,
    r.ats_margin_5, r.ats_margin_10, r.ats_wins_5, r.ats_wins_10,
    r.ou_wins_5, r.ou_wins_10, r.ou_margin_5, r.wins_5, r.wins_10,
    r.rw3_ppg, r.rw3_net_rtg, r.rw3_drtg, r.rw3_efg_pct,
    r.rw5_ppg, r.rw5_net_rtg, r.rw5_drtg, r.rw5_efg_pct,
    r.cv10_net_rtg, r.cv10_ppg, r.cv20_ppg,
    r.recency_net_rtg, r.recency_ppg,
    r.adj_off_10, r.adj_def_10,
    sa.star_ppg_5, sa.star1_ppg_5
FROM rolling r
LEFT JOIN star_agg sa
    ON sa.game_id = r.game_id AND sa.team_id = r.team_id AND sa.season_id = r.season_id
ON CONFLICT (game_id, team_side)
DO UPDATE SET
    team_id          = EXCLUDED.team_id,
    season_id        = EXCLUDED.season_id,
    game_date        = EXCLUDED.game_date,
    net_rtg_r5       = EXCLUDED.net_rtg_r5,
    net_rtg_r10      = EXCLUDED.net_rtg_r10,
    ortg_r5          = EXCLUDED.ortg_r5,
    ortg_r10         = EXCLUDED.ortg_r10,
    drtg_r5          = EXCLUDED.drtg_r5,
    drtg_r10         = EXCLUDED.drtg_r10,
    efg_r5           = EXCLUDED.efg_r5,
    efg_r10          = EXCLUDED.efg_r10,
    pace_r5          = EXCLUDED.pace_r5,
    pace_r10         = EXCLUDED.pace_r10,
    ast_ratio_r5     = EXCLUDED.ast_ratio_r5,
    ast_ratio_r10    = EXCLUDED.ast_ratio_r10,
    ft_rate_r5       = EXCLUDED.ft_rate_r5,
    ft_rate_r10      = EXCLUDED.ft_rate_r10,
    threep_rate_r5   = EXCLUDED.threep_rate_r5,
    threep_rate_r10  = EXCLUDED.threep_rate_r10,
    ats_margin_5     = EXCLUDED.ats_margin_5,
    ats_margin_10    = EXCLUDED.ats_margin_10,
    ats_wins_5       = EXCLUDED.ats_wins_5,
    ats_wins_10      = EXCLUDED.ats_wins_10,
    ou_wins_5        = EXCLUDED.ou_wins_5,
    ou_wins_10       = EXCLUDED.ou_wins_10,
    ou_margin_5      = EXCLUDED.ou_margin_5,
    wins_5           = EXCLUDED.wins_5,
    wins_10          = EXCLUDED.wins_10,
    rw3_ppg          = EXCLUDED.rw3_ppg,
    rw3_net_rtg      = EXCLUDED.rw3_net_rtg,
    rw3_drtg         = EXCLUDED.rw3_drtg,
    rw3_efg_pct      = EXCLUDED.rw3_efg_pct,
    rw5_ppg          = EXCLUDED.rw5_ppg,
    rw5_net_rtg      = EXCLUDED.rw5_net_rtg,
    rw5_drtg         = EXCLUDED.rw5_drtg,
    rw5_efg_pct      = EXCLUDED.rw5_efg_pct,
    cv10_net_rtg     = EXCLUDED.cv10_net_rtg,
    cv10_ppg         = EXCLUDED.cv10_ppg,
    cv20_ppg         = EXCLUDED.cv20_ppg,
    recency_net_rtg  = EXCLUDED.recency_net_rtg,
    recency_ppg      = EXCLUDED.recency_ppg,
    adj_off_10       = EXCLUDED.adj_off_10,
    adj_def_10       = EXCLUDED.adj_def_10,
    star_ppg_5       = EXCLUDED.star_ppg_5,
    star1_ppg_5      = EXCLUDED.star1_ppg_5
"""


# ---------------------------------------------------------------------------
# Population driver
# ---------------------------------------------------------------------------
def ensure_tables(engine) -> None:
    logger.info("Ensuring table nba.team_rolling_stats exists")
    with engine.begin() as conn:
        conn.execute(text(ENSURE_TABLES_SQL))


def populate_team_rolling(engine, incremental: bool = False) -> int:
    """Populate nba.team_rolling_stats. Returns number of rows written."""
    ensure_tables(engine)

    if not incremental:
        # Full rebuild: wipe the table first, then populate fresh.
        logger.info("--full: truncating nba.team_rolling_stats")
        with engine.begin() as conn:
            conn.execute(text("TRUNCATE TABLE nba.team_rolling_stats"))

    logger.info("Running team_rolling_stats population ...")
    with engine.begin() as conn:
        res = conn.execute(text(POPULATE_TEAM_ROLLING_SQL))
        written = res.rowcount
        logger.info("Wrote %d rows", written)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true",
        help="Truncate and fully rebuild the table (default: upsert incrementally)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    engine = create_engine(get_db_url(), pool_pre_ping=True)
    try:
        written = populate_team_rolling(engine, incremental=not args.full)
        print(f"Done. {written} rows upserted into nba.team_rolling_stats.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
