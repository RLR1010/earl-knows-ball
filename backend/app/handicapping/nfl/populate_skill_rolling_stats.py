#!/usr/bin/env python3
"""
Populate nfl.skill_rolling_stats — rolling/cumulative stats for offensive skill
positions (RB / WR / TE). Feed for the Earl chat tool so Earl can answer
questions like "how has the running game been trending?" / "who's hot on the
ground/receiving lately?"

Both tables include the current game (CURRENT ROW boundary), matching the QB
pattern (nfl.qb_rolling_stats) and the team tables. The data loader would use
feed-through-prior-game reads if these were model features (they are NOT — this
is chat-facing only).

  nfl.skill_rolling_stats — per-game raw + cumulative-through-current +
                           3/5/10-game rolling windows for rush/receiving.

NOTE: QBs intentionally excluded (they have their own qb_cumulative_stats /
qb_rolling_stats). Kickers have their own kicker_rolling_stats table.

Usage:
    python -m backend.app.handicapping.nfl.populate_skill_rolling_stats
Or call populate_skill_rolling_tables() from code.
"""

import logging
import os
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
#  Skill Rolling Stats DDL  —  per-game raw + cumulative + 3/5/10 windows
# ═══════════════════════════════════════════════════════════════════════════════

CREATE_SKILL_SQL = """
CREATE TABLE IF NOT EXISTS nfl.skill_rolling_stats (
    player_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    game_id         INTEGER NOT NULL,
    game_type       VARCHAR(10) NOT NULL DEFAULT 'REG',
    week            INTEGER NOT NULL,
    position        VARCHAR(5) NOT NULL,
    team_abbr       TEXT NOT NULL,
    opponent_abbr   TEXT,
    game_date       DATE,

    -- Per-game raw events
    rush_attempts     DOUBLE PRECISION DEFAULT 0,
    rush_yards        DOUBLE PRECISION DEFAULT 0,
    rush_tds          DOUBLE PRECISION DEFAULT 0,
    rush_long         DOUBLE PRECISION DEFAULT 0,
    targets           DOUBLE PRECISION DEFAULT 0,
    receptions        DOUBLE PRECISION DEFAULT 0,
    receiving_yards   DOUBLE PRECISION DEFAULT 0,
    receiving_tds     DOUBLE PRECISION DEFAULT 0,
    receiving_long    DOUBLE PRECISION DEFAULT 0,
    fumbles           DOUBLE PRECISION DEFAULT 0,
    fumbles_lost      DOUBLE PRECISION DEFAULT 0,
    total_tds         DOUBLE PRECISION DEFAULT 0,

    -- Cumulative through current game (UNBOUNDED PRECEDING TO CURRENT ROW)
    cum_rush_att      DOUBLE PRECISION,
    cum_rush_yds      DOUBLE PRECISION,
    cum_rush_td       DOUBLE PRECISION,
    cum_rec           DOUBLE PRECISION,
    cum_recv_yds      DOUBLE PRECISION,
    cum_recv_td       DOUBLE PRECISION,
    cum_targets       DOUBLE PRECISION,
    cum_fumbles       DOUBLE PRECISION,
    cum_td            DOUBLE PRECISION,
    games_played      INTEGER,

    -- 3 / 5 / 10 game rolling windows through current game
    rush_att_3        DOUBLE PRECISION, rush_att_5        DOUBLE PRECISION, rush_att_10        DOUBLE PRECISION,
    rush_yds_3        DOUBLE PRECISION, rush_yds_5        DOUBLE PRECISION, rush_yds_10        DOUBLE PRECISION,
    rush_td_3         DOUBLE PRECISION, rush_td_5         DOUBLE PRECISION, rush_td_10         DOUBLE PRECISION,
    rec_3             DOUBLE PRECISION, rec_5             DOUBLE PRECISION, rec_10             DOUBLE PRECISION,
    recv_yds_3        DOUBLE PRECISION, recv_yds_5        DOUBLE PRECISION, recv_yds_10        DOUBLE PRECISION,
    recv_td_3         DOUBLE PRECISION, recv_td_5         DOUBLE PRECISION, recv_td_10         DOUBLE PRECISION,
    targets_3         DOUBLE PRECISION, targets_5         DOUBLE PRECISION, targets_10         DOUBLE PRECISION,
    fumbles_3         DOUBLE PRECISION, fumbles_5         DOUBLE PRECISION, fumbles_10         DOUBLE PRECISION,
    td_3              DOUBLE PRECISION, td_5              DOUBLE PRECISION, td_10              DOUBLE PRECISION,
    games_3           INTEGER,
    games_5           INTEGER,
    games_10          INTEGER,

    PRIMARY KEY (player_id, season, game_id, game_type)
);
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Skill Source CTE  —  RB / WR / TE from player_weekly_stats
# ═══════════════════════════════════════════════════════════════════════════════

SKILL_SOURCE_CTE = """
WITH skill_games AS (
    SELECT
        pws.player_id,
        s.year       AS season,
        g.id         AS game_id,
        g.week,
        p.position,
        t.abbreviation    AS team_abbr,
        ot.abbreviation   AS opponent_abbr,
        g.date       AS game_date,
        COALESCE(pws.rush_attempts::NUMERIC, 0)    AS rush_att,
        COALESCE(pws.rush_yards::NUMERIC, 0)       AS rush_yds,
        COALESCE(pws.rush_tds::NUMERIC, 0)         AS rush_td,
        COALESCE(pws.rush_long::NUMERIC, 0)        AS rush_long,
        COALESCE(pws.targets::NUMERIC, 0)          AS targets,
        COALESCE(pws.receptions::NUMERIC, 0)       AS receptions,
        COALESCE(pws.receiving_yards::NUMERIC, 0)  AS recv_yds,
        COALESCE(pws.receiving_tds::NUMERIC, 0)    AS recv_td,
        COALESCE(pws.receiving_long::NUMERIC, 0)   AS recv_long,
        COALESCE(pws.fumbles::NUMERIC, 0)          AS fumbles,
        COALESCE(pws.fumbles_lost::NUMERIC, 0)     AS fumbles_lost,
        (COALESCE(pws.rush_tds::NUMERIC, 0) + COALESCE(pws.receiving_tds::NUMERIC, 0)) AS total_td,
        g.game_type                                 AS game_type
    FROM nfl.player_weekly_stats pws
    JOIN nfl.games g     ON g.id    = pws.game_id
    JOIN nfl.seasons s   ON s.id    = pws.season_id
    JOIN nfl.teams t     ON t.id    = pws.team_id
    JOIN nfl.teams ot    ON ot.id   = pws.opponent_id
    JOIN nfl.players p   ON p.id    = pws.player_id
    WHERE p.position IN ('RB', 'WR', 'TE')
      AND pws.game_id IS NOT NULL
      AND s.year IS NOT NULL
      AND g.game_type IN ('REG', 'POST')  -- include playoffs so postseason rolls carry into them
)
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Populate Skill Rolling  —  cumulative + rolling in one insert
# ═══════════════════════════════════════════════════════════════════════════════

