"""Populate nfl.team_rolling_stats -- rolling window averages & performance metrics.

Pattern follows mlb.populate_rolling_stats:
1. Build per-game stats from nfl.cumulative_game_stats (diff cumulative -> per-game)
2. Compute rolling window averages (AVG over 3/5/10-game windows, including current game)
3. Compute season-to-date records and streaks (including current game)
4. Insert into nfl.team_rolling_stats

All rolling windows include the current game (ROWS BETWEEN N-1 PRECEDING AND CURRENT ROW).
The data loader processes completed games only, so each row reflects its own contribution.
"""

import logging

from sqlalchemy import text
from app.database import SessionLocal

logger = logging.getLogger(__name__)


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nfl.team_rolling_stats (
    game_id          INTEGER NOT NULL,
    team_abbr        VARCHAR(3) NOT NULL,
    season           INTEGER NOT NULL,
    game_type        VARCHAR(10) NOT NULL DEFAULT 'REG',
    week             INTEGER NOT NULL,
    game_date        DATE,
    is_home          BOOLEAN,
    games_played     INTEGER,
    feeds_into_game_id INTEGER,

    -- Offensive rolling: points & yards
    off_pts_r3       REAL,
    off_pts_r5       REAL,
    off_pts_r10      REAL,
    off_yds_r3       REAL,
    off_yds_r5       REAL,
    off_yds_r10      REAL,
    pass_yds_r3      REAL,
    pass_yds_r5      REAL,
    pass_yds_r10     REAL,
    rush_yds_r3      REAL,
    rush_yds_r5      REAL,
    rush_yds_r10     REAL,

    -- Offensive rolling: efficiency
    ypp_r3           REAL,
    ypp_r5           REAL,
    ypp_r10          REAL,
    pass_ypa_r3      REAL,
    pass_ypa_r5      REAL,
    pass_ypa_r10     REAL,
    rush_ypa_r3      REAL,
    rush_ypa_r5      REAL,
    rush_ypa_r10     REAL,
    cmp_pct_r3       REAL,
    cmp_pct_r5       REAL,
    cmp_pct_r10      REAL,

    -- Offensive rolling: meta
    first_downs_r3       REAL,
    first_downs_r5       REAL,
    third_down_pct_r3    REAL,
    third_down_pct_r5    REAL,
    rz_td_pct_r3         REAL,
    rz_td_pct_r5         REAL,
    epa_per_play_r3      REAL,
    epa_per_play_r5      REAL,
    explosive_rate_r3    REAL,
    explosive_rate_r5    REAL,
    three_and_out_rate_r3 REAL,
    three_and_out_rate_r5 REAL,
    pass_att_r3          REAL,
    pass_att_r5          REAL,
    rush_att_r3          REAL,
    rush_att_r5          REAL,
    rush_td_r3           REAL,
    rush_td_r5           REAL,
    fumbles_r3           REAL,
    fumbles_r5           REAL,
    fourth_down_pct_r3   REAL,
    fourth_down_pct_r5   REAL,
    off_pts_stddev_r5    REAL,
    off_yds_stddev_r5    REAL,
    opp_pts_stddev_r5    REAL,
    opp_yds_stddev_r5    REAL,

    -- Season ranks
    off_yardage_rank     INTEGER,
    def_yardage_rank     INTEGER,
    off_scoring_rank     INTEGER,
    def_scoring_rank     INTEGER,
    off_rushing_rank     INTEGER,
    def_rushing_rank     INTEGER,
    off_passing_rank     INTEGER,
    def_passing_rating_rank INTEGER,

    -- Defensive rolling: points & yards
    def_pts_r3       REAL,
    def_pts_r5       REAL,
    def_pts_r10      REAL,
    def_yds_r3       REAL,
    def_yds_r5       REAL,
    def_yds_r10      REAL,
    def_pass_yds_r3  REAL,
    def_pass_yds_r5  REAL,
    def_pass_yds_r10 REAL,
    def_rush_yds_r3  REAL,
    def_rush_yds_r5  REAL,
    def_rush_yds_r10 REAL,

    -- Defensive rolling: efficiency
    def_ypp_r3           REAL,
    def_ypp_r5           REAL,
    def_ypp_r10          REAL,
    def_third_down_pct_r3 REAL,
    def_third_down_pct_r5 REAL,
    def_rz_td_pct_r3     REAL,
    def_rz_td_pct_r5     REAL,
    def_first_downs_r3   REAL,
    def_first_downs_r5   REAL,
    def_rz_trips_r3      REAL,
    def_rz_trips_r5      REAL,
    def_three_and_outs_r3 REAL,
    def_three_and_outs_r5 REAL,
    def_ints_thrown_r3   REAL,
    def_ints_thrown_r5   REAL,
    def_fourth_down_pct_r3 REAL,
    def_fourth_down_pct_r5 REAL,
    sacks_r3             REAL,
    sacks_r5             REAL,
    takeaways_r3         REAL,
    takeaways_r5         REAL,
    def_epa_per_play_r3  REAL,
    def_epa_per_play_r5  REAL,
    def_explosive_rate_r3 REAL,
    def_explosive_rate_r5 REAL,

    -- Differential rolling
    point_diff_r3        REAL,
    point_diff_r5        REAL,
    point_diff_r10       REAL,
    yardage_diff_r3      REAL,
    yardage_diff_r5      REAL,
    yardage_diff_r10     REAL,
    turnover_margin_r3   REAL,
    turnover_margin_r5   REAL,
    turnover_margin_r10  REAL,

    -- Performance rolling
    win_pct_r3        REAL,
    win_pct_r5        REAL,
    win_pct_r10       REAL,
    cover_pct_r3      REAL,
    cover_pct_r5      REAL,
    cover_pct_r10     REAL,
    ou_over_pct_r3    REAL,
    ou_over_pct_r5    REAL,
    ou_over_pct_r10   REAL,
    margin_r3         REAL,
    margin_r5         REAL,
    margin_r10        REAL,

    -- ATS/OU rolling
    ou_margin_r3      REAL,
    ou_margin_r5      REAL,
    ou_margin_r10     REAL,
    ats_margin_r3     REAL,
    ats_margin_r5     REAL,
    ats_margin_r10    REAL,

    -- Season-to-date records (including current game)
    season_wins       INTEGER,
    season_losses     INTEGER,
    season_win_pct    REAL,
    season_ats_pct    REAL,
    season_ou_over_pct REAL,

    -- Streaks (including current game)
    win_streak        INTEGER,
    loss_streak       INTEGER,
    cover_streak      INTEGER,
    ou_streak         INTEGER,

    PRIMARY KEY (game_id, team_abbr)
);

