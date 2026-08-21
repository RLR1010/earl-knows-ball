"""
Build mlb.player_batting_rolling_stats — per-hitter cumulative/rolling stats.

One row per (player, game) the batter appeared in a FINAL game. Windows use
ROWS BETWEEN ... AND CURRENT ROW, so each row INCLUDES its own game's result
(matches the team_rolling_stats / pitcher_rolling_stats contract). The data
loader reads the PREVIOUS FINAL row per player (leak-safe).

Usage:
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/handicapping/mlb/populate_batting_rolling.py
    ./venv/bin/python app/handicapping/mlb/populate_batting_rolling.py --seasons 19 20 2026
"""
import argparse
import logging
import re
import sys
import time
from pathlib import Path

# Repo root (backend/) on path so `import app...` resolves
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))  # backend/ (repo root with app/)

from sqlalchemy import create_engine, text

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("earl.batting_rolling")
DATABASE_URL = settings.database_url_sync

HERE = Path(__file__).parent

# Schema DDL block for the player batting rolling table (from rolling_stats.sql)
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS mlb.player_batting_rolling_stats (
    game_id        INTEGER NOT NULL,
    player_id      INTEGER NOT NULL,
    team_id        INTEGER,
    team_side      TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id      INTEGER NOT NULL,
    game_date      TIMESTAMPTZ NOT NULL,
    game_n         INTEGER,
    pa             INTEGER,
    at_bats        INTEGER,
    runs           INTEGER,
    hits           INTEGER,
    doubles        INTEGER,
    triples        INTEGER,
    home_runs      INTEGER,
    runs_batted_in INTEGER,
    walks          INTEGER,
    strikeouts     INTEGER,
    total_bases    INTEGER,
    hit_by_pitch   INTEGER,
    sacrifice_flies INTEGER,
    avg_this   DOUBLE PRECISION,
    obp_this   DOUBLE PRECISION,
    slg_this   DOUBLE PRECISION,
    ops_this   DOUBLE PRECISION,
    ytd_games   INTEGER,
    ytd_pa      INTEGER,
    ytd_ab      INTEGER,
    ytd_hits    INTEGER,
    ytd_bb      INTEGER,
    ytd_hbp     INTEGER,
    ytd_sf      INTEGER,
    ytd_runs    INTEGER,
    ytd_rbi     INTEGER,
    ytd_tb      INTEGER,
    ytd_hr      INTEGER,
    ytd_so      INTEGER,
    ytd_avg     DOUBLE PRECISION,
    ytd_obp     DOUBLE PRECISION,
    ytd_slg     DOUBLE PRECISION,
    ytd_ops     DOUBLE PRECISION,
    avg_5       DOUBLE PRECISION,
    obp_5       DOUBLE PRECISION,
    slg_5       DOUBLE PRECISION,
    ops_5       DOUBLE PRECISION,
    avg_15      DOUBLE PRECISION,
    obp_15      DOUBLE PRECISION,
    slg_15      DOUBLE PRECISION,
    ops_15      DOUBLE PRECISION,
    avg_30      DOUBLE PRECISION,
    obp_30      DOUBLE PRECISION,
    slg_30      DOUBLE PRECISION,
    ops_30      DOUBLE PRECISION,
    prev_game_id          INTEGER,
    prev_game_date        TIMESTAMPTZ,
    prev_game_id_season   INTEGER,
    prev_game_date_season TIMESTAMPTZ,
    PRIMARY KEY (player_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_pbrs_player_season
    ON mlb.player_batting_rolling_stats (player_id, season_id, game_date);
CREATE INDEX IF NOT EXISTS idx_pbrs_game
    ON mlb.player_batting_rolling_stats (game_id);

-- Add columns introduced after the table's initial creation. CREATE TABLE IF
-- NOT EXISTS above is a no-op on an existing table, so these keep schema synced
-- on rebuild for tables created before ytd_runs/ytd_rbi existed.
ALTER TABLE mlb.player_batting_rolling_stats ADD COLUMN IF NOT EXISTS ytd_runs INTEGER;
ALTER TABLE mlb.player_batting_rolling_stats ADD COLUMN IF NOT EXISTS ytd_rbi INTEGER;
ALTER TABLE mlb.player_batting_rolling_stats
    ADD COLUMN IF NOT EXISTS prev_game_id INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER,
    ADD COLUMN IF NOT EXISTS prev_game_date_season TIMESTAMPTZ;
"""

COMPUTE_SQL = """
WITH base AS (
    SELECT
        b.game_id,
        b.player_id,
        b.team_side,
        g.home_team_id AS team_id,   -- side-agnostic placeholder (corrected below)
        g.season_id,
        g.date AS game_date,
        g.status,
        b.plate_appearances AS pa,
        b.at_bats,
        b.runs,
        b.hits,
        b.doubles,
        b.triples,
        b.home_runs,
        b.runs_batted_in,
        b.base_on_balls AS walks,
        b.strikeouts,
        b.total_bases,
        b.hit_by_pitch,
        b.sacrifice_flies
    FROM mlb.batting_game_stats b
    JOIN mlb.games g ON g.id = b.game_id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND b.player_id IS NOT NULL
      {season_predicate}
),
sided AS (
    SELECT *,
        CASE WHEN team_side = 'home' THEN team_id
             ELSE (SELECT away_team_id FROM mlb.games g2 WHERE g2.id = base.game_id) END AS team_id2
    FROM base
),
pergame AS (
    SELECT
        s.game_id, s.player_id, s.team_side, s.team_id2 AS team_id, s.season_id, s.game_date,
        s.pa, s.at_bats, s.runs, s.hits, s.doubles, s.triples, s.home_runs,
        s.runs_batted_in, s.walks, s.strikeouts, s.total_bases, s.hit_by_pitch, s.sacrifice_flies,
        CASE WHEN s.at_bats > 0 THEN s.hits::double precision / s.at_bats END AS avg_this,
        CASE WHEN (s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) > 0
             THEN (s.hits + s.walks + s.hit_by_pitch)::double precision
                  / (s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies) END AS obp_this,
        CASE WHEN s.at_bats > 0 THEN s.total_bases::double precision / s.at_bats END AS slg_this,
        CASE WHEN s.at_bats > 0
             THEN (s.hits + s.walks + s.hit_by_pitch)::double precision
                  / (s.at_bats + s.walks + s.hit_by_pitch + s.sacrifice_flies)
                  + s.total_bases::double precision / s.at_bats END AS ops_this,
        ROW_NUMBER() OVER (PARTITION BY s.player_id, s.season_id
                           ORDER BY s.game_date, s.game_id) AS game_n
    FROM sided s
)
SELECT
    p.game_id, p.player_id, p.team_id, p.team_side, p.season_id, p.game_date, p.game_n,
    p.pa, p.at_bats, p.runs, p.hits, p.doubles, p.triples, p.home_runs,
    p.runs_batted_in, p.walks, p.strikeouts, p.total_bases, p.hit_by_pitch, p.sacrifice_flies,
    p.avg_this, p.obp_this, p.slg_this, p.ops_this,

    -- Season-to-date (CURRENT ROW inclusive)
    count(*)      OVER w AS ytd_games,
    sum(p.pa)     OVER w AS ytd_pa,
    sum(p.at_bats) OVER w AS ytd_ab,
    sum(p.hits)   OVER w AS ytd_hits,
    sum(p.walks)  OVER w AS ytd_bb,
    sum(p.hit_by_pitch) OVER w AS ytd_hbp,
    sum(p.sacrifice_flies) OVER w AS ytd_sf,
    sum(p.runs) OVER w              AS ytd_runs,
    sum(p.runs_batted_in) OVER w    AS ytd_rbi,
    sum(p.total_bases) OVER w AS ytd_tb,
    sum(p.home_runs) OVER w AS ytd_hr,
    sum(p.strikeouts) OVER w AS ytd_so,
    CASE WHEN sum(p.at_bats) OVER w > 0
         THEN sum(p.hits) OVER w::double precision / sum(p.at_bats) OVER w END AS ytd_avg,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w END AS ytd_obp,
    CASE WHEN sum(p.at_bats) OVER w > 0
         THEN sum(p.total_bases) OVER w::double precision / sum(p.at_bats) OVER w END AS ytd_slg,
    -- ytd_ops = ytd_obp + ytd_slg
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w > 0
              AND sum(p.at_bats) OVER w > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w
              + sum(p.total_bases) OVER w::double precision / sum(p.at_bats) OVER w END AS ytd_ops,

    -- Rolling 5-game
    CASE WHEN sum(p.at_bats) OVER w5 > 0
         THEN sum(p.hits) OVER w5::double precision / sum(p.at_bats) OVER w5 END AS avg_5,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w5 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w5)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w5 END AS obp_5,
    CASE WHEN sum(p.at_bats) OVER w5 > 0
         THEN sum(p.total_bases) OVER w5::double precision / sum(p.at_bats) OVER w5 END AS slg_5,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w5 > 0
              AND sum(p.at_bats) OVER w5 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w5)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w5
              + sum(p.total_bases) OVER w5::double precision / sum(p.at_bats) OVER w5 END AS ops_5,

    -- Rolling 15-game
    CASE WHEN sum(p.at_bats) OVER w15 > 0
         THEN sum(p.hits) OVER w15::double precision / sum(p.at_bats) OVER w15 END AS avg_15,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w15 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w15)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w15 END AS obp_15,
    CASE WHEN sum(p.at_bats) OVER w15 > 0
         THEN sum(p.total_bases) OVER w15::double precision / sum(p.at_bats) OVER w15 END AS slg_15,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w15 > 0
              AND sum(p.at_bats) OVER w15 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w15)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w15
              + sum(p.total_bases) OVER w15::double precision / sum(p.at_bats) OVER w15 END AS ops_15,

    -- Rolling 30-game
    CASE WHEN sum(p.at_bats) OVER w30 > 0
         THEN sum(p.hits) OVER w30::double precision / sum(p.at_bats) OVER w30 END AS avg_30,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w30 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w30)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w30 END AS obp_30,
    CASE WHEN sum(p.at_bats) OVER w30 > 0
         THEN sum(p.total_bases) OVER w30::double precision / sum(p.at_bats) OVER w30 END AS slg_30,
    CASE WHEN sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w30 > 0
              AND sum(p.at_bats) OVER w30 > 0
         THEN (sum(p.hits + p.walks + p.hit_by_pitch) OVER w30)::double precision
              / sum(p.at_bats + p.walks + p.hit_by_pitch + p.sacrifice_flies) OVER w30
              + sum(p.total_bases) OVER w30::double precision / sum(p.at_bats) OVER w30 END AS ops_30,

    LAG(p.game_id) OVER w       AS prev_game_id_season,
    LAG(p.game_date) OVER w     AS prev_game_date_season
