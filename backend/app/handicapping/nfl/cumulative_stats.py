"""Build nfl.cumulative_game_stats — cumulative (backward-looking) team stats per game.

Replaces repeated window‑function recomputation with Python‑side incremental
accumulation, matching the MLB cumulative_stats pattern.

Table layout:
  identity columns   (game_id, team_abbr, season, week, …)
  raw accumulators   (off_pts, def_pts_allowed, …)
  derived rates      (off_ppg, def_ppg_allowed, …)
  differentials      (point_differential_avg, …)
  momentum           (win_streak, last_5_wins, …)
  variance           (off_pts_stddev_5, …)
  recency‑weighted   (rw_off_ppg, rw_def_ppg, …)

Incremental: only processes games not yet in the table.
"""

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# ──────────────── Table schema ────────────────

CREATE_CUMULATIVE_TABLE = """
CREATE TABLE IF NOT EXISTS nfl.cumulative_game_stats (
    -- Identity
    game_id             INTEGER NOT NULL,
    team_abbr           VARCHAR(3)  NOT NULL,
    season              INTEGER NOT NULL,
    week                INTEGER NOT NULL,
    season_type         VARCHAR(10) NOT NULL DEFAULT 'REG',
    opponent_abbr       VARCHAR(3)  NOT NULL,
    games_played        INTEGER DEFAULT 0,
    -- Offensive accumulators
    off_pts             INTEGER DEFAULT 0,
    off_total_yds       INTEGER DEFAULT 0,
    off_plays           INTEGER DEFAULT 0,
    off_pass_yds        INTEGER DEFAULT 0,
    off_pass_att        INTEGER DEFAULT 0,
    off_pass_cmp        INTEGER DEFAULT 0,
    off_rush_yds        INTEGER DEFAULT 0,
    off_rush_att        INTEGER DEFAULT 0,
    off_tds             INTEGER DEFAULT 0,
    off_pass_td         INTEGER DEFAULT 0,
    off_rush_td         INTEGER DEFAULT 0,
    off_first_downs     INTEGER DEFAULT 0,
    off_third_down_att  INTEGER DEFAULT 0,
    off_third_down_conv INTEGER DEFAULT 0,
    off_fourth_down_att INTEGER DEFAULT 0,
    off_fourth_down_conv INTEGER DEFAULT 0,
    off_red_zone_trips  INTEGER DEFAULT 0,
    off_red_zone_td     INTEGER DEFAULT 0,
    off_explosive_plays INTEGER DEFAULT 0,
    off_three_and_outs  INTEGER DEFAULT 0,
    off_interceptions   INTEGER DEFAULT 0,
    off_fumbles_lost    INTEGER DEFAULT 0,
    off_sacks_allowed   INTEGER DEFAULT 0,
    off_sack_yds_lost   INTEGER DEFAULT 0,
    off_passing_epa     FLOAT DEFAULT 0,
    off_rushing_epa     FLOAT DEFAULT 0,
    -- Defensive accumulators
    def_pts_allowed             INTEGER DEFAULT 0,
    def_total_yds_allowed       INTEGER DEFAULT 0,
    def_plays_faced             INTEGER DEFAULT 0,
    def_pass_yds_allowed        INTEGER DEFAULT 0,
    def_pass_att_faced          INTEGER DEFAULT 0,
    def_pass_cmp_allowed        INTEGER DEFAULT 0,
    def_rush_yds_allowed        INTEGER DEFAULT 0,
    def_rush_att_faced          INTEGER DEFAULT 0,
    def_tds_allowed             INTEGER DEFAULT 0,
    def_pass_td_allowed         INTEGER DEFAULT 0,
    def_rush_td_allowed         INTEGER DEFAULT 0,
    def_first_downs_allowed     INTEGER DEFAULT 0,
    def_third_down_att          INTEGER DEFAULT 0,
    def_third_down_conv         INTEGER DEFAULT 0,
    def_fourth_down_att         INTEGER DEFAULT 0,
    def_fourth_down_conv        INTEGER DEFAULT 0,
    def_red_zone_trips          INTEGER DEFAULT 0,
    def_red_zone_td             INTEGER DEFAULT 0,
    def_sacks                   INTEGER DEFAULT 0,
    def_interceptions           INTEGER DEFAULT 0,
    def_fumbles_recovered       INTEGER DEFAULT 0,
    def_explosive_plays_allowed INTEGER DEFAULT 0,
    def_three_and_outs_forced   INTEGER DEFAULT 0,
    def_passing_epa_allowed     FLOAT DEFAULT 0,
    def_rushing_epa_allowed     FLOAT DEFAULT 0,
    -- Discipline
    pen_committed   INTEGER DEFAULT 0,
    pen_yds         INTEGER DEFAULT 0,
    pen_drawn       INTEGER DEFAULT 0,
    pen_yds_drawn   INTEGER DEFAULT 0,
    -- Momentum / streaks
    win_streak      INTEGER DEFAULT 0,
    -- Derived offensive rates
    off_ppg                 FLOAT DEFAULT 0,
    off_ypg                 FLOAT DEFAULT 0,
    off_pass_ypg            FLOAT DEFAULT 0,
    off_rush_ypg            FLOAT DEFAULT 0,
    off_ypa                 FLOAT DEFAULT 0,
    off_ypc                 FLOAT DEFAULT 0,
    off_ypp                 FLOAT DEFAULT 0,
    off_cmp_pct             FLOAT DEFAULT 0,
    off_third_down_pct      FLOAT DEFAULT 0,
    off_fourth_down_pct     FLOAT DEFAULT 0,
    off_rz_td_pct           FLOAT DEFAULT 0,
    off_explosive_rate      FLOAT DEFAULT 0,
    off_three_and_out_rate  FLOAT DEFAULT 0,
    off_int_rate            FLOAT DEFAULT 0,
    off_epa_per_play        FLOAT DEFAULT 0,
    -- Derived defensive rates
    def_ppg_allowed         FLOAT DEFAULT 0,
    def_ypg_allowed         FLOAT DEFAULT 0,
    def_pass_ypg_allowed    FLOAT DEFAULT 0,
    def_rush_ypg_allowed    FLOAT DEFAULT 0,
    def_ypa_allowed         FLOAT DEFAULT 0,
    def_ypc_allowed         FLOAT DEFAULT 0,
    def_ypp_allowed         FLOAT DEFAULT 0,
    def_cmp_pct_allowed     FLOAT DEFAULT 0,
    def_third_down_pct      FLOAT DEFAULT 0,
    def_fourth_down_pct     FLOAT DEFAULT 0,
    def_rz_td_pct           FLOAT DEFAULT 0,
    def_sack_rate           FLOAT DEFAULT 0,
    def_takeaway_rate       FLOAT DEFAULT 0,
    def_explosive_rate      FLOAT DEFAULT 0,
    def_three_and_out_rate  FLOAT DEFAULT 0,
    def_epa_per_play        FLOAT DEFAULT 0,
    -- Differentials
    point_differential_avg      FLOAT DEFAULT 0,
    yardage_differential_avg    FLOAT DEFAULT 0,
    pass_yds_differential_avg   FLOAT DEFAULT 0,
    rush_yds_differential_avg   FLOAT DEFAULT 0,
    turnover_margin_avg         FLOAT DEFAULT 0,
    third_down_differential     FLOAT DEFAULT 0,
    -- Variance (rolling 5‑game std dev)
    off_pts_stddev_5        FLOAT DEFAULT 0,
    off_yds_stddev_5        FLOAT DEFAULT 0,
    def_pts_stddev_5        FLOAT DEFAULT 0,
    def_yds_stddev_5        FLOAT DEFAULT 0,
    -- Recency‑weighted averages (exponential, α≈0.6)
    rw_off_ppg              FLOAT DEFAULT 0,
    rw_off_ypg              FLOAT DEFAULT 0,
    rw_def_ppg              FLOAT DEFAULT 0,
    rw_def_ypg              FLOAT DEFAULT 0,
    -- Opponent‑adjusted stats (additive SoS correction)
    adj_off_ppg             FLOAT DEFAULT 0,
    adj_off_ypg             FLOAT DEFAULT 0,
    adj_def_ppg             FLOAT DEFAULT 0,
    adj_def_ypg             FLOAT DEFAULT 0,

    PRIMARY KEY (game_id, team_abbr, season)
);
"""
# ──────────────── SQL query ────────────────