POPULATE_SKILL_SQL = SKILL_SOURCE_CTE + """
INSERT INTO nfl.skill_rolling_stats (
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    rush_attempts, rush_yards, rush_tds, rush_long, targets, receptions,
    receiving_yards, receiving_tds, receiving_long, fumbles, fumbles_lost, total_tds,
    cum_rush_att, cum_rush_yds, cum_rush_td, cum_rec, cum_recv_yds, cum_recv_td,
    cum_targets, cum_fumbles, cum_td, games_played,
    rush_att_3, rush_att_5, rush_att_10,
    rush_yds_3, rush_yds_5, rush_yds_10,
    rush_td_3, rush_td_5, rush_td_10,
    rec_3, rec_5, rec_10,
    recv_yds_3, recv_yds_5, recv_yds_10,
    recv_td_3, recv_td_5, recv_td_10,
    targets_3, targets_5, targets_10,
    fumbles_3, fumbles_5, fumbles_10,
    td_3, td_5, td_10,
    games_3, games_5, games_10
)
SELECT
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    rush_att, rush_yds, rush_td, rush_long, targets, receptions,
    recv_yds, recv_td, recv_long, fumbles, fumbles_lost, total_td,

    -- Cumulative through current game (UNBOUNDED PRECEDING TO CURRENT ROW)
    SUM(rush_att) OVER w_cum     AS cum_rush_att,
    SUM(rush_yds) OVER w_cum     AS cum_rush_yds,
    SUM(rush_td)  OVER w_cum     AS cum_rush_td,
    SUM(receptions) OVER w_cum   AS cum_rec,
    SUM(recv_yds) OVER w_cum     AS cum_recv_yds,
    SUM(recv_td)  OVER w_cum     AS cum_recv_td,
    SUM(targets)  OVER w_cum     AS cum_targets,
    SUM(fumbles)  OVER w_cum     AS cum_fumbles,
    SUM(total_td) OVER w_cum     AS cum_td,
    COUNT(*) OVER w_cum          AS games_played,

    -- 3 / 5 / 10 game rolling windows through current game
    SUM(rush_att) OVER w3   AS rush_att_3,  SUM(rush_att) OVER w5   AS rush_att_5,  SUM(rush_att) OVER w10   AS rush_att_10,
    SUM(rush_yds) OVER w3   AS rush_yds_3,  SUM(rush_yds) OVER w5   AS rush_yds_5,  SUM(rush_yds) OVER w10   AS rush_yds_10,
    SUM(rush_td)  OVER w3   AS rush_td_3,   SUM(rush_td)  OVER w5   AS rush_td_5,   SUM(rush_td)  OVER w10   AS rush_td_10,
    SUM(receptions) OVER w3 AS rec_3,       SUM(receptions) OVER w5 AS rec_5,       SUM(receptions) OVER w10 AS rec_10,
    SUM(recv_yds) OVER w3   AS recv_yds_3,  SUM(recv_yds) OVER w5   AS recv_yds_5,  SUM(recv_yds) OVER w10   AS recv_yds_10,
    SUM(recv_td)  OVER w3   AS recv_td_3,   SUM(recv_td)  OVER w5   AS recv_td_5,   SUM(recv_td)  OVER w10   AS recv_td_10,
    SUM(targets)  OVER w3   AS targets_3,   SUM(targets)  OVER w5   AS targets_5,   SUM(targets)  OVER w10   AS targets_10,
    SUM(fumbles)  OVER w3   AS fumbles_3,   SUM(fumbles)  OVER w5   AS fumbles_5,   SUM(fumbles)  OVER w10   AS fumbles_10,
    SUM(total_td) OVER w3   AS td_3,        SUM(total_td) OVER w5   AS td_5,        SUM(total_td) OVER w10   AS td_10,
    COUNT(*) OVER w3   AS games_3,
    COUNT(*) OVER w5   AS games_5,
    COUNT(*) OVER w10  AS games_10
FROM skill_games
WHERE game_type IN ('REG', 'POST')
WINDOW w_cum AS (PARTITION BY player_id, season ORDER BY game_date, game_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       w3  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
       w5  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
       w10 AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
ON CONFLICT (player_id, season, game_id, game_type) DO UPDATE SET
    week = EXCLUDED.week,
    position = EXCLUDED.position,
    team_abbr = EXCLUDED.team_abbr,
    opponent_abbr = EXCLUDED.opponent_abbr,
    game_date = EXCLUDED.game_date,
    rush_attempts = EXCLUDED.rush_attempts,
    rush_yards = EXCLUDED.rush_yards,
    rush_tds = EXCLUDED.rush_tds,
    rush_long = EXCLUDED.rush_long,
    targets = EXCLUDED.targets,
    receptions = EXCLUDED.receptions,
    receiving_yards = EXCLUDED.receiving_yards,
    receiving_tds = EXCLUDED.receiving_tds,
    receiving_long = EXCLUDED.receiving_long,
    fumbles = EXCLUDED.fumbles,
    fumbles_lost = EXCLUDED.fumbles_lost,
    total_tds = EXCLUDED.total_tds,
    cum_rush_att = EXCLUDED.cum_rush_att,
    cum_rush_yds = EXCLUDED.cum_rush_yds,
    cum_rush_td = EXCLUDED.cum_rush_td,
    cum_rec = EXCLUDED.cum_rec,
    cum_recv_yds = EXCLUDED.cum_recv_yds,
    cum_recv_td = EXCLUDED.cum_recv_td,
    cum_targets = EXCLUDED.cum_targets,
    cum_fumbles = EXCLUDED.cum_fumbles,
    cum_td = EXCLUDED.cum_td,
    games_played = EXCLUDED.games_played,
    rush_att_3 = EXCLUDED.rush_att_3, rush_att_5 = EXCLUDED.rush_att_5, rush_att_10 = EXCLUDED.rush_att_10,
    rush_yds_3 = EXCLUDED.rush_yds_3, rush_yds_5 = EXCLUDED.rush_yds_5, rush_yds_10 = EXCLUDED.rush_yds_10,
    rush_td_3 = EXCLUDED.rush_td_3, rush_td_5 = EXCLUDED.rush_td_5, rush_td_10 = EXCLUDED.rush_td_10,
    rec_3 = EXCLUDED.rec_3, rec_5 = EXCLUDED.rec_5, rec_10 = EXCLUDED.rec_10,
    recv_yds_3 = EXCLUDED.recv_yds_3, recv_yds_5 = EXCLUDED.recv_yds_5, recv_yds_10 = EXCLUDED.recv_yds_10,
    recv_td_3 = EXCLUDED.recv_td_3, recv_td_5 = EXCLUDED.recv_td_5, recv_td_10 = EXCLUDED.recv_td_10,
    targets_3 = EXCLUDED.targets_3, targets_5 = EXCLUDED.targets_5, targets_10 = EXCLUDED.targets_10,
    fumbles_3 = EXCLUDED.fumbles_3, fumbles_5 = EXCLUDED.fumbles_5, fumbles_10 = EXCLUDED.fumbles_10,
    td_3 = EXCLUDED.td_3, td_5 = EXCLUDED.td_5, td_10 = EXCLUDED.td_10,
    games_3 = EXCLUDED.games_3, games_5 = EXCLUDED.games_5, games_10 = EXCLUDED.games_10;
"""