CREATE INDEX IF NOT EXISTS idx_trs_season_team ON nfl.team_rolling_stats (season, team_abbr);
CREATE INDEX IF NOT EXISTS idx_trs_date       ON nfl.team_rolling_stats (game_date);
CREATE INDEX IF NOT EXISTS idx_trs_game       ON nfl.team_rolling_stats (game_id);
"""


POPULATE_SQL = """
-- Clean REG+POST rows so the combined (full-season, playoffs-roll-in) insert coexists
-- cleanly. PRE rows are never built here (cumulative_game_stats has no PRE rows).
DELETE FROM nfl.team_rolling_stats WHERE game_type IN ('REG', 'POST');

-- Step 1: Per-game values by diffing cumulative totals from cumulative_game_stats.
WITH per_game AS (
    SELECT
        c.game_id,
        c.team_abbr,
        c.season,
        c.season_type AS game_type,
        c.week,
        g.date AS game_date,
        CASE WHEN t_home.abbreviation = c.team_abbr THEN true ELSE false END AS is_home,
        c.games_played,

        -- Offensive per-game (diff of cumulative totals; for first game, LAG is NULL -> COALESCE 0)
        c.off_pts - COALESCE(LAG(c.off_pts) OVER w, 0) AS off_pts_pg,
        c.off_total_yds - COALESCE(LAG(c.off_total_yds) OVER w, 0) AS off_yds_pg,
        c.off_pass_yds - COALESCE(LAG(c.off_pass_yds) OVER w, 0) AS pass_yds_pg,
        c.off_pass_att - COALESCE(LAG(c.off_pass_att) OVER w, 0) AS pass_att_pg,
        c.off_pass_cmp - COALESCE(LAG(c.off_pass_cmp) OVER w, 0) AS pass_cmp_pg,
        c.off_rush_yds - COALESCE(LAG(c.off_rush_yds) OVER w, 0) AS rush_yds_pg,
        c.off_rush_att - COALESCE(LAG(c.off_rush_att) OVER w, 0) AS rush_att_pg,
        c.off_rush_td - COALESCE(LAG(c.off_rush_td) OVER w, 0) AS rush_td_pg,
        c.off_plays - COALESCE(LAG(c.off_plays) OVER w, 0) AS off_plays_pg,
        c.off_first_downs - COALESCE(LAG(c.off_first_downs) OVER w, 0) AS first_downs_pg,
        c.off_third_down_att - COALESCE(LAG(c.off_third_down_att) OVER w, 0) AS third_down_att_pg,
        c.off_third_down_conv - COALESCE(LAG(c.off_third_down_conv) OVER w, 0) AS third_down_conv_pg,
        c.off_red_zone_trips - COALESCE(LAG(c.off_red_zone_trips) OVER w, 0) AS rz_trips_pg,
        c.off_red_zone_td - COALESCE(LAG(c.off_red_zone_td) OVER w, 0) AS rz_td_pg,
        c.off_explosive_plays - COALESCE(LAG(c.off_explosive_plays) OVER w, 0) AS explosive_pg,
        c.off_three_and_outs - COALESCE(LAG(c.off_three_and_outs) OVER w, 0) AS three_and_out_pg,
        c.off_interceptions - COALESCE(LAG(c.off_interceptions) OVER w, 0) AS ints_pg,
        c.off_fumbles_lost - COALESCE(LAG(c.off_fumbles_lost) OVER w, 0) AS fumbles_pg,
        c.off_fourth_down_att - COALESCE(LAG(c.off_fourth_down_att) OVER w, 0) AS fourth_down_att_pg,
        c.off_fourth_down_conv - COALESCE(LAG(c.off_fourth_down_conv) OVER w, 0) AS fourth_down_conv_pg,
        c.off_sacks_allowed - COALESCE(LAG(c.off_sacks_allowed) OVER w, 0) AS sacks_taken_pg,
        c.off_passing_epa - COALESCE(LAG(c.off_passing_epa) OVER w, 0) AS pass_epa_pg,
        c.off_rushing_epa - COALESCE(LAG(c.off_rushing_epa) OVER w, 0) AS rush_epa_pg,

        -- Defensive per-game
        c.def_pts_allowed - COALESCE(LAG(c.def_pts_allowed) OVER w, 0) AS def_pts_pg,
        c.def_total_yds_allowed - COALESCE(LAG(c.def_total_yds_allowed) OVER w, 0) AS def_yds_pg,
        c.def_pass_yds_allowed - COALESCE(LAG(c.def_pass_yds_allowed) OVER w, 0) AS def_pass_yds_pg,
        c.def_pass_att_faced - COALESCE(LAG(c.def_pass_att_faced) OVER w, 0) AS def_pass_att_pg,
        c.def_pass_cmp_allowed - COALESCE(LAG(c.def_pass_cmp_allowed) OVER w, 0) AS def_pass_cmp_pg,
        c.def_rush_yds_allowed - COALESCE(LAG(c.def_rush_yds_allowed) OVER w, 0) AS def_rush_yds_pg,
        c.def_rush_att_faced - COALESCE(LAG(c.def_rush_att_faced) OVER w, 0) AS def_rush_att_pg,
        c.def_plays_faced - COALESCE(LAG(c.def_plays_faced) OVER w, 0) AS def_plays_pg,
        c.def_first_downs_allowed - COALESCE(LAG(c.def_first_downs_allowed) OVER w, 0) AS def_first_downs_pg,
        c.def_third_down_att - COALESCE(LAG(c.def_third_down_att) OVER w, 0) AS def_third_down_att_pg,
        c.def_third_down_conv - COALESCE(LAG(c.def_third_down_conv) OVER w, 0) AS def_third_down_conv_pg,
        c.def_red_zone_trips - COALESCE(LAG(c.def_red_zone_trips) OVER w, 0) AS def_rz_trips_pg,
        c.def_red_zone_td - COALESCE(LAG(c.def_red_zone_td) OVER w, 0) AS def_rz_td_pg,
        c.def_fourth_down_att - COALESCE(LAG(c.def_fourth_down_att) OVER w, 0) AS def_fourth_down_att_pg,
        c.def_fourth_down_conv - COALESCE(LAG(c.def_fourth_down_conv) OVER w, 0) AS def_fourth_down_conv_pg,
        c.def_sacks - COALESCE(LAG(c.def_sacks) OVER w, 0) AS sacks_pg,
        c.def_interceptions - COALESCE(LAG(c.def_interceptions) OVER w, 0) AS def_ints_pg,
        c.def_fumbles_recovered - COALESCE(LAG(c.def_fumbles_recovered) OVER w, 0) AS def_fumbles_pg,
        c.def_explosive_plays_allowed - COALESCE(LAG(c.def_explosive_plays_allowed) OVER w, 0) AS def_explosive_pg,
        c.def_three_and_outs_forced - COALESCE(LAG(c.def_three_and_outs_forced) OVER w, 0) AS def_three_and_out_pg,
        c.def_passing_epa_allowed - COALESCE(LAG(c.def_passing_epa_allowed) OVER w, 0) AS def_pass_epa_pg,
        c.def_rushing_epa_allowed - COALESCE(LAG(c.def_rushing_epa_allowed) OVER w, 0) AS def_rush_epa_pg,

        -- Scores and betting data
        g.home_score,
        g.away_score,
        bl.closing_spread,
        bl.closing_ou

    FROM nfl.cumulative_game_stats c
    JOIN nfl.games g ON c.game_id = g.id
    JOIN nfl.teams t_home ON t_home.id = g.home_team_id
    JOIN nfl.teams t_away ON t_away.id = g.away_team_id
    LEFT JOIN nfl.betting_lines_consolidated bl ON c.game_id = bl.game_id
    WHERE g.game_type IN ('REG', 'POST')  -- include playoffs so postseason rolls carry
    WINDOW w AS (PARTITION BY c.season, c.team_abbr ORDER BY c.games_played
                 ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING)
),
derived AS (
    SELECT
        *,
        -- Derived per-game rate metrics
        CASE WHEN pass_att_pg > 0 THEN pass_cmp_pg::REAL / pass_att_pg ELSE NULL END AS cmp_pct_pg,
        CASE WHEN off_plays_pg > 0 THEN off_yds_pg::REAL / off_plays_pg ELSE NULL END AS ypp_pg,
        CASE WHEN pass_att_pg > 0 THEN pass_yds_pg::REAL / pass_att_pg ELSE NULL END AS pass_ypa_pg,
        CASE WHEN rush_att_pg > 0 THEN rush_yds_pg::REAL / rush_att_pg ELSE NULL END AS rush_ypa_pg,
        CASE WHEN third_down_att_pg > 0 THEN third_down_conv_pg::REAL / third_down_att_pg ELSE NULL END AS third_down_pct_pg,
        CASE WHEN rz_trips_pg > 0 THEN rz_td_pg::REAL / rz_trips_pg ELSE NULL END AS rz_td_pct_pg,
        CASE WHEN fourth_down_att_pg > 0 THEN fourth_down_conv_pg::REAL / fourth_down_att_pg ELSE NULL END AS fourth_down_pct_pg,
        CASE WHEN off_plays_pg > 0 THEN (pass_epa_pg + rush_epa_pg) / off_plays_pg ELSE NULL END AS epa_per_play_pg,
        CASE WHEN off_plays_pg > 0 THEN explosive_pg::REAL / off_plays_pg ELSE NULL END AS explosive_rate_pg,
        CASE WHEN off_plays_pg > 0 THEN three_and_out_pg::REAL / off_plays_pg ELSE NULL END AS three_and_out_rate_pg,

        CASE WHEN def_plays_pg > 0 THEN def_yds_pg::REAL / def_plays_pg ELSE NULL END AS def_ypp_pg,
        CASE WHEN def_third_down_att_pg > 0 THEN def_third_down_conv_pg::REAL / def_third_down_att_pg ELSE NULL END AS def_third_down_pct_pg,
        CASE WHEN def_rz_trips_pg > 0 THEN def_rz_td_pg::REAL / def_rz_trips_pg ELSE NULL END AS def_rz_td_pct_pg,
        CASE WHEN def_plays_pg > 0 THEN (def_pass_epa_pg + def_rush_epa_pg) / def_plays_pg ELSE NULL END AS def_epa_per_play_pg,
        CASE WHEN def_plays_pg > 0 THEN def_explosive_pg::REAL / def_plays_pg ELSE NULL END AS def_explosive_rate_pg,
        CASE WHEN def_plays_pg > 0 THEN def_three_and_out_pg::REAL / def_plays_pg ELSE NULL END AS def_three_and_out_rate_pg,
        CASE WHEN pass_att_pg > 0 THEN sacks_taken_pg::REAL / pass_att_pg ELSE NULL END AS sack_rate_taken_pg,
        CASE WHEN def_pass_att_pg > 0 THEN sacks_pg::REAL / def_pass_att_pg ELSE NULL END AS def_sack_rate_pg,
        CASE WHEN off_plays_pg > 0 THEN (ints_pg + fumbles_pg)::REAL / off_plays_pg ELSE NULL END AS turnover_rate_pg,
        CASE WHEN def_plays_pg > 0 THEN (def_ints_pg + def_fumbles_pg)::REAL / def_plays_pg ELSE NULL END AS takeaway_rate_pg,

        -- Game outcome from team perspective
        CASE WHEN is_home THEN home_score ELSE away_score END AS team_score,
        CASE WHEN is_home THEN away_score ELSE home_score END AS opp_score,
        CASE WHEN is_home THEN (home_score - away_score) ELSE (away_score - home_score) END AS margin,
        CASE
            WHEN (CASE WHEN is_home THEN home_score ELSE away_score END) >
                 (CASE WHEN is_home THEN away_score ELSE home_score END) THEN 1
            ELSE 0
        END AS won,

        -- ATS cover from team perspective
        CASE
            WHEN closing_spread IS NOT NULL THEN
                CASE WHEN is_home THEN
                    CASE WHEN (home_score - away_score + closing_spread::REAL) > 0 THEN 1 ELSE 0 END
                ELSE
                    CASE WHEN (away_score - home_score - closing_spread::REAL) > 0 THEN 1 ELSE 0 END
                END
            ELSE NULL
        END AS covered,

        -- Over result
        CASE WHEN closing_ou IS NOT NULL THEN
            CASE WHEN (home_score + away_score) > closing_ou THEN 1 ELSE 0 END
        ELSE NULL END AS over_result,

        -- ATS/OU margin
        CASE WHEN closing_spread IS NOT NULL THEN
            CASE WHEN is_home THEN (home_score - away_score + closing_spread::REAL)
                 ELSE (away_score - home_score - closing_spread::REAL)
            END
        ELSE NULL END AS ats_margin,
        CASE WHEN closing_ou IS NOT NULL THEN
            (home_score + away_score - closing_ou::REAL)
        ELSE NULL END AS ou_margin

    FROM per_game
),
-- Step 2: Rolling window averages (INCLUDING current game's data)
rolling AS (
    SELECT
        game_id, team_abbr, season, game_type, week, game_date, is_home, games_played,

        -- Offensive rolling
        AVG(off_pts_pg)       FILTER (WHERE off_pts_pg IS NOT NULL) OVER w3  AS off_pts_r3,
        AVG(off_pts_pg)       FILTER (WHERE off_pts_pg IS NOT NULL) OVER w5  AS off_pts_r5,
        AVG(off_pts_pg)       FILTER (WHERE off_pts_pg IS NOT NULL) OVER w10 AS off_pts_r10,
        AVG(off_yds_pg)       FILTER (WHERE off_yds_pg IS NOT NULL) OVER w3  AS off_yds_r3,
        AVG(off_yds_pg)       FILTER (WHERE off_yds_pg IS NOT NULL) OVER w5  AS off_yds_r5,
        AVG(off_yds_pg)       FILTER (WHERE off_yds_pg IS NOT NULL) OVER w10 AS off_yds_r10,
        AVG(pass_yds_pg)      FILTER (WHERE pass_yds_pg IS NOT NULL) OVER w3  AS pass_yds_r3,
        AVG(pass_yds_pg)      FILTER (WHERE pass_yds_pg IS NOT NULL) OVER w5  AS pass_yds_r5,
        AVG(pass_yds_pg)      FILTER (WHERE pass_yds_pg IS NOT NULL) OVER w10 AS pass_yds_r10,
        AVG(rush_yds_pg)      FILTER (WHERE rush_yds_pg IS NOT NULL) OVER w3  AS rush_yds_r3,
        AVG(rush_yds_pg)      FILTER (WHERE rush_yds_pg IS NOT NULL) OVER w5  AS rush_yds_r5,
        AVG(rush_yds_pg)      FILTER (WHERE rush_yds_pg IS NOT NULL) OVER w10 AS rush_yds_r10,

        AVG(ypp_pg)           FILTER (WHERE ypp_pg IS NOT NULL)      OVER w3  AS ypp_r3,
        AVG(ypp_pg)           FILTER (WHERE ypp_pg IS NOT NULL)      OVER w5  AS ypp_r5,
        AVG(ypp_pg)           FILTER (WHERE ypp_pg IS NOT NULL)      OVER w10 AS ypp_r10,
        AVG(pass_ypa_pg)      FILTER (WHERE pass_ypa_pg IS NOT NULL) OVER w3  AS pass_ypa_r3,
        AVG(pass_ypa_pg)      FILTER (WHERE pass_ypa_pg IS NOT NULL) OVER w5  AS pass_ypa_r5,
        AVG(pass_ypa_pg)      FILTER (WHERE pass_ypa_pg IS NOT NULL) OVER w10 AS pass_ypa_r10,
        AVG(rush_ypa_pg)      FILTER (WHERE rush_ypa_pg IS NOT NULL) OVER w3  AS rush_ypa_r3,
        AVG(rush_ypa_pg)      FILTER (WHERE rush_ypa_pg IS NOT NULL) OVER w5  AS rush_ypa_r5,
        AVG(rush_ypa_pg)      FILTER (WHERE rush_ypa_pg IS NOT NULL) OVER w10 AS rush_ypa_r10,
        AVG(cmp_pct_pg)       FILTER (WHERE cmp_pct_pg IS NOT NULL)  OVER w3  AS cmp_pct_r3,
        AVG(cmp_pct_pg)       FILTER (WHERE cmp_pct_pg IS NOT NULL)  OVER w5  AS cmp_pct_r5,
        AVG(cmp_pct_pg)       FILTER (WHERE cmp_pct_pg IS NOT NULL)  OVER w10 AS cmp_pct_r10,

        AVG(first_downs_pg)   FILTER (WHERE first_downs_pg IS NOT NULL) OVER w3 AS first_downs_r3,
        AVG(first_downs_pg)   FILTER (WHERE first_downs_pg IS NOT NULL) OVER w5 AS first_downs_r5,
        AVG(rz_trips_pg)      FILTER (WHERE rz_trips_pg IS NOT NULL) OVER w3 AS rz_trips_r3,
        AVG(rz_trips_pg)      FILTER (WHERE rz_trips_pg IS NOT NULL) OVER w5 AS rz_trips_r5,
        AVG(ints_pg)          FILTER (WHERE ints_pg IS NOT NULL) OVER w3 AS ints_thrown_r3,
        AVG(ints_pg)          FILTER (WHERE ints_pg IS NOT NULL) OVER w5 AS ints_thrown_r5,
        AVG(third_down_pct_pg) FILTER (WHERE third_down_pct_pg IS NOT NULL) OVER w3 AS third_down_pct_r3,
        AVG(third_down_pct_pg) FILTER (WHERE third_down_pct_pg IS NOT NULL) OVER w5 AS third_down_pct_r5,
        AVG(rz_td_pct_pg)     FILTER (WHERE rz_td_pct_pg IS NOT NULL) OVER w3 AS rz_td_pct_r3,
        AVG(rz_td_pct_pg)     FILTER (WHERE rz_td_pct_pg IS NOT NULL) OVER w5 AS rz_td_pct_r5,
        AVG(epa_per_play_pg)  FILTER (WHERE epa_per_play_pg IS NOT NULL) OVER w3 AS epa_per_play_r3,
        AVG(epa_per_play_pg)  FILTER (WHERE epa_per_play_pg IS NOT NULL) OVER w5 AS epa_per_play_r5,
        AVG(explosive_rate_pg) FILTER (WHERE explosive_rate_pg IS NOT NULL) OVER w3 AS explosive_rate_r3,
        AVG(explosive_rate_pg) FILTER (WHERE explosive_rate_pg IS NOT NULL) OVER w5 AS explosive_rate_r5,
        AVG(three_and_out_rate_pg) FILTER (WHERE three_and_out_rate_pg IS NOT NULL) OVER w3 AS three_and_out_rate_r3,
        AVG(three_and_out_rate_pg) FILTER (WHERE three_and_out_rate_pg IS NOT NULL) OVER w5 AS three_and_out_rate_r5,

        AVG(pass_att_pg)      FILTER (WHERE pass_att_pg IS NOT NULL) OVER w3 AS pass_att_r3,
        AVG(pass_att_pg)      FILTER (WHERE pass_att_pg IS NOT NULL) OVER w5 AS pass_att_r5,
        AVG(rush_att_pg)      FILTER (WHERE rush_att_pg IS NOT NULL) OVER w3 AS rush_att_r3,
        AVG(rush_att_pg)      FILTER (WHERE rush_att_pg IS NOT NULL) OVER w5 AS rush_att_r5,
        AVG(rush_td_pg)       FILTER (WHERE rush_td_pg IS NOT NULL) OVER w3 AS rush_td_r3,
        AVG(rush_td_pg)       FILTER (WHERE rush_td_pg IS NOT NULL) OVER w5 AS rush_td_r5,
        AVG(fumbles_pg)       FILTER (WHERE fumbles_pg IS NOT NULL) OVER w3 AS fumbles_r3,
        AVG(fumbles_pg)       FILTER (WHERE fumbles_pg IS NOT NULL) OVER w5 AS fumbles_r5,
        AVG(fourth_down_pct_pg) FILTER (WHERE fourth_down_pct_pg IS NOT NULL) OVER w3 AS fourth_down_pct_r3,
        AVG(fourth_down_pct_pg) FILTER (WHERE fourth_down_pct_pg IS NOT NULL) OVER w5 AS fourth_down_pct_r5,

        -- Standard deviations (5-game)
        STDDEV_SAMP(off_pts_pg)  FILTER (WHERE off_pts_pg IS NOT NULL)  OVER w5 AS off_pts_stddev_r5,
        STDDEV_SAMP(off_yds_pg)  FILTER (WHERE off_yds_pg IS NOT NULL)  OVER w5 AS off_yds_stddev_r5,
        STDDEV_SAMP(def_pts_pg)  FILTER (WHERE def_pts_pg IS NOT NULL)  OVER w5 AS opp_pts_stddev_r5,
        STDDEV_SAMP(def_yds_pg)  FILTER (WHERE def_yds_pg IS NOT NULL)  OVER w5 AS opp_yds_stddev_r5,

        -- Defensive rolling
        AVG(def_pts_pg)       FILTER (WHERE def_pts_pg IS NOT NULL) OVER w3  AS def_pts_r3,
        AVG(def_pts_pg)       FILTER (WHERE def_pts_pg IS NOT NULL) OVER w5  AS def_pts_r5,
        AVG(def_pts_pg)       FILTER (WHERE def_pts_pg IS NOT NULL) OVER w10 AS def_pts_r10,
        AVG(def_yds_pg)       FILTER (WHERE def_yds_pg IS NOT NULL) OVER w3  AS def_yds_r3,
        AVG(def_yds_pg)       FILTER (WHERE def_yds_pg IS NOT NULL) OVER w5  AS def_yds_r5,
        AVG(def_yds_pg)       FILTER (WHERE def_yds_pg IS NOT NULL) OVER w10 AS def_yds_r10,
        AVG(def_pass_yds_pg)  FILTER (WHERE def_pass_yds_pg IS NOT NULL) OVER w3  AS def_pass_yds_r3,
        AVG(def_pass_yds_pg)  FILTER (WHERE def_pass_yds_pg IS NOT NULL) OVER w5  AS def_pass_yds_r5,
        AVG(def_pass_yds_pg)  FILTER (WHERE def_pass_yds_pg IS NOT NULL) OVER w10 AS def_pass_yds_r10,
        AVG(def_rush_yds_pg)  FILTER (WHERE def_rush_yds_pg IS NOT NULL) OVER w3  AS def_rush_yds_r3,
        AVG(def_rush_yds_pg)  FILTER (WHERE def_rush_yds_pg IS NOT NULL) OVER w5  AS def_rush_yds_r5,
        AVG(def_rush_yds_pg)  FILTER (WHERE def_rush_yds_pg IS NOT NULL) OVER w10 AS def_rush_yds_r10,

        AVG(def_ypp_pg)       FILTER (WHERE def_ypp_pg IS NOT NULL) OVER w3  AS def_ypp_r3,
        AVG(def_ypp_pg)       FILTER (WHERE def_ypp_pg IS NOT NULL) OVER w5  AS def_ypp_r5,
        AVG(def_ypp_pg)       FILTER (WHERE def_ypp_pg IS NOT NULL) OVER w10 AS def_ypp_r10,
        AVG(def_third_down_pct_pg) FILTER (WHERE def_third_down_pct_pg IS NOT NULL) OVER w3 AS def_third_down_pct_r3,
        AVG(def_third_down_pct_pg) FILTER (WHERE def_third_down_pct_pg IS NOT NULL) OVER w5 AS def_third_down_pct_r5,
        AVG(def_rz_td_pct_pg) FILTER (WHERE def_rz_td_pct_pg IS NOT NULL) OVER w3 AS def_rz_td_pct_r3,
        AVG(def_rz_td_pct_pg) FILTER (WHERE def_rz_td_pct_pg IS NOT NULL) OVER w5 AS def_rz_td_pct_r5,
        AVG(sacks_pg)         FILTER (WHERE sacks_pg IS NOT NULL) OVER w3 AS sacks_r3,
        AVG(sacks_pg)         FILTER (WHERE sacks_pg IS NOT NULL) OVER w5 AS sacks_r5,
        AVG(def_ints_pg + def_fumbles_pg) FILTER (WHERE def_ints_pg IS NOT NULL AND def_fumbles_pg IS NOT NULL) OVER w3 AS takeaways_r3,
        AVG(def_ints_pg + def_fumbles_pg) FILTER (WHERE def_ints_pg IS NOT NULL AND def_fumbles_pg IS NOT NULL) OVER w5 AS takeaways_r5,
        AVG(def_epa_per_play_pg) FILTER (WHERE def_epa_per_play_pg IS NOT NULL) OVER w3 AS def_epa_per_play_r3,
        AVG(def_epa_per_play_pg) FILTER (WHERE def_epa_per_play_pg IS NOT NULL) OVER w5 AS def_epa_per_play_r5,
        AVG(def_explosive_rate_pg) FILTER (WHERE def_explosive_rate_pg IS NOT NULL) OVER w3 AS def_explosive_rate_r3,
        AVG(def_explosive_rate_pg) FILTER (WHERE def_explosive_rate_pg IS NOT NULL) OVER w5 AS def_explosive_rate_r5,
        -- Additional defensive rolling
        AVG(def_first_downs_pg) FILTER (WHERE def_first_downs_pg IS NOT NULL) OVER w3 AS def_first_downs_r3,
        AVG(def_first_downs_pg) FILTER (WHERE def_first_downs_pg IS NOT NULL) OVER w5 AS def_first_downs_r5,
        AVG(def_rz_trips_pg) FILTER (WHERE def_rz_trips_pg IS NOT NULL) OVER w3 AS def_rz_trips_r3,
        AVG(def_rz_trips_pg) FILTER (WHERE def_rz_trips_pg IS NOT NULL) OVER w5 AS def_rz_trips_r5,
        AVG(def_three_and_out_pg) FILTER (WHERE def_three_and_out_pg IS NOT NULL) OVER w3 AS def_three_and_outs_r3,
        AVG(def_three_and_out_pg) FILTER (WHERE def_three_and_out_pg IS NOT NULL) OVER w5 AS def_three_and_outs_r5,
        AVG(def_ints_pg) FILTER (WHERE def_ints_pg IS NOT NULL) OVER w3 AS def_ints_thrown_r3,
        AVG(def_ints_pg) FILTER (WHERE def_ints_pg IS NOT NULL) OVER w5 AS def_ints_thrown_r5,
        AVG(def_fourth_down_conv_pg) FILTER (WHERE def_fourth_down_att_pg > 0) OVER w3
          / NULLIF(AVG(def_fourth_down_att_pg) FILTER (WHERE def_fourth_down_att_pg > 0) OVER w3, 0) AS def_fourth_down_pct_r3,
        AVG(def_fourth_down_conv_pg) FILTER (WHERE def_fourth_down_att_pg > 0) OVER w5
          / NULLIF(AVG(def_fourth_down_att_pg) FILTER (WHERE def_fourth_down_att_pg > 0) OVER w5, 0) AS def_fourth_down_pct_r5,

        -- Differential rolling
        AVG(margin)               FILTER (WHERE margin IS NOT NULL)               OVER w3  AS point_diff_r3,
        AVG(margin)               FILTER (WHERE margin IS NOT NULL)               OVER w5  AS point_diff_r5,
        AVG(margin)               FILTER (WHERE margin IS NOT NULL)               OVER w10 AS point_diff_r10,
        AVG(off_yds_pg - def_yds_pg) FILTER (WHERE off_yds_pg IS NOT NULL AND def_yds_pg IS NOT NULL) OVER w3  AS yardage_diff_r3,
        AVG(off_yds_pg - def_yds_pg) FILTER (WHERE off_yds_pg IS NOT NULL AND def_yds_pg IS NOT NULL) OVER w5  AS yardage_diff_r5,
        AVG(off_yds_pg - def_yds_pg) FILTER (WHERE off_yds_pg IS NOT NULL AND def_yds_pg IS NOT NULL) OVER w10 AS yardage_diff_r10,
        AVG((ints_pg + fumbles_pg) - (def_ints_pg + def_fumbles_pg))
            FILTER (WHERE ints_pg IS NOT NULL AND fumbles_pg IS NOT NULL AND def_ints_pg IS NOT NULL AND def_fumbles_pg IS NOT NULL)
            OVER w3  AS turnover_margin_r3,
        AVG((ints_pg + fumbles_pg) - (def_ints_pg + def_fumbles_pg))
            FILTER (WHERE ints_pg IS NOT NULL AND fumbles_pg IS NOT NULL AND def_ints_pg IS NOT NULL AND def_fumbles_pg IS NOT NULL)
            OVER w5  AS turnover_margin_r5,
        AVG((ints_pg + fumbles_pg) - (def_ints_pg + def_fumbles_pg))
            FILTER (WHERE ints_pg IS NOT NULL AND fumbles_pg IS NOT NULL AND def_ints_pg IS NOT NULL AND def_fumbles_pg IS NOT NULL)
            OVER w10 AS turnover_margin_r10,

        -- Performance rolling
        AVG(won::int::REAL)        FILTER (WHERE won IS NOT NULL)       OVER w3  AS win_pct_r3,
        AVG(won::int::REAL)        FILTER (WHERE won IS NOT NULL)       OVER w5  AS win_pct_r5,
        AVG(won::int::REAL)        FILTER (WHERE won IS NOT NULL)       OVER w10 AS win_pct_r10,
        AVG(covered::REAL)         FILTER (WHERE covered IS NOT NULL)   OVER w3  AS cover_pct_r3,
        AVG(covered::REAL)         FILTER (WHERE covered IS NOT NULL)   OVER w5  AS cover_pct_r5,
        AVG(covered::REAL)         FILTER (WHERE covered IS NOT NULL)   OVER w10 AS cover_pct_r10,
        AVG(over_result::REAL)     FILTER (WHERE over_result IS NOT NULL) OVER w3  AS ou_over_pct_r3,
        AVG(over_result::REAL)     FILTER (WHERE over_result IS NOT NULL) OVER w5  AS ou_over_pct_r5,
        AVG(over_result::REAL)     FILTER (WHERE over_result IS NOT NULL) OVER w10 AS ou_over_pct_r10,
        AVG(margin)                FILTER (WHERE margin IS NOT NULL)    OVER w3  AS margin_r3,
        AVG(margin)                FILTER (WHERE margin IS NOT NULL)    OVER w5  AS margin_r5,
        AVG(margin)                FILTER (WHERE margin IS NOT NULL)    OVER w10 AS margin_r10,

        -- ATS/OU rolling
        AVG(ou_margin)             FILTER (WHERE ou_margin IS NOT NULL) OVER w3  AS ou_margin_r3,
        AVG(ou_margin)             FILTER (WHERE ou_margin IS NOT NULL) OVER w5  AS ou_margin_r5,
        AVG(ou_margin)             FILTER (WHERE ou_margin IS NOT NULL) OVER w10 AS ou_margin_r10,
        AVG(ats_margin)            FILTER (WHERE ats_margin IS NOT NULL) OVER w3  AS ats_margin_r3,
        AVG(ats_margin)            FILTER (WHERE ats_margin IS NOT NULL) OVER w5  AS ats_margin_r5,
        AVG(ats_margin)            FILTER (WHERE ats_margin IS NOT NULL) OVER w10 AS ats_margin_r10

    FROM derived
    -- INCLUDING current game's data (data loader processes completed games)
    WINDOW
        w3  AS (PARTITION BY season, team_abbr ORDER BY games_played
                ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
        w5  AS (PARTITION BY season, team_abbr ORDER BY games_played
                ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
        w10 AS (PARTITION BY season, team_abbr ORDER BY games_played
                ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
),
-- Step 3: Season-to-date cumulative stats (including current game)
season_cumul AS (
    SELECT
        game_id, team_abbr, season, game_type, games_played,
        SUM(won::int) OVER w_season AS cum_wins,
        COUNT(*) OVER w_season - SUM(won::int) OVER w_season AS cum_losses,
        SUM(covered::int) FILTER (WHERE covered IS NOT NULL) OVER w_season AS cum_ats_wins,
        SUM(CASE WHEN covered IS NOT NULL THEN 1 ELSE 0 END) OVER w_season AS cum_ats_games,
        SUM(over_result::int) FILTER (WHERE over_result IS NOT NULL) OVER w_season AS cum_ou_overs,
        SUM(CASE WHEN over_result IS NOT NULL THEN 1 ELSE 0 END) OVER w_season AS cum_ou_games
    FROM derived
    WINDOW w_season AS (PARTITION BY season, team_abbr ORDER BY games_played
                        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
),
-- Step 4: Streaks via gaps-and-islands (including current game)
islands AS (
    SELECT *,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr ORDER BY games_played)
            - ROW_NUMBER() OVER (PARTITION BY season, team_abbr, won ORDER BY games_played) AS win_grp,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr ORDER BY games_played)
            - ROW_NUMBER() OVER (PARTITION BY season, team_abbr, covered ORDER BY games_played) AS cover_grp,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr ORDER BY games_played)
            - ROW_NUMBER() OVER (PARTITION BY season, team_abbr, over_result ORDER BY games_played) AS ou_grp
    FROM derived
),
streak_counts AS (
    SELECT *,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr, won, win_grp ORDER BY games_played) AS win_streak_n,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr, covered, cover_grp ORDER BY games_played) AS cover_streak_n,
        ROW_NUMBER() OVER (PARTITION BY season, team_abbr, over_result, ou_grp ORDER BY games_played) AS ou_streak_n
    FROM islands
),
streaks AS (
    SELECT game_id, team_abbr, season, games_played,
        CASE WHEN won = 1 THEN win_streak_n ELSE 0 END AS win_streak,
        CASE WHEN won = 0 THEN win_streak_n ELSE 0 END AS loss_streak,
        CASE WHEN covered = 1 THEN cover_streak_n ELSE 0 END AS cover_streak,
        CASE WHEN over_result = 1 THEN ou_streak_n ELSE 0 END AS ou_streak
    FROM streak_counts
),
season_ranks AS (
    SELECT
        r.game_id, r.team_abbr, r.season, r.week,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.off_yds_r5 DESC)  AS off_yardage_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.def_yds_r5 ASC)  AS def_yardage_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.off_pts_r5 DESC)  AS off_scoring_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.def_pts_r5 ASC)  AS def_scoring_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.rush_yds_r5 DESC)  AS off_rushing_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.def_rush_yds_r5 ASC) AS def_rushing_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.pass_yds_r5 DESC)  AS off_passing_rank,
        DENSE_RANK()  OVER (PARTITION BY season, r.week ORDER BY r.def_ypp_r5 ASC)  AS def_passing_rating_rank
    FROM rolling r
)
INSERT INTO nfl.team_rolling_stats (
    game_id, team_abbr, season, game_type, week, game_date, is_home, games_played, feeds_into_game_id,
    off_pts_r3, off_pts_r5, off_pts_r10,
    off_yds_r3, off_yds_r5, off_yds_r10,
    pass_yds_r3, pass_yds_r5, pass_yds_r10,
    rush_yds_r3, rush_yds_r5, rush_yds_r10,
    ypp_r3, ypp_r5, ypp_r10,
    pass_ypa_r3, pass_ypa_r5, pass_ypa_r10,
    rush_ypa_r3, rush_ypa_r5, rush_ypa_r10,
    cmp_pct_r3, cmp_pct_r5, cmp_pct_r10,
    first_downs_r3, first_downs_r5,
    rz_trips_r3, rz_trips_r5,
    ints_thrown_r3, ints_thrown_r5,
    third_down_pct_r3, third_down_pct_r5,
    rz_td_pct_r3, rz_td_pct_r5,
    epa_per_play_r3, epa_per_play_r5,
    explosive_rate_r3, explosive_rate_r5,
    three_and_out_rate_r3, three_and_out_rate_r5,
    pass_att_r3, pass_att_r5,
    rush_att_r3, rush_att_r5,
    rush_td_r3, rush_td_r5,
    fumbles_r3, fumbles_r5,
    fourth_down_pct_r3, fourth_down_pct_r5,
    off_pts_stddev_r5, off_yds_stddev_r5,
    opp_pts_stddev_r5, opp_yds_stddev_r5,
    def_pts_r3, def_pts_r5, def_pts_r10,
    def_yds_r3, def_yds_r5, def_yds_r10,
    def_pass_yds_r3, def_pass_yds_r5, def_pass_yds_r10,
    def_rush_yds_r3, def_rush_yds_r5, def_rush_yds_r10,
    def_ypp_r3, def_ypp_r5, def_ypp_r10,
    def_third_down_pct_r3, def_third_down_pct_r5,
    def_rz_td_pct_r3, def_rz_td_pct_r5,
    def_first_downs_r3, def_first_downs_r5,
    def_rz_trips_r3, def_rz_trips_r5,
    def_three_and_outs_r3, def_three_and_outs_r5,
    def_ints_thrown_r3, def_ints_thrown_r5,
    def_fourth_down_pct_r3, def_fourth_down_pct_r5,
    sacks_r3, sacks_r5,
    takeaways_r3, takeaways_r5,
    def_epa_per_play_r3, def_epa_per_play_r5,
    def_explosive_rate_r3, def_explosive_rate_r5,
    point_diff_r3, point_diff_r5, point_diff_r10,
    yardage_diff_r3, yardage_diff_r5, yardage_diff_r10,
    turnover_margin_r3, turnover_margin_r5, turnover_margin_r10,
    win_pct_r3, win_pct_r5, win_pct_r10,
    cover_pct_r3, cover_pct_r5, cover_pct_r10,
    ou_over_pct_r3, ou_over_pct_r5, ou_over_pct_r10,
    margin_r3, margin_r5, margin_r10,
    ou_margin_r3, ou_margin_r5, ou_margin_r10,
    ats_margin_r3, ats_margin_r5, ats_margin_r10,
    season_wins, season_losses, season_win_pct,
    season_ats_pct, season_ou_over_pct,
    win_streak, loss_streak, cover_streak, ou_streak,
    off_yardage_rank, def_yardage_rank,
    off_scoring_rank, def_scoring_rank,
    off_rushing_rank, def_rushing_rank,
    off_passing_rank, def_passing_rating_rank
)
SELECT
    r.game_id, r.team_abbr, r.season, r.game_type, r.week, r.game_date, r.is_home, r.games_played,
    LEAD(r.game_id) OVER (PARTITION BY r.team_abbr, r.season ORDER BY r.game_date) AS feeds_into_game_id,
    r.off_pts_r3, r.off_pts_r5, r.off_pts_r10,
    r.off_yds_r3, r.off_yds_r5, r.off_yds_r10,
    r.pass_yds_r3, r.pass_yds_r5, r.pass_yds_r10,
    r.rush_yds_r3, r.rush_yds_r5, r.rush_yds_r10,
    r.ypp_r3, r.ypp_r5, r.ypp_r10,
    r.pass_ypa_r3, r.pass_ypa_r5, r.pass_ypa_r10,
    r.rush_ypa_r3, r.rush_ypa_r5, r.rush_ypa_r10,
    r.cmp_pct_r3, r.cmp_pct_r5, r.cmp_pct_r10,
    r.first_downs_r3, r.first_downs_r5,
    r.rz_trips_r3, r.rz_trips_r5,
    r.ints_thrown_r3, r.ints_thrown_r5,
    r.third_down_pct_r3, r.third_down_pct_r5,
    r.rz_td_pct_r3, r.rz_td_pct_r5,
    r.epa_per_play_r3, r.epa_per_play_r5,
    r.explosive_rate_r3, r.explosive_rate_r5,
    r.three_and_out_rate_r3, r.three_and_out_rate_r5,
    r.pass_att_r3, r.pass_att_r5,
    r.rush_att_r3, r.rush_att_r5,
    r.rush_td_r3, r.rush_td_r5,
    r.fumbles_r3, r.fumbles_r5,
    r.fourth_down_pct_r3, r.fourth_down_pct_r5,
    r.off_pts_stddev_r5, r.off_yds_stddev_r5,
    r.opp_pts_stddev_r5, r.opp_yds_stddev_r5,
    r.def_pts_r3, r.def_pts_r5, r.def_pts_r10,
    r.def_yds_r3, r.def_yds_r5, r.def_yds_r10,
    r.def_pass_yds_r3, r.def_pass_yds_r5, r.def_pass_yds_r10,
    r.def_rush_yds_r3, r.def_rush_yds_r5, r.def_rush_yds_r10,
    r.def_ypp_r3, r.def_ypp_r5, r.def_ypp_r10,
    r.def_third_down_pct_r3, r.def_third_down_pct_r5,
    r.def_rz_td_pct_r3, r.def_rz_td_pct_r5,
    r.def_first_downs_r3, r.def_first_downs_r5,
    r.def_rz_trips_r3, r.def_rz_trips_r5,
    r.def_three_and_outs_r3, r.def_three_and_outs_r5,
    r.def_ints_thrown_r3, r.def_ints_thrown_r5,
    r.def_fourth_down_pct_r3, r.def_fourth_down_pct_r5,
    r.sacks_r3, r.sacks_r5,
    r.takeaways_r3, r.takeaways_r5,
    r.def_epa_per_play_r3, r.def_epa_per_play_r5,
    r.def_explosive_rate_r3, r.def_explosive_rate_r5,
    r.point_diff_r3, r.point_diff_r5, r.point_diff_r10,
    r.yardage_diff_r3, r.yardage_diff_r5, r.yardage_diff_r10,
    r.turnover_margin_r3, r.turnover_margin_r5, r.turnover_margin_r10,
    r.win_pct_r3, r.win_pct_r5, r.win_pct_r10,
    r.cover_pct_r3, r.cover_pct_r5, r.cover_pct_r10,
    r.ou_over_pct_r3, r.ou_over_pct_r5, r.ou_over_pct_r10,
    r.margin_r3, r.margin_r5, r.margin_r10,
    r.ou_margin_r3, r.ou_margin_r5, r.ou_margin_r10,
    r.ats_margin_r3, r.ats_margin_r5, r.ats_margin_r10,
    COALESCE(sc.cum_wins, 0),
    COALESCE(sc.cum_losses, 0),
    CASE WHEN COALESCE(sc.cum_wins, 0) + COALESCE(sc.cum_losses, 0) > 0
         THEN COALESCE(sc.cum_wins, 0)::REAL / (COALESCE(sc.cum_wins, 0) + COALESCE(sc.cum_losses, 0))
         ELSE NULL END,
    CASE WHEN COALESCE(sc.cum_ats_games, 0) > 0
         THEN COALESCE(sc.cum_ats_wins, 0)::REAL / sc.cum_ats_games
         ELSE NULL END,
    CASE WHEN COALESCE(sc.cum_ou_games, 0) > 0
         THEN COALESCE(sc.cum_ou_overs, 0)::REAL / sc.cum_ou_games
         ELSE NULL END,
    COALESCE(st.win_streak, 0),
    COALESCE(st.loss_streak, 0),
    COALESCE(st.cover_streak, 0),
    COALESCE(st.ou_streak, 0),
    sr.off_yardage_rank,
    sr.def_yardage_rank,
    sr.off_scoring_rank,
    sr.def_scoring_rank,
    sr.off_rushing_rank,
    sr.def_rushing_rank,
    sr.off_passing_rank,
    sr.def_passing_rating_rank