PER_GAME_QUERY = """
-- One row per team per game: offensive stats from that team's game_stats,
-- defensive stats from the opponent's game_stats.
-- Resolves nfl.games FK references (season via seasons, teams via teams).
WITH team_games AS (
    -- Home team perspective
    SELECT
        g.id              AS game_id,
        s.year            AS season,
        g.week,
        gs.season_type,
        g.date,
        ht.abbreviation   AS team_abbr,
        at.abbreviation   AS opponent_abbr,
        g.home_score      AS pts_scored,
        g.away_score      AS pts_allowed,
        gs.total_yards,
        (gs.pass_attempts + gs.rush_attempts) AS plays,
        gs.pass_yards,
        gs.pass_attempts,
        gs.pass_completions,
        gs.rush_yards,
        gs.rush_attempts,
        (gs.pass_tds + gs.rush_tds)  AS tds,
        gs.pass_tds,
        gs.rush_tds,
        gs.pass_interceptions,
        COALESCE(gs.sacks_suffered, 0)          AS sacks_suffered,
        COALESCE(gs.sack_yards_lost, 0)         AS sack_yards_lost,
        COALESCE(gs.fumbles_lost, 0)            AS fumbles_lost,
        COALESCE(gs.penalties, 0)               AS penalties,
        COALESCE(gs.penalty_yards, 0)           AS penalty_yards,
        COALESCE(gs.passing_epa, 0)             AS passing_epa,
        COALESCE(gs.rushing_epa, 0)             AS rushing_epa,
        COALESCE(gs.first_downs, 0)             AS first_downs,
        COALESCE(gs.third_down_attempts, 0)     AS third_down_attempts,
        COALESCE(gs.third_down_conversions, 0)  AS third_down_conversions,
        COALESCE(gs.fourth_down_attempts, 0)    AS fourth_down_attempts,
        COALESCE(gs.fourth_down_conversions, 0) AS fourth_down_conversions,
        COALESCE(gs.interceptions_thrown, 0)    AS interceptions_thrown,
        COALESCE(gs.explosive_plays, 0)         AS explosive_plays,
        COALESCE(gs.red_zone_trips, 0)          AS red_zone_trips,
        COALESCE(gs.red_zone_tds, 0)            AS red_zone_tds,
        COALESCE(gs.three_and_outs, 0)          AS three_and_outs,
        -- Defensive stats come from opponent's game_stats
        opp.total_yards                          AS opp_total_yards,
        (opp.pass_attempts + opp.rush_attempts)  AS opp_plays,
        opp.pass_yards                           AS opp_pass_yards,
        opp.pass_attempts                        AS opp_pass_attempts,
        opp.pass_completions                     AS opp_pass_completions,
        opp.rush_yards                           AS opp_rush_yards,
        opp.rush_attempts                        AS opp_rush_attempts,
        (opp.pass_tds + opp.rush_tds)            AS opp_tds,
        opp.pass_tds                             AS opp_pass_tds,
        opp.rush_tds                             AS opp_rush_tds,
        opp.pass_interceptions                   AS opp_pass_interceptions,
        COALESCE(opp.sacks_suffered, 0)          AS opp_sacks_suffered,
        COALESCE(opp.fumbles_lost, 0)            AS opp_fumbles_lost,
        COALESCE(opp.first_downs, 0)             AS opp_first_downs,
        COALESCE(opp.third_down_attempts, 0)     AS opp_third_down_attempts,
        COALESCE(opp.third_down_conversions, 0)  AS opp_third_down_conversions,
        COALESCE(opp.fourth_down_attempts, 0)    AS opp_fourth_down_attempts,
        COALESCE(opp.fourth_down_conversions, 0) AS opp_fourth_down_conversions,
        COALESCE(opp.explosive_plays, 0)         AS opp_explosive_plays,
        COALESCE(opp.red_zone_trips, 0)          AS opp_red_zone_trips,
        COALESCE(opp.red_zone_tds, 0)            AS opp_red_zone_tds,
        COALESCE(opp.three_and_outs, 0)          AS opp_three_and_outs,
        COALESCE(opp.penalties, 0)               AS opp_penalties,
        COALESCE(opp.penalty_yards, 0)           AS opp_penalty_yards,
        COALESCE(opp.passing_epa, 0)             AS opp_passing_epa,
        COALESCE(opp.rushing_epa, 0)             AS opp_rushing_epa
    FROM nfl.games g
    JOIN nfl.seasons s ON s.id = g.season_id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    JOIN nfl.game_stats gs
        ON gs.season = s.year AND gs.week = g.week
        AND gs.team_abbr = ht.abbreviation
    JOIN nfl.game_stats opp
        ON opp.season = s.year AND opp.week = g.week
        AND opp.team_abbr = at.abbreviation
    WHERE s.year = :season AND g.week >= 1

    UNION ALL

    -- Away team perspective
    SELECT
        g.id              AS game_id,
        s.year            AS season,
        g.week,
        gs.season_type,
        g.date,
        at.abbreviation   AS team_abbr,
        ht.abbreviation   AS opponent_abbr,
        g.away_score      AS pts_scored,
        g.home_score      AS pts_allowed,
        opp.total_yards,
        (opp.pass_attempts + opp.rush_attempts) AS plays,
        opp.pass_yards,
        opp.pass_attempts,
        opp.pass_completions,
        opp.rush_yards,
        opp.rush_attempts,
        (opp.pass_tds + opp.rush_tds)  AS tds,
        opp.pass_tds,
        opp.rush_tds,
        opp.pass_interceptions,
        COALESCE(opp.sacks_suffered, 0)          AS sacks_suffered,
        COALESCE(opp.sack_yards_lost, 0)         AS sack_yards_lost,
        COALESCE(opp.fumbles_lost, 0)            AS fumbles_lost,
        COALESCE(opp.penalties, 0)               AS penalties,
        COALESCE(opp.penalty_yards, 0)           AS penalty_yards,
        COALESCE(opp.passing_epa, 0)             AS passing_epa,
        COALESCE(opp.rushing_epa, 0)             AS rushing_epa,
        COALESCE(opp.first_downs, 0)             AS first_downs,
        COALESCE(opp.third_down_attempts, 0)     AS third_down_attempts,
        COALESCE(opp.third_down_conversions, 0)  AS third_down_conversions,
        COALESCE(opp.fourth_down_attempts, 0)    AS fourth_down_attempts,
        COALESCE(opp.fourth_down_conversions, 0) AS fourth_down_conversions,
        COALESCE(opp.interceptions_thrown, 0)    AS interceptions_thrown,
        COALESCE(opp.explosive_plays, 0)         AS explosive_plays,
        COALESCE(opp.red_zone_trips, 0)          AS red_zone_trips,
        COALESCE(opp.red_zone_tds, 0)            AS red_zone_tds,
        COALESCE(opp.three_and_outs, 0)          AS three_and_outs,
        -- Defensive stats come from opponent's game_stats (gs = home team for away perspective)
        gs.total_yards                            AS opp_total_yards,
        (gs.pass_attempts + gs.rush_attempts)     AS opp_plays,
        gs.pass_yards                             AS opp_pass_yards,
        gs.pass_attempts                          AS opp_pass_attempts,
        gs.pass_completions                       AS opp_pass_completions,
        gs.rush_yards                             AS opp_rush_yards,
        gs.rush_attempts                          AS opp_rush_attempts,
        (gs.pass_tds + gs.rush_tds)               AS opp_tds,
        gs.pass_tds                               AS opp_pass_tds,
        gs.rush_tds                               AS opp_rush_tds,
        gs.pass_interceptions                     AS opp_pass_interceptions,
        COALESCE(gs.sacks_suffered, 0)            AS opp_sacks_suffered,
        COALESCE(gs.fumbles_lost, 0)              AS opp_fumbles_lost,
        COALESCE(gs.first_downs, 0)               AS opp_first_downs,
        COALESCE(gs.third_down_attempts, 0)       AS opp_third_down_attempts,
        COALESCE(gs.third_down_conversions, 0)    AS opp_third_down_conversions,
        COALESCE(gs.fourth_down_attempts, 0)      AS opp_fourth_down_attempts,
        COALESCE(gs.fourth_down_conversions, 0)   AS opp_fourth_down_conversions,
        COALESCE(gs.explosive_plays, 0)           AS opp_explosive_plays,
        COALESCE(gs.red_zone_trips, 0)            AS opp_red_zone_trips,
        COALESCE(gs.red_zone_tds, 0)              AS opp_red_zone_tds,
        COALESCE(gs.three_and_outs, 0)            AS opp_three_and_outs,
        COALESCE(gs.penalties, 0)                 AS opp_penalties,
        COALESCE(gs.penalty_yards, 0)             AS opp_penalty_yards,
        COALESCE(gs.passing_epa, 0)               AS opp_passing_epa,
        COALESCE(gs.rushing_epa, 0)               AS opp_rushing_epa
    FROM nfl.games g
    JOIN nfl.seasons s ON s.id = g.season_id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at ON at.id = g.away_team_id
    JOIN nfl.game_stats gs
        ON gs.season = s.year AND gs.week = g.week
        AND gs.team_abbr = at.abbreviation
    JOIN nfl.game_stats opp
        ON opp.season = s.year AND opp.week = g.week
        AND opp.team_abbr = ht.abbreviation
    WHERE s.year = :season AND g.week >= 1
)
SELECT * FROM team_games
ORDER BY date, game_id, team_abbr
"""