# ═══════════════════════════════════════════════════════════════════════════════
#  Kicker Rolling Stats DDL  —  per-game raw + cumulative + 3/5/10 windows
# ═══════════════════════════════════════════════════════════════════════════════

CREATE_KICKER_SQL = """
CREATE TABLE IF NOT EXISTS nfl.kicker_rolling_stats (
    player_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    game_id         INTEGER NOT NULL,
    game_type       VARCHAR(10) NOT NULL DEFAULT 'REG',
    week            INTEGER NOT NULL,
    position        VARCHAR(5) NOT NULL,
    team_abbr       TEXT NOT NULL,
    opponent_abbr   TEXT,
    game_date       DATE,

    -- Per-game raw events
    fg_made         DOUBLE PRECISION DEFAULT 0,
    fg_attempted    DOUBLE PRECISION DEFAULT 0,
    xp_made         DOUBLE PRECISION DEFAULT 0,
    xp_attempted    DOUBLE PRECISION DEFAULT 0,

    -- Cumulative through current game
    cum_fg_made     DOUBLE PRECISION,
    cum_fg_att      DOUBLE PRECISION,
    cum_xp_made     DOUBLE PRECISION,
    cum_xp_att      DOUBLE PRECISION,
    games_played    INTEGER,

    -- 3 / 5 / 10 game rolling windows through current game
    fg_made_3       DOUBLE PRECISION, fg_made_5       DOUBLE PRECISION, fg_made_10       DOUBLE PRECISION,
    fg_att_3        DOUBLE PRECISION, fg_att_5        DOUBLE PRECISION, fg_att_10        DOUBLE PRECISION,
    xp_made_3       DOUBLE PRECISION, xp_made_5       DOUBLE PRECISION, xp_made_10       DOUBLE PRECISION,
    xp_att_3        DOUBLE PRECISION, xp_att_5        DOUBLE PRECISION, xp_att_10        DOUBLE PRECISION,
    games_3         INTEGER,
    games_5         INTEGER,
    games_10        INTEGER,

    PRIMARY KEY (player_id, season, game_id, game_type)
);
"""