FROM rolling r
LEFT JOIN season_cumul sc ON r.game_id = sc.game_id AND r.team_abbr = sc.team_abbr
LEFT JOIN streaks st     ON r.game_id = st.game_id   AND r.team_abbr = st.team_abbr
LEFT JOIN season_ranks sr ON r.game_id = sr.game_id AND r.team_abbr = sr.team_abbr
WHERE r.game_type IN ('REG', 'POST');
"""


def create_table() -> None:
    """Create nfl.team_rolling_stats if it doesn't exist."""
    with SessionLocal() as session:
        session.execute(text(CREATE_TABLE_SQL))
        session.commit()
    logger.info("nfl.team_rolling_stats table created/verified")


def populate(game_type: str = "REG") -> None:
    """Populate nfl.team_rolling_stats (REG+POST, playoffs roll into postseason).

    `game_type` kept for backward-compat; ignored. Rows are always built over
    REG+POST so playoff games carry the season's regular-season history. Preseason
    (PRE) rows are never built (source has none).
    """
    with SessionLocal() as session:
        logger.info("Populating nfl.team_rolling_stats (REG+POST, playoffs roll in)...")
        result = session.execute(text(POPULATE_SQL))
        session.commit()
        if result.rowcount >= 0:
            logger.info("Populated %d rows", result.rowcount)
        else:
            logger.info("Populate complete (rowcount unavailable)")


def run(game_type: str = "REG") -> None:
    """Create table and populate in one call."""
    create_table()
    populate(game_type)


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    _ap = argparse.ArgumentParser()
    _ap.add_argument("--game-type", default="REG", choices=["REG", "PRE", "POST"],
                     help="Which game_type to compute rolling stats for (default REG)")
    _args = _ap.parse_args()
    run(_args.game_type)