# ──────────────── Field maps ────────────────

# (source CTE column → dest cum key)
RAW_OFF_FIELDS = [
    ("pts_scored", "off_pts"),
    ("total_yards", "off_total_yds"),
    ("pass_yards", "off_pass_yds"),
    ("pass_attempts", "off_pass_att"),
    ("pass_completions", "off_pass_cmp"),
    ("rush_yards", "off_rush_yds"),
    ("rush_attempts", "off_rush_att"),
    ("pass_tds", "off_pass_td"),
    ("rush_tds", "off_rush_td"),
    ("pass_interceptions", "off_pick_faced"),
    ("first_downs", "off_first_downs"),
    ("third_down_attempts", "off_third_down_att"),
    ("third_down_conversions", "off_third_down_conv"),
    ("fourth_down_attempts", "off_fourth_down_att"),
    ("fourth_down_conversions", "off_fourth_down_conv"),
    ("red_zone_trips", "off_red_zone_trips"),
    ("red_zone_tds", "off_red_zone_td"),
    ("explosive_plays", "off_explosive_plays"),
    ("three_and_outs", "off_three_and_outs"),
    ("interceptions_thrown", "off_interceptions"),
    ("fumbles_lost", "off_fumbles_lost"),
    ("sacks_suffered", "off_sacks_allowed"),
    ("sack_yards_lost", "off_sack_yds_lost"),
    ("passing_epa", "off_passing_epa"),
    ("rushing_epa", "off_rushing_epa"),
]