# ═══════════════════════════════════════════════════════════════════════════════
#  Kicker Source CTE  —  position K from player_weekly_stats
# ═══════════════════════════════════════════════════════════════════════════════

KICKER_SOURCE_CTE = """
WITH kicker_games AS (
    SELECT
        pws.player_id,
        s.year       AS season,
        g.id         AS game_id,
        g.week,
        p.position,
        t.abbreviation    AS team_abbr,
        ot.abbreviation   AS opponent_abbr,
        g.date       AS game_date,
        COALESCE(pws.field_goals_made::NUMERIC, 0)      AS fg_made,
        COALESCE(pws.field_goals_attempted::NUMERIC, 0) AS fg_att,
        COALESCE(pws.extra_points_made::NUMERIC, 0)     AS xp_made,
        COALESCE(pws.extra_points_attempted::NUMERIC, 0) AS xp_att,
        g.game_type                                      AS game_type
    FROM nfl.player_weekly_stats pws
    JOIN nfl.games g     ON g.id    = pws.game_id
    JOIN nfl.seasons s   ON s.id    = pws.season_id
    JOIN nfl.teams t     ON t.id    = pws.team_id
    JOIN nfl.teams ot    ON ot.id   = pws.opponent_id
    JOIN nfl.players p   ON p.id    = pws.player_id
    WHERE p.position = 'K'
      AND pws.game_id IS NOT NULL
      AND s.year IS NOT NULL
      AND g.game_type IN ('REG', 'POST')  -- include playoffs so postseason rolls carry into them
)
"""

