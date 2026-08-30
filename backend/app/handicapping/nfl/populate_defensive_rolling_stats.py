#!/usr/bin/env python3
"""
Populate nfl.defensive_rolling_stats — per-game DEFENSIVE player stats with
cumulative (season-to-date) and rolling (3/5/10-game) windows from
nfl.player_weekly_stats, mirroring the skill/kicker rolling pattern.

Convention (per MEMORY.md hard rule): each row INCLUDES that row's own game
(INCLUSIVE). Windows use `ROWS BETWEEN 2/4/9 PRECEDING AND CURRENT ROW`; the
season-to-date column uses `UNBOUNDED PRECEDING AND CURRENT ROW`. Leak-safety
is the consumption side's job (reads the prior row).

A player is included if they hold a defensive position (CB, DE, DL, DT, LB, S)
OR recorded any defensive stat in that game (so a DB/two-way or unclassified
player with tackles/INT/etc. is never dropped).

Usage:
    python app/handicapping/nfl/populate_defensive_rolling_stats.py [--game-type REG] [--seasons ...]
"""

import logging

import psycopg2
from sqlalchemy import create_engine, text

from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

DEF_POSITIONS = ("CB", "DE", "DL", "DT", "LB", "S")

# ────────────────────────────────────────────────────────────────────────────────
#  Defensive Source CTE
# ────────────────────────────────────────────────────────────────────────────────
DEF_SOURCE_CTE = """
WITH def_games AS (
    SELECT
        pws.player_id,
        s.year        AS season,
        g.id          AS game_id,
        g.week,
        p.position,
        t.abbreviation    AS team_abbr,
        ot.abbreviation   AS opponent_abbr,
        g.date        AS game_date,
        COALESCE(pws.tackles_solo::NUMERIC, 0)      AS solo,
        COALESCE(pws.tackles_assist::NUMERIC, 0)    AS ast,
        COALESCE(pws.tackles_combined::NUMERIC, 0)  AS tack,
        COALESCE(pws.tackles_for_loss::NUMERIC, 0)  AS tfl,
        COALESCE(pws.sacks::NUMERIC, 0)             AS sacks,
        COALESCE(pws.sacks_assisted::NUMERIC, 0)    AS sacks_assisted,
        COALESCE(pws.sacks_unassisted::NUMERIC, 0)  AS sacks_unassisted,
        COALESCE(pws.qb_hits::NUMERIC, 0)           AS qb_hits,
        COALESCE(pws.hurries::NUMERIC, 0)           AS hurries,
        COALESCE(pws.stuffs::NUMERIC, 0)            AS stuffs,
        COALESCE(pws.passes_defended::NUMERIC, 0)   AS pd,
        COALESCE(pws.passes_batted_down::NUMERIC,0) AS pbd,
        COALESCE(pws.interceptions::NUMERIC, 0)     AS interceptions,
        COALESCE(pws.interception_yards::NUMERIC,0) AS int_yds,
        COALESCE(pws.interception_tds::NUMERIC, 0)  AS int_td,
        COALESCE(pws.fumbles_forced::NUMERIC, 0)    AS ff,
        COALESCE(pws.fumbles_recovered::NUMERIC, 0) AS fr,
        COALESCE(pws.safeties::NUMERIC, 0)          AS safeties,
        COALESCE(pws.defensive_tds::NUMERIC, 0)     AS def_td,
        (COALESCE(pws.tackles_combined::NUMERIC,0) +
         COALESCE(pws.sacks::NUMERIC,0))            AS tack_sack,
        -- flag: any defensive stat recorded (so we never drop a two-way player)
        (COALESCE(pws.tackles_solo,0)+COALESCE(pws.tackles_assist,0)+
         COALESCE(pws.tackles_combined,0)+COALESCE(pws.tackles_for_loss,0)+
         COALESCE(pws.sacks,0)+COALESCE(pws.qb_hits,0)+COALESCE(pws.hurries,0)+
         COALESCE(pws.passes_defended,0)+COALESCE(pws.interceptions,0)+
         COALESCE(pws.fumbles_forced,0)+COALESCE(pws.fumbles_recovered,0)+
         COALESCE(pws.safeties,0)+COALESCE(pws.defensive_tds,0)) > 0 AS has_def,
        g.game_type                                 AS game_type
    FROM nfl.player_weekly_stats pws
    JOIN nfl.games g     ON g.id    = pws.game_id
    JOIN nfl.seasons s   ON s.id    = pws.season_id
    JOIN nfl.teams t     ON t.id    = pws.team_id
    JOIN nfl.teams ot    ON ot.id   = pws.opponent_id
    JOIN nfl.players p   ON p.id    = pws.player_id
    WHERE (p.position IN ('CB','DE','DL','DT','LB','S')
           OR (COALESCE(pws.tackles_solo,0)+COALESCE(pws.tackles_assist,0)+
               COALESCE(pws.tackles_combined,0)+COALESCE(pws.tackles_for_loss,0)+
               COALESCE(pws.sacks,0)+COALESCE(pws.qb_hits,0)+COALESCE(pws.hurries,0)+
               COALESCE(pws.passes_defended,0)+COALESCE(pws.interceptions,0)+
               COALESCE(pws.fumbles_forced,0)+COALESCE(pws.fumbles_recovered,0)+
               COALESCE(pws.safeties,0)+COALESCE(pws.defensive_tds,0)) > 0)
      AND pws.game_id IS NOT NULL
      AND s.year IS NOT NULL
      AND g.game_type IN ('REG', 'POST')  -- include playoffs so postseason rolls carry into them
)
"""