RAW_DEF_FIELDS = [
    ("pts_allowed", "def_pts_allowed"),
    ("opp_total_yards", "def_total_yds_allowed"),
    ("opp_plays", "def_plays_faced"),
    ("opp_pass_yards", "def_pass_yds_allowed"),
    ("opp_pass_attempts", "def_pass_att_faced"),
    ("opp_pass_completions", "def_pass_cmp_allowed"),
    ("opp_rush_yards", "def_rush_yds_allowed"),
    ("opp_rush_attempts", "def_rush_att_faced"),
    ("opp_tds", "def_tds_allowed"),
    ("opp_pass_tds", "def_pass_td_allowed"),
    ("opp_rush_tds", "def_rush_td_allowed"),
    ("opp_first_downs", "def_first_downs_allowed"),
    ("opp_third_down_attempts", "def_third_down_att"),
    ("opp_third_down_conversions", "def_third_down_conv"),
    ("opp_fourth_down_attempts", "def_fourth_down_att"),
    ("opp_fourth_down_conversions", "def_fourth_down_conv"),
    ("opp_red_zone_trips", "def_red_zone_trips"),
    ("opp_red_zone_tds", "def_red_zone_td"),
    ("opp_sacks_suffered", "def_sacks"),
    ("opp_pass_interceptions", "def_interceptions"),
    ("opp_fumbles_lost", "def_fumbles_recovered"),
    ("opp_explosive_plays", "def_explosive_plays_allowed"),
    ("opp_three_and_outs", "def_three_and_outs_forced"),
    ("opp_passing_epa", "def_passing_epa_allowed"),
    ("opp_rushing_epa", "def_rushing_epa_allowed"),
]

RAW_PEN_FIELDS = [
    ("penalties", "pen_committed"),
    ("penalty_yards", "pen_yds"),
    ("opp_penalties", "pen_drawn"),
    ("opp_penalty_yards", "pen_yds_drawn"),
]


# ──────────────── Helper functions ────────────────

def _compute_plays(row: dict) -> int:
    return int(row.get("pass_attempts", 0) or 0) + int(row.get("rush_attempts", 0) or 0)


def _compute_plays_faced(row: dict) -> int:
    return int(row.get("opp_pass_attempts", 0) or 0) + int(row.get("opp_rush_attempts", 0) or 0)


def _compute_opp_tds(row: dict) -> int:
    return int(row.get("opp_pass_tds", 0) or 0) + int(row.get("opp_rush_tds", 0) or 0)


def _compute_fumbles_rec(row: dict) -> int:
    """Opponent fumbles lost = this team's fumbles recovered."""
    return int(row.get("opp_fumbles_lost", 0) or 0)


def _compute_tds(row: dict) -> int:
    return int(row.get("pass_tds", 0) or 0) + int(row.get("rush_tds", 0) or 0)


def _compute_won(row: dict) -> bool:
    return int(row.get("pts_scored", 0)) > int(row.get("pts_allowed", 0))


# Fields that are computed (not directly summed from the query)
COMPUTED_FIELDS = {
    "plays": _compute_plays,
    "tds": _compute_tds,
    "opp_plays": _compute_plays_faced,
    "opp_tds": _compute_opp_tds,
    "opp_fumbles_lost": _compute_fumbles_rec,
}

DERIVED_FIELDS = [
    "off_ppg", "off_ypg", "off_pass_ypg", "off_rush_ypg",
    "off_ypa", "off_ypc", "off_ypp", "off_cmp_pct",
    "off_third_down_pct", "off_fourth_down_pct", "off_rz_td_pct",
    "off_explosive_rate", "off_three_and_out_rate", "off_int_rate",
    "off_epa_per_play",
    "def_ppg_allowed", "def_ypg_allowed", "def_pass_ypg_allowed",
    "def_rush_ypg_allowed", "def_ypa_allowed", "def_ypc_allowed",
    "def_ypp_allowed", "def_cmp_pct_allowed", "def_third_down_pct",
    "def_fourth_down_pct", "def_rz_td_pct", "def_sack_rate", "def_takeaway_rate",
    "def_explosive_rate", "def_three_and_out_rate",
    "def_epa_per_play",
    "point_differential_avg", "yardage_differential_avg",
    "pass_yds_differential_avg", "rush_yds_differential_avg",
    "turnover_margin_avg", "third_down_differential",
    "off_pts_stddev_5", "off_yds_stddev_5", "def_pts_stddev_5", "def_yds_stddev_5",
    "rw_off_ppg", "rw_off_ypg", "rw_def_ppg", "rw_def_ypg",
    "adj_off_ppg", "adj_off_ypg", "adj_def_ppg", "adj_def_ypg",
    "adj_off_ppg", "adj_off_ypg", "adj_def_ppg", "adj_def_ypg",
]


# ──────────────── Core accumulation ────────────────