POPULATE_KICKER_SQL = KICKER_SOURCE_CTE + """
INSERT INTO nfl.kicker_rolling_stats (
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    fg_made, fg_attempted, xp_made, xp_attempted,
    cum_fg_made, cum_fg_att, cum_xp_made, cum_xp_att, games_played,
    fg_made_3, fg_made_5, fg_made_10,
    fg_att_3, fg_att_5, fg_att_10,
    xp_made_3, xp_made_5, xp_made_10,
    xp_att_3, xp_att_5, xp_att_10,
    games_3, games_5, games_10
)
SELECT
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    fg_made, fg_att, xp_made, xp_att,

    SUM(fg_made) OVER w_cum  AS cum_fg_made,
    SUM(fg_att)  OVER w_cum  AS cum_fg_att,
    SUM(xp_made) OVER w_cum  AS cum_xp_made,
    SUM(xp_att)  OVER w_cum  AS cum_xp_att,
    COUNT(*) OVER w_cum      AS games_played,

    SUM(fg_made) OVER w3  AS fg_made_3,  SUM(fg_made) OVER w5  AS fg_made_5,  SUM(fg_made) OVER w10  AS fg_made_10,
    SUM(fg_att)  OVER w3  AS fg_att_3,   SUM(fg_att)  OVER w5  AS fg_att_5,   SUM(fg_att)  OVER w10  AS fg_att_10,
    SUM(xp_made) OVER w3  AS xp_made_3,  SUM(xp_made) OVER w5  AS xp_made_5,  SUM(xp_made) OVER w10  AS xp_made_10,
    SUM(xp_att)  OVER w3  AS xp_att_3,   SUM(xp_att)  OVER w5  AS xp_att_5,   SUM(xp_att)  OVER w10  AS xp_att_10,
    COUNT(*) OVER w3  AS games_3,
    COUNT(*) OVER w5  AS games_5,
    COUNT(*) OVER w10 AS games_10
FROM kicker_games
WHERE game_type IN ('REG', 'POST')
WINDOW w_cum AS (PARTITION BY player_id, season ORDER BY game_date, game_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       w3  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
       w5  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
       w10 AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
ON CONFLICT (player_id, season, game_id, game_type) DO UPDATE SET
    week = EXCLUDED.week,
    position = EXCLUDED.position,
    team_abbr = EXCLUDED.team_abbr,
    opponent_abbr = EXCLUDED.opponent_abbr,
    game_date = EXCLUDED.game_date,
    fg_made = EXCLUDED.fg_made,
    fg_attempted = EXCLUDED.fg_attempted,
    xp_made = EXCLUDED.xp_made,
    xp_attempted = EXCLUDED.xp_attempted,
    cum_fg_made = EXCLUDED.cum_fg_made,
    cum_fg_att = EXCLUDED.cum_fg_att,
    cum_xp_made = EXCLUDED.cum_xp_made,
    cum_xp_att = EXCLUDED.cum_xp_att,
    games_played = EXCLUDED.games_played,
    fg_made_3 = EXCLUDED.fg_made_3, fg_made_5 = EXCLUDED.fg_made_5, fg_made_10 = EXCLUDED.fg_made_10,
    fg_att_3 = EXCLUDED.fg_att_3, fg_att_5 = EXCLUDED.fg_att_5, fg_att_10 = EXCLUDED.fg_att_10,
    xp_made_3 = EXCLUDED.xp_made_3, xp_made_5 = EXCLUDED.xp_made_5, xp_made_10 = EXCLUDED.xp_made_10,
    xp_att_3 = EXCLUDED.xp_att_3, xp_att_5 = EXCLUDED.xp_att_5, xp_att_10 = EXCLUDED.xp_att_10,
    games_3 = EXCLUDED.games_3, games_5 = EXCLUDED.games_5, games_10 = EXCLUDED.games_10;
"""