POPULATE_DEF_SQL = DEF_SOURCE_CTE + """
INSERT INTO nfl.defensive_rolling_stats (
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    solo, assist, tackles, tfl, sacks, sacks_assisted, sacks_unassisted, qb_hits, hurries,
    stuffs, passes_defended, passes_batted_down, interceptions, int_yards, int_tds,
    fumbles_forced, fumbles_recovered, safeties, defensive_tds,
    cum_tackles, cum_sacks, cum_interceptions, cum_ff, cum_fr, cum_qb_hits, cum_pd, cum_tfl,
    games_played,
    tackles_3, tackles_5, tackles_10,
    sacks_3, sacks_5, sacks_10,
    interceptions_3, interceptions_5, interceptions_10,
    ff_3, ff_5, ff_10,
    fr_3, fr_5, fr_10,
    qb_hits_3, qb_hits_5, qb_hits_10,
    pd_3, pd_5, pd_10,
    tfl_3, tfl_5, tfl_10,
    games_3, games_5, games_10
)
SELECT
    player_id, season, game_id, game_type, week, position, team_abbr, opponent_abbr, game_date,
    solo, ast, tack, tfl, sacks, sacks_assisted, sacks_unassisted, qb_hits, hurries,
    stuffs, pd, pbd, interceptions, int_yds, int_td,
    ff, fr, safeties, def_td,
    SUM(tack)       OVER w_cum AS cum_tackles,
    SUM(sacks)      OVER w_cum AS cum_sacks,
    SUM(interceptions) OVER w_cum AS cum_interceptions,
    SUM(ff)         OVER w_cum AS cum_ff,
    SUM(fr)         OVER w_cum AS cum_fr,
    SUM(qb_hits)    OVER w_cum AS cum_qb_hits,
    SUM(pd)         OVER w_cum AS cum_pd,
    SUM(tfl)        OVER w_cum AS cum_tfl,
    COUNT(*)        OVER w_cum AS games_played,
    SUM(tack)  OVER w3  AS tackles_3,  SUM(tack)  OVER w5  AS tackles_5,  SUM(tack)  OVER w10  AS tackles_10,
    SUM(sacks) OVER w3  AS sacks_3,    SUM(sacks) OVER w5  AS sacks_5,    SUM(sacks) OVER w10  AS sacks_10,
    SUM(interceptions) OVER w3  AS interceptions_3, SUM(interceptions) OVER w5  AS interceptions_5, SUM(interceptions) OVER w10  AS interceptions_10,
    SUM(ff) OVER w3  AS ff_3,    SUM(ff) OVER w5  AS ff_5,    SUM(ff) OVER w10  AS ff_10,
    SUM(fr) OVER w3  AS fr_3,    SUM(fr) OVER w5  AS fr_5,    SUM(fr) OVER w10  AS fr_10,
    SUM(qb_hits) OVER w3  AS qb_hits_3, SUM(qb_hits) OVER w5  AS qb_hits_5, SUM(qb_hits) OVER w10  AS qb_hits_10,
    SUM(pd) OVER w3  AS pd_3,    SUM(pd) OVER w5  AS pd_5,    SUM(pd) OVER w10  AS pd_10,
    SUM(tfl) OVER w3  AS tfl_3,   SUM(tfl) OVER w5  AS tfl_5,  SUM(tfl) OVER w10  AS tfl_10,
    COUNT(*) OVER w3  AS games_3,
    COUNT(*) OVER w5  AS games_5,
    COUNT(*) OVER w10 AS games_10
FROM def_games
WINDOW w_cum AS (PARTITION BY player_id, season ORDER BY game_date, game_id
                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       w3  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 2 PRECEDING AND CURRENT ROW),
       w5  AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
       w10 AS (PARTITION BY player_id, season ORDER BY game_date, game_id
               ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
ON CONFLICT (player_id, season, game_id, game_type) DO UPDATE SET
    week = EXCLUDED.week, position = EXCLUDED.position,
    team_abbr = EXCLUDED.team_abbr, opponent_abbr = EXCLUDED.opponent_abbr,
    game_date = EXCLUDED.game_date,
    solo = EXCLUDED.solo, assist = EXCLUDED.assist, tackles = EXCLUDED.tackles,
    tfl = EXCLUDED.tfl, sacks = EXCLUDED.sacks,
    sacks_assisted = EXCLUDED.sacks_assisted, sacks_unassisted = EXCLUDED.sacks_unassisted,
    qb_hits = EXCLUDED.qb_hits, hurries = EXCLUDED.hurries, stuffs = EXCLUDED.stuffs,
    passes_defended = EXCLUDED.passes_defended, passes_batted_down = EXCLUDED.passes_batted_down,
    interceptions = EXCLUDED.interceptions, int_yards = EXCLUDED.int_yards, int_tds = EXCLUDED.int_tds,
    fumbles_forced = EXCLUDED.fumbles_forced, fumbles_recovered = EXCLUDED.fumbles_recovered,
    safeties = EXCLUDED.safeties, defensive_tds = EXCLUDED.defensive_tds,
    cum_tackles = EXCLUDED.cum_tackles, cum_sacks = EXCLUDED.cum_sacks,
    cum_interceptions = EXCLUDED.cum_interceptions, cum_ff = EXCLUDED.cum_ff,
    cum_fr = EXCLUDED.cum_fr, cum_qb_hits = EXCLUDED.cum_qb_hits,
    cum_pd = EXCLUDED.cum_pd, cum_tfl = EXCLUDED.cum_tfl, games_played = EXCLUDED.games_played,
    tackles_3 = EXCLUDED.tackles_3, tackles_5 = EXCLUDED.tackles_5, tackles_10 = EXCLUDED.tackles_10,
    sacks_3 = EXCLUDED.sacks_3, sacks_5 = EXCLUDED.sacks_5, sacks_10 = EXCLUDED.sacks_10,
    interceptions_3 = EXCLUDED.interceptions_3, interceptions_5 = EXCLUDED.interceptions_5, interceptions_10 = EXCLUDED.interceptions_10,
    ff_3 = EXCLUDED.ff_3, ff_5 = EXCLUDED.ff_5, ff_10 = EXCLUDED.ff_10,
    fr_3 = EXCLUDED.fr_3, fr_5 = EXCLUDED.fr_5, fr_10 = EXCLUDED.fr_10,
    qb_hits_3 = EXCLUDED.qb_hits_3, qb_hits_5 = EXCLUDED.qb_hits_5, qb_hits_10 = EXCLUDED.qb_hits_10,
    pd_3 = EXCLUDED.pd_3, pd_5 = EXCLUDED.pd_5, pd_10 = EXCLUDED.pd_10,
    tfl_3 = EXCLUDED.tfl_3, tfl_5 = EXCLUDED.tfl_5, tfl_10 = EXCLUDED.tfl_10,
    games_3 = EXCLUDED.games_3, games_5 = EXCLUDED.games_5, games_10 = EXCLUDED.games_10;
"""