def accumulate_row(cum: dict, row: dict) -> dict:
    """Add one game's values to the cumulative totals (in‑place + return)."""
    cum["games_played"] += 1

    # Offensive stats
    for src_key, dst_key in RAW_OFF_FIELDS:
        if src_key in ("plays", "tds"):
            continue
        val = row.get(src_key, 0) or 0
        cum[dst_key] = cum.get(dst_key, 0) + val

    cum["off_plays"] = cum.get("off_plays", 0) + _compute_plays(row)
    cum["off_tds"] = cum.get("off_tds", 0) + _compute_tds(row)

    # Defensive stats
    for src_key, dst_key in RAW_DEF_FIELDS:
        if src_key in ("opp_plays", "opp_tds", "opp_fumbles_lost"):
            continue
        val = row.get(src_key, 0) or 0
        cum[dst_key] = cum.get(dst_key, 0) + val

    cum["def_plays_faced"] = cum.get("def_plays_faced", 0) + _compute_plays_faced(row)
    cum["def_tds_allowed"] = cum.get("def_tds_allowed", 0) + _compute_opp_tds(row)
    cum["def_fumbles_recovered"] = cum.get("def_fumbles_recovered", 0) + _compute_fumbles_rec(row)
    # NOTE: def_total_yds_allowed, def_pts_allowed are covered by RAW_DEF_FIELDS loop above.

    # Penalties
    for src_key, dst_key in RAW_PEN_FIELDS:
        val = row.get(src_key, 0) or 0
        cum[dst_key] = cum.get(dst_key, 0) + val

    # ── Momentum: win streak ──
    won = _compute_won(row)
    if won:
        cum["win_streak"] = cum.get("win_streak", 0) + 1
    else:
        cum["win_streak"] = 0

    # ── Variance buffer: keep last N game values for std dev ──
    buf = cum.setdefault("_buf", {
        "off_pts": [],
        "off_total_yds": [],
        "def_pts_allowed": [],
        "def_total_yds_allowed": [],
    })
    MAX_BUF = 5
    buf["off_pts"].append(int(row.get("pts_scored", 0) or 0))
    if len(buf["off_pts"]) > MAX_BUF:
        buf["off_pts"].pop(0)
    buf["off_total_yds"].append(int(row.get("total_yards", 0) or 0))
    if len(buf["off_total_yds"]) > MAX_BUF:
        buf["off_total_yds"].pop(0)
    buf["def_pts_allowed"].append(int(row.get("pts_allowed", 0) or 0))
    if len(buf["def_pts_allowed"]) > MAX_BUF:
        buf["def_pts_allowed"].pop(0)
    buf["def_total_yds_allowed"].append(int(row.get("opp_total_yards", 0) or 0))
    if len(buf["def_total_yds_allowed"]) > MAX_BUF:
        buf["def_total_yds_allowed"].pop(0)

    # ── Recency‑weighted (exponential, α=0.6) ──
    alpha = 0.6
    rw_map = {
        "rw_off_ppg": ("pts_scored", "off_ppg"),
        "rw_off_ypg": ("total_yards", "off_ypg"),
        "rw_def_ppg": ("pts_allowed", "def_ppg_allowed"),
        "rw_def_ypg": ("opp_total_yards", "def_ypg_allowed"),
    }
    for rw_key, (src_col, _rate_col) in rw_map.items():
        cur_val = float(row.get(src_col, 0) or 0)
        old_rw = cum.get(rw_key, 0)
        cum[rw_key] = round(alpha * cur_val + (1 - alpha) * old_rw, 2)

    return cum