FROM pergame p
WINDOW w AS (PARTITION BY p.player_id, p.season_id ORDER BY p.game_date, p.game_id
             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
       w5  AS (PARTITION BY p.player_id, p.season_id ORDER BY p.game_date, p.game_id
             ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
       w15 AS (PARTITION BY p.player_id, p.season_id ORDER BY p.game_date, p.game_id
             ROWS BETWEEN 14 PRECEDING AND CURRENT ROW),
       w30 AS (PARTITION BY p.player_id, p.season_id ORDER BY p.game_date, p.game_id
             ROWS BETWEEN 29 PRECEDING AND CURRENT ROW)
ORDER BY p.player_id, p.game_date, p.game_id
"""


def _get_conn():
    import psycopg2
    from urllib.parse import urlparse
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=url.hostname, port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username, password=url.password,
    )


def ensure_table(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def populate(seasons=None, truncate=False, incremental=False):
    conn = _get_conn()
    try:
        ensure_table(conn)
        if truncate:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE mlb.player_batting_rolling_stats")
            conn.commit()
            logger.info("Truncated player_batting_rolling_stats")

        season_predicate = ""
        if seasons:
            season_predicate = (
                "AND g.season_id IN (" + ",".join(str(int(s)) for s in seasons) + ")"
            )

        if incremental and not seasons:
            # Windows are season-scoped (PARTITION BY player_id, season_id), so
            # computing the CURRENT season alone yields fully-correct ytd/rolling
            # for every row in it. Auto-target the latest season with FINAL games
            # to keep the incremental cheap (~35k rows vs 700k full history).
            cur = conn.cursor()
            cur.execute("""
                SELECT season_id FROM mlb.games
                WHERE status='FINAL' AND season_id BETWEEN 11 AND 21
                ORDER BY season_id DESC LIMIT 1
            """)
            row = cur.fetchone()
            cur.close()
            if row:
                seasons = [int(row[0])]
                logger.info("Incremental auto-targeting current season_id=%d", seasons[0])
        season_predicate = ""
        if seasons:
            season_predicate = (
                "AND g.season_id IN (" + ",".join(str(int(s)) for s in seasons) + ")"
            )

        sql = COMPUTE_SQL.format(season_predicate=season_predicate)

        if incremental:
            # Compute (season-scoped) so all window functions see their full
            # per-season history, but only emit rows for games not yet present
            # in the table. Existing rows keep their as-of-game ytd (which is
            # correct — ytd is deliberately as-of that game; the data_loader
            # reads the newest row for a scheduled game). ON CONFLICT DO NOTHING.
            sql = (
                "SELECT * FROM (\n"
                + sql.rstrip().rstrip(";")
                + "\n) AS full_calc\n"
                "WHERE NOT EXISTS (SELECT 1 FROM mlb.player_batting_rolling_stats t "
                "WHERE t.player_id = full_calc.player_id AND t.game_id = full_calc.game_id)"
            )
        t0 = time.time()
        with conn.cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()
            logger.info("Computed %d rows in %.1fs", len(rows), time.time() - t0)

            if not rows:
                conn.commit()
                return 0

            from psycopg2.extras import execute_values
            col_names = [d.name for d in cur.description]
            # column order matches table (minus id)
            insert_cols = [c for c in col_names]
            cols_str = ", ".join(insert_cols)
            execute_values(
                cur,
                f"INSERT INTO mlb.player_batting_rolling_stats ({cols_str}) "
                "VALUES %s ON CONFLICT DO NOTHING",
                rows,
                page_size=5000,
            )
            conn.commit()
            logger.info("Bulk-upserted %d rows", len(rows))
            return len(rows)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="*", default=None,
                    help="Restrict to season ids (e.g. 19 20 21). Default: all.")
    ap.add_argument("--truncate", action="store_true",
                    help="Truncate table before rebuild.")
    ap.add_argument("--incremental", action="store_true",
                    help="Only insert rows for games not yet in the table "
                         "(recomputes full history for correct windows).")
    args = ap.parse_args()
    n = populate(seasons=args.seasons, truncate=args.truncate, incremental=args.incremental)
    logger.info("DONE: %d rows", n)


if __name__ == "__main__":
    main()