def ensure_tables(engine):
    with engine.begin() as conn:
        conn.execute(text(CREATE_SKILL_SQL))
        conn.execute(text(CREATE_KICKER_SQL))
    logger.info("Ensured nfl.skill_rolling_stats + nfl.kicker_rolling_stats tables exist")


def populate_skill_rolling_tables(
    engine=None,
    seasons: list[int] | None = None,
    game_type: str = "REG",
) -> dict:
    """Populate nfl.skill_rolling_stats (and kicker_rolling_stats).

    Args:
        engine: SQLAlchemy sync engine. If None, creates one (and disposes it).
        seasons: List of seasons (years) to process. None = all available.
        game_type: kept for backward-compat; ignored. Rolling rows are always built
            over REG+POST so playoff games carry the season's regular-season history
            ("rolling into the postseason"). Preseason (PRE) rows never enter the
            windows from player_weekly_stats (which has no PRE rows).

    Returns:
        dict with inserted row count.
    """
    if engine is None:
        engine = create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
        _owns_engine = True
    else:
        _owns_engine = False

    result = {"skill_rolling": 0, "kicker_rolling": 0}

    try:
        ensure_tables(engine)

        def _apply_season(sql_text):
            if not seasons:
                return sql_text
            season_list = ", ".join(str(s) for s in seasons)
            # anchor on the game_type WHERE that follows FROM ..._games in both SQL blocks
            return sql_text.replace(
                "WHERE game_type IN ('REG', 'POST')",
                f"WHERE game_type IN ('REG', 'POST')\n  AND season IN ({season_list})",
            )

        with engine.begin() as conn:
            logger.info("Running skill rolling stats population (REG+POST, playoffs roll in)...")
            r = conn.execute(text(_apply_season(POPULATE_SKILL_SQL)))
            result["skill_rolling"] = r.rowcount
            logger.info("Inserted/updated %d skill rolling stat rows", r.rowcount)

            logger.info("Running kicker rolling stats population (REG+POST, playoffs roll in)...")
            r = conn.execute(text(_apply_season(POPULATE_KICKER_SQL)))
            result["kicker_rolling"] = r.rowcount
            logger.info("Inserted/updated %d kicker rolling stat rows", r.rowcount)

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
                     help="Which game_type to compute skill rolling stats for (default REG)")
    _ap.add_argument("--seasons", nargs="*", type=int, default=None,
                     help="Season years to process (default all)")
    _args = _ap.parse_args()

    result = populate_skill_rolling_tables(game_type=_args.game_type, seasons=_args.seasons)
    logger.info("Done — skill_rolling: %d, kicker_rolling: %d", result["skill_rolling"], result["kicker_rolling"])


if __name__ == "__main__":
    main()