def compute_rates(cum: dict) -> dict:
    """Compute derived rate stats from the cumulative sums."""
    r = dict(cum)
    gp = max(cum.get("games_played", 0), 1)
    gp_f = float(gp)

    # Offensive rates
    r["off_ppg"] = round(float(cum.get("off_pts", 0)) / gp_f, 2)
    r["off_ypg"] = round(float(cum.get("off_total_yds", 0)) / gp_f, 2)
    r["off_pass_ypg"] = round(float(cum.get("off_pass_yds", 0)) / gp_f, 2)
    r["off_rush_ypg"] = round(float(cum.get("off_rush_yds", 0)) / gp_f, 2)

    pa = float(cum.get("off_pass_att", 0) or 0)
    r["off_ypa"] = round(float(cum.get("off_pass_yds", 0)) / pa, 2) if pa else 0.0
    ra = float(cum.get("off_rush_att", 0) or 0)
    r["off_ypc"] = round(float(cum.get("off_rush_yds", 0)) / ra, 2) if ra else 0.0
    pl = float(cum.get("off_plays", 0) or 0)
    r["off_ypp"] = round(float(cum.get("off_total_yds", 0)) / pl, 2) if pl else 0.0
    r["off_cmp_pct"] = round(float(cum.get("off_pass_cmp", 0)) / pa, 4) if pa else 0.0

    t3a = float(cum.get("off_third_down_att", 0) or 0)
    r["off_third_down_pct"] = round(float(cum.get("off_third_down_conv", 0)) / t3a, 4) if t3a else 0.0
    t4a = float(cum.get("off_fourth_down_att", 0) or 0)
    r["off_fourth_down_pct"] = round(float(cum.get("off_fourth_down_conv", 0)) / t4a, 4) if t4a else 0.0
    rzt = float(cum.get("off_red_zone_trips", 0) or 0)
    r["off_rz_td_pct"] = round(float(cum.get("off_red_zone_td", 0)) / rzt, 4) if rzt else 0.0
    r["off_explosive_rate"] = round(float(cum.get("off_explosive_plays", 0)) / gp_f, 2)
    r["off_three_and_out_rate"] = round(float(cum.get("off_three_and_outs", 0)) / gp_f, 2)
    r["off_int_rate"] = round(float(cum.get("off_interceptions", 0)) / pa, 4) if pa else 0.0

    # EPA/play (offense)
    off_epa_total = float(cum.get("off_passing_epa", 0) or 0) + float(cum.get("off_rushing_epa", 0) or 0)
    r["off_epa_per_play"] = round(off_epa_total / pl, 3) if pl else 0.0

    # Defensive rates
    r["def_ppg_allowed"] = round(float(cum.get("def_pts_allowed", 0)) / gp_f, 2)
    r["def_ypg_allowed"] = round(float(cum.get("def_total_yds_allowed", 0)) / gp_f, 2)
    r["def_pass_ypg_allowed"] = round(float(cum.get("def_pass_yds_allowed", 0)) / gp_f, 2)
    r["def_rush_ypg_allowed"] = round(float(cum.get("def_rush_yds_allowed", 0)) / gp_f, 2)

    dpf = float(cum.get("def_pass_att_faced", 0) or 0)
    r["def_ypa_allowed"] = round(float(cum.get("def_pass_yds_allowed", 0)) / dpf, 2) if dpf else 0.0
    drf = float(cum.get("def_rush_att_faced", 0) or 0)
    r["def_ypc_allowed"] = round(float(cum.get("def_rush_yds_allowed", 0)) / drf, 2) if drf else 0.0
    dpl = float(cum.get("def_plays_faced", 0) or 0)
    r["def_ypp_allowed"] = round(float(cum.get("def_total_yds_allowed", 0)) / dpl, 2) if dpl else 0.0
    r["def_cmp_pct_allowed"] = round(float(cum.get("def_pass_cmp_allowed", 0)) / dpf, 4) if dpf else 0.0

    dt3a = float(cum.get("def_third_down_att", 0) or 0)
    r["def_third_down_pct"] = round(float(cum.get("def_third_down_conv", 0)) / dt3a, 4) if dt3a else 0.0
    dt4a = float(cum.get("def_fourth_down_att", 0) or 0)
    r["def_fourth_down_pct"] = round(float(cum.get("def_fourth_down_conv", 0)) / dt4a, 4) if dt4a else 0.0
    drzt = float(cum.get("def_red_zone_trips", 0) or 0)
    r["def_rz_td_pct"] = round(float(cum.get("def_red_zone_td", 0)) / drzt, 4) if drzt else 0.0
    r["def_sack_rate"] = round(float(cum.get("def_sacks", 0)) / dpf, 4) if dpf else 0.0
    r["def_takeaway_rate"] = round(float(cum.get("def_interceptions", 0) + cum.get("def_fumbles_recovered", 0)) / gp_f, 2)
    r["def_explosive_rate"] = round(float(cum.get("def_explosive_plays_allowed", 0)) / gp_f, 2)
    r["def_three_and_out_rate"] = round(float(cum.get("def_three_and_outs_forced", 0)) / gp_f, 2)

    # EPA/play (defense)
    def_epa_total = float(cum.get("def_passing_epa_allowed", 0) or 0) + float(cum.get("def_rushing_epa_allowed", 0) or 0)
    r["def_epa_per_play"] = round(def_epa_total / dpl, 3) if dpl else 0.0

    # Differentials
    r["point_differential_avg"] = round(r["off_ppg"] - r["def_ppg_allowed"], 2)
    r["yardage_differential_avg"] = round(r["off_ypg"] - r["def_ypg_allowed"], 2)
    r["pass_yds_differential_avg"] = round(r["off_pass_ypg"] - r["def_pass_ypg_allowed"], 2)
    r["rush_yds_differential_avg"] = round(r["off_rush_ypg"] - r["def_rush_ypg_allowed"], 2)
    giv = float(cum.get("off_interceptions", 0) or 0) + float(cum.get("off_fumbles_lost", 0) or 0)
    takes = float(cum.get("def_interceptions", 0) or 0) + float(cum.get("def_fumbles_recovered", 0) or 0)
    r["turnover_margin_avg"] = round((takes - giv) / gp_f, 2)
    r["third_down_differential"] = round(r["off_third_down_pct"] - r["def_third_down_pct"], 4)

    # ── Variance: std dev of last 5 games ──
    import statistics
    buf = cum.get("_buf", {})
    for key, vals_key in [("off_pts_stddev_5", "off_pts"),
                          ("off_yds_stddev_5", "off_total_yds"),
                          ("def_pts_stddev_5", "def_pts_allowed"),
                          ("def_yds_stddev_5", "def_total_yds_allowed")]:
        arr = buf.get(vals_key, [])
        if len(arr) >= 2:
            r[key] = round(statistics.stdev(arr), 2)
        else:
            r[key] = 0.0

    return r


UPSERT_COLS = [
    "game_id", "team_abbr", "season", "week", "season_type", "opponent_abbr",
    "games_played",
    # Offensive accumulators
    "off_pts", "off_total_yds", "off_plays", "off_pass_yds", "off_pass_att",
    "off_pass_cmp", "off_rush_yds", "off_rush_att", "off_tds", "off_pass_td",
    "off_rush_td", "off_first_downs", "off_third_down_att", "off_third_down_conv",
    "off_fourth_down_att", "off_fourth_down_conv", "off_red_zone_trips",
    "off_red_zone_td", "off_explosive_plays", "off_three_and_outs",
    "off_interceptions", "off_fumbles_lost", "off_sacks_allowed",
    "off_sack_yds_lost", "off_passing_epa", "off_rushing_epa",
    # Defensive accumulators
    "def_pts_allowed", "def_total_yds_allowed", "def_plays_faced",
    "def_pass_yds_allowed", "def_pass_att_faced", "def_pass_cmp_allowed",
    "def_rush_yds_allowed", "def_rush_att_faced", "def_tds_allowed",
    "def_pass_td_allowed", "def_rush_td_allowed", "def_first_downs_allowed",
    "def_third_down_att", "def_third_down_conv", "def_fourth_down_att",
    "def_fourth_down_conv", "def_red_zone_trips", "def_red_zone_td",
    "def_sacks", "def_interceptions", "def_fumbles_recovered",
    "def_explosive_plays_allowed", "def_three_and_outs_forced",
    "def_passing_epa_allowed", "def_rushing_epa_allowed",
    # Penalties
    "pen_committed", "pen_yds", "pen_drawn", "pen_yds_drawn",
    # Momentum
    "win_streak",
    # Derived rates
    "off_ppg", "off_ypg", "off_pass_ypg", "off_rush_ypg", "off_ypa", "off_ypc",
    "off_ypp", "off_cmp_pct", "off_third_down_pct", "off_fourth_down_pct", "off_rz_td_pct",
    "off_explosive_rate", "off_three_and_out_rate", "off_int_rate",
    "off_epa_per_play",
    "def_ppg_allowed", "def_ypg_allowed", "def_pass_ypg_allowed",
    "def_rush_ypg_allowed", "def_ypa_allowed", "def_ypc_allowed",
    "def_ypp_allowed", "def_cmp_pct_allowed", "def_third_down_pct",
    "def_fourth_down_pct", "def_rz_td_pct", "def_sack_rate", "def_takeaway_rate",
    "def_explosive_rate", "def_three_and_out_rate",
    "def_epa_per_play",
    # Differentials
    "point_differential_avg", "yardage_differential_avg",
    "pass_yds_differential_avg", "rush_yds_differential_avg",
    "turnover_margin_avg", "third_down_differential",
    # Variance
    "off_pts_stddev_5", "off_yds_stddev_5", "def_pts_stddev_5", "def_yds_stddev_5",
    # Recency-weighted
    "rw_off_ppg", "rw_off_ypg", "rw_def_ppg", "rw_def_ypg",
]