CREATE_DEF_SQL = """
CREATE TABLE IF NOT EXISTS nfl.defensive_rolling_stats (
    player_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    game_id     INTEGER NOT NULL,
    game_type   TEXT    NOT NULL DEFAULT 'REG',
    week        INTEGER,
    position    TEXT,
    team_abbr   TEXT,
    opponent_abbr TEXT,
    game_date   DATE,
    -- per-game
    solo        NUMERIC DEFAULT 0,
    assist      NUMERIC DEFAULT 0,
    tackles     NUMERIC DEFAULT 0,
    tfl         NUMERIC DEFAULT 0,
    sacks       NUMERIC DEFAULT 0,
    sacks_assisted NUMERIC DEFAULT 0,
    sacks_unassisted NUMERIC DEFAULT 0,
    qb_hits     NUMERIC DEFAULT 0,
    hurries     NUMERIC DEFAULT 0,
    stuffs      NUMERIC DEFAULT 0,
    passes_defended NUMERIC DEFAULT 0,
    passes_batted_down NUMERIC DEFAULT 0,
    interceptions NUMERIC DEFAULT 0,
    int_yards   NUMERIC DEFAULT 0,
    int_tds     NUMERIC DEFAULT 0,
    fumbles_forced NUMERIC DEFAULT 0,
    fumbles_recovered NUMERIC DEFAULT 0,
    safeties    NUMERIC DEFAULT 0,
    defensive_tds NUMERIC DEFAULT 0,
    -- cumulative
    cum_tackles NUMERIC DEFAULT 0,
    cum_sacks   NUMERIC DEFAULT 0,
    cum_interceptions NUMERIC DEFAULT 0,
    cum_ff      NUMERIC DEFAULT 0,
    cum_fr      NUMERIC DEFAULT 0,
    cum_qb_hits NUMERIC DEFAULT 0,
    cum_pd      NUMERIC DEFAULT 0,
    cum_tfl     NUMERIC DEFAULT 0,
    games_played INTEGER DEFAULT 0,
    -- rolling
    tackles_3 NUMERIC DEFAULT 0, tackles_5 NUMERIC DEFAULT 0, tackles_10 NUMERIC DEFAULT 0,
    sacks_3   NUMERIC DEFAULT 0, sacks_5   NUMERIC DEFAULT 0, sacks_10   NUMERIC DEFAULT 0,
    interceptions_3 NUMERIC DEFAULT 0, interceptions_5 NUMERIC DEFAULT 0, interceptions_10 NUMERIC DEFAULT 0,
    ff_3 NUMERIC DEFAULT 0, ff_5 NUMERIC DEFAULT 0, ff_10 NUMERIC DEFAULT 0,
    fr_3 NUMERIC DEFAULT 0, fr_5 NUMERIC DEFAULT 0, fr_10 NUMERIC DEFAULT 0,
    qb_hits_3 NUMERIC DEFAULT 0, qb_hits_5 NUMERIC DEFAULT 0, qb_hits_10 NUMERIC DEFAULT 0,
    pd_3 NUMERIC DEFAULT 0, pd_5 NUMERIC DEFAULT 0, pd_10 NUMERIC DEFAULT 0,
    tfl_3 NUMERIC DEFAULT 0, tfl_5 NUMERIC DEFAULT 0, tfl_10 NUMERIC DEFAULT 0,
    games_3 INTEGER DEFAULT 0, games_5 INTEGER DEFAULT 0, games_10 INTEGER DEFAULT 0,
    PRIMARY KEY (player_id, season, game_id, game_type)
);
CREATE INDEX IF NOT EXISTS ix_def_rolling_player_season ON nfl.defensive_rolling_stats (player_id, season, game_type, game_date);
CREATE INDEX IF NOT EXISTS ix_def_rolling_team_week ON nfl.defensive_rolling_stats (team_abbr, season, week, game_type);
"""