# ──────────────── Public API ────────────────

async def ensure_table_exists(db: AsyncSession):
    """Create the cumulative_game_stats table if it doesn't exist."""
    conn = await db.connection()
    await conn.execute(text(CREATE_CUMULATIVE_TABLE))
    await conn.commit()
    logger.info("Ensured nfl.cumulative_game_stats table exists")


async def compute_for_season(db: AsyncSession, season: int) -> dict:
    """Compute cumulative stats for one season, upserting into the DB."""
    from sqlalchemy import text as sql_text

    conn = await db.connection()

    # Fetch per-game data ordered chronologically
    rows_raw = await conn.execute(
        sql_text(PER_GAME_QUERY),
        {"season": season},
    )
    all_games = [dict(r._mapping) for r in rows_raw]

    if not all_games:
        logger.info(f"Season {season}: no games found")
        return {"season": season, "rows_processed": 0, "teams": 0}

    # Group by team (they come in chronological order already)
    team_games: dict[str, list[dict]] = {}
    for row in all_games:
        team_games.setdefault(row["team_abbr"], []).append(row)

    logger.info(f"Season {season}: {len(all_games)} game-rows across {len(team_games)} teams")

    # Accumulate per team
    all_cumulative = []
    for team_abbr, games in team_games.items():
        cum = {
            "games_played": 0,
            "win_streak": 0,
            "rw_off_ppg": 0.0,
            "rw_off_ypg": 0.0,
            "rw_def_ppg": 0.0,
            "rw_def_ypg": 0.0,
        }
        for game in games:
            row_with_rates = compute_rates(cum)
            row_with_rates["game_id"] = game["game_id"]
            row_with_rates["season"] = game["season"]
            row_with_rates["week"] = game["week"]
            row_with_rates["season_type"] = game["season_type"]
            row_with_rates["team_abbr"] = game["team_abbr"]
            row_with_rates["opponent_abbr"] = game["opponent_abbr"]
            all_cumulative.append(row_with_rates)
            cum = accumulate_row(cum, game)

    # Batch upsert
    batch_size = 500
    processed = 0
    for i in range(0, len(all_cumulative), batch_size):
        batch = all_cumulative[i:i + batch_size]
        await _upsert_batch(conn, batch)
        processed += len(batch)

    await conn.commit()
    logger.info(f"Season {season}: Pass 1 complete — upserted {processed} cumulative rows ({len(team_games)} teams)")

    # ── Pass 2: Opponent-adjusted stats ──
    logger.info(f"Season {season}: computing opponent-adjusted stats (SoS correction)")
    await _compute_opponent_adjusted(conn, season)

    return {"season": season, "rows_processed": processed}


async def recompute(db: AsyncSession, seasons: Optional[list[int]] = None) -> dict:
    """Recompute cumulative stats for all (or specified) seasons."""
    await ensure_table_exists(db)
    conn = await db.connection()

    if seasons is None:
        result = await conn.execute(
            text("SELECT DISTINCT s.year AS season FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id WHERE g.week >= 1 ORDER BY s.year DESC")
        )
        seasons = [row[0] for row in result]

    results = {}
    for season in seasons:
        res = await compute_for_season(db, season)
        results[season] = res

    return results


async def refresh_cumulative_stats(db: AsyncSession) -> dict:
    """Sync entry point: compute only seasons with missing games."""
    await ensure_table_exists(db)
    conn = await db.connection()

    # Get existing max week per season in cumulative table
    existing = await conn.execute(
        sql_text("""
            SELECT season, MAX(week) AS max_week
            FROM nfl.cumulative_game_stats
            GROUP BY season
        """)
    )
    existing_seasons = {row[0]: row[1] for row in existing}

    # Get all seasons
    all_seasons_res = await conn.execute(
        sql_text("SELECT DISTINCT s.year AS season FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id WHERE g.week >= 1 ORDER BY s.year")
    )
    all_seasons = [row[0] for row in all_seasons_res]

    results = {}
    for season in all_seasons:
        max_week = existing_seasons.get(season, 0)
        # Get max week in games for this season
        week_res = await conn.execute(
            sql_text("SELECT MAX(week) FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id WHERE s.year = :season"),
            {"season": season},
        )
        season_max_week = (await week_res.fetchone())[0] or 0

        if season_max_week > max_week:
            logger.info(f"Season {season}: new weeks found ({max_week} → {season_max_week}), recomputing")
            results[season] = await compute_for_season(db, season)
        else:
            logger.info(f"Season {season}: up to date (max_week={max_week})")

    return results


# ──────────────── Database helpers ────────────────

async def _compute_opponent_adjusted(conn, season: int):
    """Pass 2: compute additive SoS-adjusted PPG/YPG from raw cumulative data.

    For each team-game, adjusts the per-game stat by how much better/worse
    the opponent's defense (or offense) was vs league average entering that game.
    """
    # Read all rows for this season
    rows_raw = await conn.execute(
        text("SELECT * FROM nfl.cumulative_game_stats WHERE season = :s ORDER BY week, team_abbr"),
        {"s": season},
    )
    all_rows = [dict(r._mapping) for r in rows_raw]
    if not all_rows:
        return

    # Group by team (already in week order from the SQL ORDER BY)
    team_rows: dict[str, list[dict]] = {}
    for r in all_rows:
        team_rows.setdefault(r["team_abbr"], []).append(r)

    # League averages (overall season avg from raw totals)
    total_games = sum(r["games_played"] for r in all_rows)
    if total_games == 0:
        return

    def _avg(total_key: str, default: float) -> float:
        total = sum(r.get(total_key, 0) or 0 for r in all_rows)
        return round(total / total_games, 2) if total_games else default

    la_off_ppg = _avg("off_pts", 22.0)
    la_def_ppg = _avg("def_pts_allowed", 22.0)
    la_off_ypg = _avg("off_total_yds", 340.0)
    la_def_ypg = _avg("def_total_yds_allowed", 340.0)

    # Build fast lookup: (game_id, team_abbr) -> row
    lookup = {(r["game_id"], r["team_abbr"]): r for r in all_rows}

    updates: list[dict] = []
    COLUMNS = ["game_id", "team_abbr", "season", "adj_off_ppg", "adj_off_ypg", "adj_def_ppg", "adj_def_ypg"]

    for team_abbr, rows in team_rows.items():
        # rows are in chronological order
        cum_adj_off_pts = 0.0
        cum_adj_off_yds = 0.0
        cum_adj_def_pts = 0.0
        cum_adj_def_yds = 0.0

        for i, row in enumerate(rows):
            if i == 0:
                # No prior games — adjusted stats = 0
                updates.append({"game_id": row["game_id"], "team_abbr": team_abbr,
                                "season": season, "adj_off_ppg": 0.0, "adj_off_ypg": 0.0,
                                "adj_def_ppg": 0.0, "adj_def_ypg": 0.0})
                continue

            prev = rows[i - 1]

            # Per-game raw values from the most recent game
            # (difference between consecutive cumulative rows)
            pg_pts = row["off_pts"] - prev["off_pts"]
            pg_yds = row["off_total_yds"] - prev["off_total_yds"]
            pg_def_pts = row["def_pts_allowed"] - prev["def_pts_allowed"]
            pg_def_yds = row["def_total_yds_allowed"] - prev["def_total_yds_allowed"]

            # Opponent's cumulative defensive/offensive rating entering this game
            opp = row["opponent_abbr"]
            opp_row = lookup.get((row["game_id"], opp))

            if opp_row and opp_row["games_played"] > 0:
                opp_def_ppg = float(opp_row["def_ppg_allowed"] or la_def_ppg)
                opp_off_ppg = float(opp_row["off_ppg"] or la_off_ppg)
                opp_def_ypg = float(opp_row["def_ypg_allowed"] or la_def_ypg)
                opp_off_ypg = float(opp_row["off_ypg"] or la_off_ypg)
            else:
                # No data on opponent — no adjustment
                opp_def_ppg = la_def_ppg
                opp_off_ppg = la_off_ppg
                opp_def_ypg = la_def_ypg
                opp_off_ypg = la_off_ypg

            # Additive adjustment
            cum_adj_off_pts += pg_pts + (la_def_ppg - opp_def_ppg)
            cum_adj_off_yds += pg_yds + (la_def_ypg - opp_def_ypg)
            cum_adj_def_pts += pg_def_pts + (la_off_ppg - opp_off_ppg)
            cum_adj_def_yds += pg_def_yds + (la_off_ypg - opp_off_ypg)

            adj_gp = i  # number of prior games
            updates.append({
                "game_id": row["game_id"],
                "team_abbr": team_abbr,
                "season": season,
                "adj_off_ppg": round(cum_adj_off_pts / adj_gp, 2),
                "adj_off_ypg": round(cum_adj_off_yds / adj_gp, 2),
                "adj_def_ppg": round(cum_adj_def_pts / adj_gp, 2),
                "adj_def_ypg": round(cum_adj_def_yds / adj_gp, 2),
            })

    if not updates:
        return

    # Batch UPDATE (not upsert)
    update_sql = text("""
        UPDATE nfl.cumulative_game_stats
        SET adj_off_ppg = :adj_off_ppg, adj_off_ypg = :adj_off_ypg,
            adj_def_ppg = :adj_def_ppg, adj_def_ypg = :adj_def_ypg
        WHERE game_id = :game_id AND team_abbr = :team_abbr AND season = :season
    """)

    batch_size = 500
    for i in range(0, len(updates), batch_size):
        batch = updates[i:i + batch_size]
        await conn.execute(update_sql, batch)

    await conn.commit()
    logger.info(f"Season {season}: Pass 2 complete — adjusted {len(updates)} rows")


async def _upsert_batch(conn, batch: list[dict]):
    """Execute a batch upsert."""
    cols = [c for c in UPSERT_COLS if c != "games_played" or True]
    placeholders = ", ".join([f":{c}" for c in cols])
    update_expr = ", ".join([f"{c} = EXCLUDED.{c}" for c in cols if c not in ("game_id", "team_abbr", "season")])

    sql = f"""
        INSERT INTO nfl.cumulative_game_stats ({", ".join(cols)})
        VALUES ({placeholders})
        ON CONFLICT (game_id, team_abbr, season)
        DO UPDATE SET {update_expr}
    """

    params = [{c: r.get(c, 0) for c in cols} for r in batch]
    await conn.execute(text(sql), params)


# ──────────────── CLI entry point ────────────────

async def main():
    """Recompute cumulative stats for specified seasons (or all)."""
    import argparse
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

    parser = argparse.ArgumentParser()
    parser.add_argument("--seasons", nargs="+", type=int, default=None,
                        help="Seasons to compute (default: all existing)")
    args = parser.parse_args()

    engine = create_async_engine(
        "postgresql+asyncpg://earl:***@localhost/earl_knows_football"
    )
    async with AsyncSession(engine) as db:
        results = await recompute(db, args.seasons)
        print(f"Done: {results}")

    await engine.dispose()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