def _apply_season(sql: str) -> str:
    """No-op placeholder for parity with the skill builder's season filter hook."""
    return sql


def ensure_defensive_table(engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(CREATE_DEF_SQL))
    logger.info("Ensured nfl.defensive_rolling_stats table exists")


def populate_defensive_rolling_stats(
    engine=None,
    seasons: list[int] | None = None,
    game_type: str = "REG",
) -> dict:
    """Populate nfl.defensive_rolling_stats (REG+POST, playoffs roll into postseason).

    Args:
        engine: SQLAlchemy sync engine. If None, creates one.
        seasons: List of seasons to process. None = all available.
        game_type: kept for backward-compat; ignored. Rows are built over REG+POST so
            playoff games carry the season's regular-season history. Preseason (PRE)
            never enters (player_weekly_stats has no PRE rows).
    """
    if engine is None:
        engine = create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
        _owns_engine = True
    else:
        _owns_engine = False

    try:
        ensure_defensive_table(engine)
        with engine.begin() as conn:
            # re-clear the REG+POST rows being rebuilt (keep PRE untouched if any)
            if seasons:
                conn.execute(text(
                    "DELETE FROM nfl.defensive_rolling_stats WHERE game_type IN ('REG','POST') "
                    "AND season IN :seasons"),
                    {"seasons": tuple(seasons)})
            else:
                conn.execute(text(
                    "DELETE FROM nfl.defensive_rolling_stats WHERE game_type IN ('REG','POST')"))
            result = conn.execute(text(_apply_season(POPULATE_DEF_SQL)))
            count = result.rowcount
            logger.info("Inserted/updated %d defensive rolling stat rows (REG+POST, playoffs roll in)", count)
        return {"defensive_rolling": count}
    finally:
        if _owns_engine:
            engine.dispose()


def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--game-type", default="REG", choices=["REG", "PRE", "POST"])
    ap.add_argument("--seasons", nargs="*", type=int, default=None)
    args = ap.parse_args()
    result = populate_defensive_rolling_stats(game_type=args.game_type, seasons=args.seasons)
    logger.info("Done — defensive_rolling: %d", result["defensive_rolling"])


if __name__ == "__main__":
    main()
