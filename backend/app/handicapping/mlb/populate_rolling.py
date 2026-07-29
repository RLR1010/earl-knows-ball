"""
Populate mlb.team_rolling_stats and mlb.pitcher_rolling_stats.

Computes per-game team/pitcher stats and rolling window averages using
PostgreSQL window functions, then bulk-upserts into the optimized tables.
The rolling stats include per-game team and pitcher metrics derived from
cumulative_game_stats and pitcher_game_stats.

Usage:
    # Full backfill
    python -m backend.app.handicapping.mlb.populate_rolling

    # Incremental (only new games)
    python -m backend.app.handicapping.mlb.populate_rolling --incremental

    # One table only
    python -m backend.app.handicapping.mlb.populate_rolling --team-only
    python -m backend.app.handicapping.mlb.populate_rolling --pitcher-only
"""

import argparse
import logging
import time

from sqlalchemy import create_engine, text

from backend.app.core.config import settings

DATABASE_URL = settings.database_url_sync

logger = logging.getLogger(__name__)

# ── Read schema SQL ──────────────────────────────────────────────────────────
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_SQL = open(os.path.join(_HERE, "rolling_stats.sql")).read()


def _get_engine():
    return create_engine(DATABASE_URL)


def ensure_tables():
    engine = _get_engine()
    try:
        # Remove comment lines before splitting, so table CREATE stmts
        # that follow comments aren't lost
        lines = [l for l in SCHEMA_SQL.split("\n") if not l.strip().startswith("--")]
        clean = "\n".join(lines)
        with engine.begin() as conn:
            for stmt in clean.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(text(stmt))
        logger.info("Tables created/verified.")
    finally:
        engine.dispose()


# ── Team Rolling Stats ───────────────────────────────────────────────────────

TEAM_ROLLING_SQL = """
WITH games_with_status AS (
    SELECT
        cgs.game_id,
        cgs.team_id,
        cgs.team_side,
        cgs.season_id,
        cgs.game_date,
        g.home_team_id = cgs.team_id AS is_home,
        g.status = 'FINAL' AS is_final,

        -- Cumulative stats (season to date entering this game)
        cgs.bat_runs, cgs.bat_hits, cgs.bat_at_bats,
        cgs.bat_walks, cgs.bat_strikeouts, cgs.bat_home_runs, cgs.bat_total_bases,
        cgs.pitch_ip, cgs.pitch_er,
        cgs.pitch_hits_allowed, cgs.pitch_walks_allowed,
        cgs.pitch_strikeouts, cgs.pitch_home_runs_allowed,
        cgs.cum_avg, cgs.cum_obp, cgs.cum_slg, cgs.cum_ops,
        cgs.cum_era, cgs.cum_whip, cgs.cum_k9, cgs.cum_bb9,
        cgs.cum_babip, cgs.cum_k_rate, cgs.cum_bb_rate,

        ROW_NUMBER() OVER (
            PARTITION BY cgs.team_id, cgs.season_id
            ORDER BY cgs.game_date, cgs.game_id
        ) AS game_n,

        -- Previous cumulative values (for computing per-game deltas)
        LAG(cgs.bat_runs) OVER w AS prev_bat_runs,
        LAG(cgs.bat_hits) OVER w AS prev_bat_hits,
        LAG(cgs.bat_at_bats) OVER w AS prev_bat_at_bats,
        LAG(cgs.bat_walks) OVER w AS prev_bat_walks,
        LAG(cgs.bat_strikeouts) OVER w AS prev_bat_so,
        LAG(cgs.bat_home_runs) OVER w AS prev_bat_hr,
        LAG(cgs.bat_total_bases) OVER w AS prev_bat_tb,
        LAG(cgs.pitch_ip) OVER w AS prev_pitch_ip,
        LAG(cgs.pitch_er) OVER w AS prev_pitch_er,
        LAG(cgs.pitch_hits_allowed) OVER w AS prev_pitch_h,
        LAG(cgs.pitch_walks_allowed) OVER w AS prev_pitch_bb,
        LAG(cgs.pitch_strikeouts) OVER w AS prev_pitch_k,
        LAG(cgs.pitch_home_runs_allowed) OVER w AS prev_pitch_hr

    FROM mlb.cumulative_game_stats cgs
    JOIN mlb.games g ON g.id = cgs.game_id
    WINDOW w AS (PARTITION BY cgs.team_id, cgs.season_id
                 ORDER BY cgs.game_date, cgs.game_id)
)
, per_game AS (
    SELECT *,
        -- Per-game batting deltas (NULL for non-FINAL)
        CASE WHEN is_final THEN (bat_runs - COALESCE(prev_bat_runs, 0)) END AS rf,
        CASE WHEN is_final THEN (bat_hits - COALESCE(prev_bat_hits, 0)) END AS hits,
        CASE WHEN is_final THEN (bat_at_bats - COALESCE(prev_bat_at_bats, 0)) END AS at_bats,
        CASE WHEN is_final THEN (bat_walks - COALESCE(prev_bat_walks, 0)) END AS walks,
        CASE WHEN is_final THEN (bat_strikeouts - COALESCE(prev_bat_so, 0)) END AS strikeouts,
        CASE WHEN is_final THEN (bat_home_runs - COALESCE(prev_bat_hr, 0)) END AS home_runs,
        CASE WHEN is_final THEN (bat_total_bases - COALESCE(prev_bat_tb, 0)) END AS total_bases,

        -- Per-game pitching deltas
        CASE WHEN is_final THEN (pitch_ip - COALESCE(prev_pitch_ip, 0)) END AS ip_outs,
        CASE WHEN is_final THEN (pitch_er - COALESCE(prev_pitch_er, 0)) END AS ra,
        CASE WHEN is_final THEN (pitch_hits_allowed - COALESCE(prev_pitch_h, 0)) END AS hits_allowed,
        CASE WHEN is_final THEN (pitch_walks_allowed - COALESCE(prev_pitch_bb, 0)) END AS walks_allowed,
        CASE WHEN is_final THEN (pitch_strikeouts - COALESCE(prev_pitch_k, 0)) END AS k_allowed,
        CASE WHEN is_final THEN (pitch_home_runs_allowed - COALESCE(prev_pitch_hr, 0)) END AS hr_allowed

    FROM games_with_status
)
, per_game_rate AS (
    SELECT *,
        -- Per-game rate stats (separate CTE so aliases resolve)
        CASE WHEN is_final AND at_bats > 0
            THEN hits::DOUBLE PRECISION / at_bats END AS avg_this,
        CASE WHEN is_final AND (at_bats + walks) > 0
            THEN (hits + walks)::DOUBLE PRECISION / (at_bats + walks) END AS obp_this,
        CASE WHEN is_final AND at_bats > 0
            THEN total_bases::DOUBLE PRECISION / at_bats END AS slg_this,
        CASE WHEN is_final AND at_bats > 0
            THEN (hits + walks)::DOUBLE PRECISION / at_bats
                 + total_bases::DOUBLE PRECISION / at_bats END AS ops_this,
        CASE WHEN is_final AND ip_outs > 0
            THEN ra::DOUBLE PRECISION * 9 / ip_outs::DOUBLE PRECISION END AS era_this,
        CASE WHEN is_final AND ip_outs > 0
            THEN (hits_allowed + walks_allowed)::DOUBLE PRECISION / ip_outs::DOUBLE PRECISION END AS whip_this,
        CASE WHEN is_final AND ip_outs > 0
            THEN k_allowed::DOUBLE PRECISION * 9 / ip_outs::DOUBLE PRECISION END AS k9_this,
        CASE WHEN is_final AND ip_outs > 0
            THEN walks_allowed::DOUBLE PRECISION * 9 / ip_outs::DOUBLE PRECISION END AS bb9_this
    FROM per_game
)
SELECT *,
    -- 5-game rolling averages (ROWS BETWEEN excludes current game = no lookahead)
    AVG(rf)  OVER w5 AS rf5,
    AVG(ra)  OVER w5 AS ra5,
    AVG(avg_this)  OVER w5 AS avg5,
    AVG(obp_this)  OVER w5 AS obp5,
    AVG(slg_this)  OVER w5 AS slg5,
    AVG(ops_this)  OVER w5 AS ops5,
    AVG(era_this)  OVER w5 AS era5,
    AVG(whip_this) OVER w5 AS whip5,
    AVG(k9_this)   OVER w5 AS k9_5,
    AVG(bb9_this)  OVER w5 AS bb9_5,

    -- 10-game rolling
    AVG(rf)  OVER w10 AS rf10,
    AVG(ra)  OVER w10 AS ra10,
    AVG(avg_this)  OVER w10 AS avg10,
    AVG(obp_this)  OVER w10 AS obp10,
    AVG(slg_this)  OVER w10 AS slg10,
    AVG(ops_this)  OVER w10 AS ops10,
    AVG(era_this)  OVER w10 AS era10,
    AVG(whip_this) OVER w10 AS whip10,
    AVG(k9_this)   OVER w10 AS k9_10,
    AVG(bb9_this)  OVER w10 AS bb9_10,

    -- 15-game rolling (runs, avg, era, whip only)
    AVG(rf)  OVER w15 AS rf15,
    AVG(ra)  OVER w15 AS ra15,
    AVG(avg_this)  OVER w15 AS avg15,
    AVG(slg_this)  OVER w15 AS slg15,
    AVG(ops_this)  OVER w15 AS ops15,
    AVG(era_this)  OVER w15 AS era15,
    AVG(whip_this) OVER w15 AS whip15,

    -- 20-game rolling
    AVG(rf)  OVER w20 AS rf20,
    AVG(ra)  OVER w20 AS ra20,
    AVG(avg_this)  OVER w20 AS avg20,
    AVG(slg_this)  OVER w20 AS slg20,
    AVG(ops_this)  OVER w20 AS ops20,
    AVG(era_this)  OVER w20 AS era20,
    AVG(whip_this) OVER w20 AS whip20

FROM per_game_rate
WINDOW
    w5  AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING),
    w10 AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING),
    w15 AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING),
    w20 AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING)
ORDER BY team_id, season_id, game_date, game_id
;
"""



def _get_conn():
    """Get a raw psycopg2 connection for fast bulk inserts."""
    from urllib.parse import urlparse
    import psycopg2
    url = urlparse(DATABASE_URL)
    return psycopg2.connect(
        host=url.hostname, port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username, password=url.password,
    )


def _bulk_upsert(sql: str, table: str, exclude_cols: set[str], truncate: bool = False) -> int:
    """
    Run compute SQL, then bulk-upsert results into *table* using psycopg2.
    Much faster than SQLAlchemy + ORM for large row counts.
    """
    import psycopg2
    from psycopg2.extras import execute_values
    from urllib.parse import urlparse

    url = urlparse(DATABASE_URL)
    conn = psycopg2.connect(
        host=url.hostname, port=url.port or 5432,
        dbname=url.path.lstrip("/"),
        user=url.username, password=url.password,
    )
    cur = conn.cursor()

    try:
        if truncate:
            cur.execute(f"TRUNCATE {table}")
            logger.info("Truncated %s", table)

        cur.execute(sql)
        rows = cur.fetchall()
        col_names = [d.name for d in cur.description]
        logger.info("Computed %d rows for %s", len(rows), table)

        if not rows:
            conn.commit()
            return 0

        insert_cols = [c for c in col_names if c not in exclude_cols]
        col_indices = [col_names.index(c) for c in insert_cols]
        filtered_rows = [tuple(r[i] for i in col_indices) for r in rows]
        cols_str = ", ".join(insert_cols)

        execute_values(
            cur,
            f"INSERT INTO {table} ({cols_str}) VALUES %s ON CONFLICT DO NOTHING",
            filtered_rows,
            page_size=5000,
        )
        conn.commit()
        logger.info("Bulk-upserted %d rows into %s", len(filtered_rows), table)
        return len(filtered_rows)
    finally:
        cur.close()
        conn.close()


def populate_team_rolling(engine=None, incremental=False) -> int:
    sql = TEAM_ROLLING_SQL
    if incremental:
        # Subquery approach: wrap cumulative_game_stats with filter
        sql = sql.replace(
            "FROM mlb.cumulative_game_stats cgs",
            "FROM (SELECT * FROM mlb.cumulative_game_stats WHERE game_id NOT IN"
            " (SELECT game_id FROM mlb.team_rolling_stats)) cgs"
        )
    return _bulk_upsert(
        sql=sql,
        table="mlb.team_rolling_stats",
        exclude_cols={"is_home", "is_final", "game_n",
                     "bat_runs", "bat_hits", "bat_at_bats", "bat_walks",
                     "bat_strikeouts", "bat_home_runs", "bat_total_bases",
                     "pitch_ip", "pitch_er",
                     "pitch_hits_allowed", "pitch_walks_allowed",
                     "pitch_strikeouts", "pitch_home_runs_allowed",
                     "prev_bat_runs", "prev_bat_hits",
                     "prev_bat_at_bats", "prev_bat_walks", "prev_bat_so",
                     "prev_bat_hr", "prev_bat_tb",
                     "prev_pitch_ip", "prev_pitch_er", "prev_pitch_h",
                     "prev_pitch_bb", "prev_pitch_k", "prev_pitch_hr", "avg_this", "obp_this", "slg_this", "ops_this", "era_this", "whip_this", "k9_this", "bb9_this"},
        truncate=not incremental,
    )


# ── Pitcher Rolling Stats ────────────────────────────────────────────────────

PITCHER_ROLLING_SQL = """
WITH per_start AS (
    SELECT
        pgs.game_id,
        pgs.pitcher_mlb_id AS player_id,
        t.id AS team_id,
        pgs.team_abbr,
        g.season_id,
        g.date AS game_date,
        pgs.is_starter,

        -- IP in outs
        (COALESCE(NULLIF(SPLIT_PART(pgs.ip::TEXT, '.', 1), ''), '0')::INTEGER * 3
         + COALESCE(NULLIF(SPLIT_PART(pgs.ip::TEXT, '.', 2), ''), '0')::INTEGER) AS ip_outs,
        pgs.er,
        pgs.h AS hits_allowed,
        pgs.bb AS walks_allowed,
        pgs.k AS strikeouts,
        pgs.hr AS home_runs_allowed,

        ROW_NUMBER() OVER (
            PARTITION BY pgs.pitcher_mlb_id, g.season_id
            ORDER BY g.date, pgs.game_id
        ) AS start_n

    FROM mlb.pitcher_game_stats pgs
    JOIN mlb.games g ON g.id = pgs.game_id
    JOIN mlb.teams t ON t.abbreviation = pgs.team_abbr
    WHERE pgs.is_starter = TRUE
      AND g.status = 'FINAL'
)
SELECT *,
    -- Per-start derived
    CASE WHEN ip_outs > 0
        THEN 9.0 * er::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3) END AS era_this_start,
    CASE WHEN ip_outs > 0
        THEN (hits_allowed + walks_allowed)::DOUBLE PRECISION / ip_outs::DOUBLE PRECISION END AS whip_this_start,
    CASE WHEN ip_outs > 0
        THEN strikeouts::DOUBLE PRECISION / (ip_outs::DOUBLE PRECISION / 3) * 9 END AS k9_this_start,
    CASE WHEN ip_outs > 0
        THEN walks_allowed::DOUBLE PRECISION * 9 / ip_outs::DOUBLE PRECISION END AS bb9_this_start,
    CASE WHEN ip_outs >= 18 AND er <= 3 THEN TRUE ELSE FALSE END AS is_quality_start,

    -- Cumulative (season to date, before this start)
    CASE WHEN SUM(ip_outs) OVER w > 0
        THEN 9.0 * SUM(er) OVER w::DOUBLE PRECISION / (SUM(ip_outs) OVER w::DOUBLE PRECISION / 3) END AS era_ytd,
    CASE WHEN SUM(ip_outs) OVER w > 0
        THEN (SUM(hits_allowed) OVER w + SUM(walks_allowed) OVER w)::DOUBLE PRECISION
             / (SUM(ip_outs) OVER w::DOUBLE PRECISION / 3) END AS whip_ytd,
    CASE WHEN SUM(ip_outs) OVER w > 0
        THEN SUM(strikeouts) OVER w::DOUBLE PRECISION / (SUM(ip_outs) OVER w::DOUBLE PRECISION / 3) * 9 END AS k9_ytd,
    CASE WHEN SUM(ip_outs) OVER w > 0
        THEN SUM(walks_allowed) OVER w::DOUBLE PRECISION / (SUM(ip_outs) OVER w::DOUBLE PRECISION / 3) * 9 END AS bb9_ytd,
    CASE WHEN SUM(walks_allowed) OVER w > 0
        THEN SUM(strikeouts) OVER w::DOUBLE PRECISION / SUM(walks_allowed) OVER w END AS kbb_ytd,
    CASE WHEN SUM(ip_outs) OVER w > 0
        THEN (13.0 * SUM(home_runs_allowed) OVER w + 3.0 * SUM(walks_allowed) OVER w - 2.0 * SUM(strikeouts) OVER w)
             / (SUM(ip_outs) OVER w::DOUBLE PRECISION / 3) + 3.10 END AS fip_ytd,
    COUNT(*) OVER w AS starts_ytd,
    CASE WHEN COUNT(*) OVER w > 0
        THEN SUM(CASE WHEN ip_outs >= 18 AND er <= 3 THEN 1 ELSE 0 END) OVER w::DOUBLE PRECISION
             / COUNT(*) OVER w END AS qs_rate_ytd,

    -- 5-start rolling
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN 9.0 * SUM(er) OVER w5::DOUBLE PRECISION / (SUM(ip_outs) OVER w5::DOUBLE PRECISION / 3) END AS era_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN (SUM(hits_allowed) OVER w5 + SUM(walks_allowed) OVER w5)::DOUBLE PRECISION
             / (SUM(ip_outs) OVER w5::DOUBLE PRECISION / 3) END AS whip_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN SUM(strikeouts) OVER w5::DOUBLE PRECISION / (SUM(ip_outs) OVER w5::DOUBLE PRECISION / 3) * 9 END AS k9_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN SUM(walks_allowed) OVER w5::DOUBLE PRECISION / (SUM(ip_outs) OVER w5::DOUBLE PRECISION / 3) * 9 END AS bb9_5,
    CASE WHEN SUM(walks_allowed) OVER w5 > 0
        THEN SUM(strikeouts) OVER w5::DOUBLE PRECISION / SUM(walks_allowed) OVER w5 END AS kbb_5,

    -- 10-start rolling
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN 9.0 * SUM(er) OVER w10::DOUBLE PRECISION / (SUM(ip_outs) OVER w10::DOUBLE PRECISION / 3) END AS era_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN (SUM(hits_allowed) OVER w10 + SUM(walks_allowed) OVER w10)::DOUBLE PRECISION
             / (SUM(ip_outs) OVER w10::DOUBLE PRECISION / 3) END AS whip_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN SUM(strikeouts) OVER w10::DOUBLE PRECISION / (SUM(ip_outs) OVER w10::DOUBLE PRECISION / 3) * 9 END AS k9_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN SUM(walks_allowed) OVER w10::DOUBLE PRECISION / (SUM(ip_outs) OVER w10::DOUBLE PRECISION / 3) * 9 END AS bb9_10,
    CASE WHEN SUM(walks_allowed) OVER w10 > 0
        THEN SUM(strikeouts) OVER w10::DOUBLE PRECISION / SUM(walks_allowed) OVER w10 END AS kbb_10,

    -- 15-start rolling
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN 9.0 * SUM(er) OVER w15::DOUBLE PRECISION / (SUM(ip_outs) OVER w15::DOUBLE PRECISION / 3) END AS era_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN (SUM(hits_allowed) OVER w15 + SUM(walks_allowed) OVER w15)::DOUBLE PRECISION
             / (SUM(ip_outs) OVER w15::DOUBLE PRECISION / 3) END AS whip_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN SUM(strikeouts) OVER w15::DOUBLE PRECISION / (SUM(ip_outs) OVER w15::DOUBLE PRECISION / 3) * 9 END AS k9_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN SUM(walks_allowed) OVER w15::DOUBLE PRECISION / (SUM(ip_outs) OVER w15::DOUBLE PRECISION / 3) * 9 END AS bb9_15

FROM per_start
WINDOW
    w   AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
    w5  AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING),
    w10 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 10 PRECEDING AND 1 PRECEDING),
    w15 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 15 PRECEDING AND 1 PRECEDING)
ORDER BY player_id, season_id, game_date, game_id
;
"""


def populate_pitcher_rolling(engine=None, incremental=False) -> int:
    sql = PITCHER_ROLLING_SQL
    if incremental:
        sql = sql.replace(
            "FROM mlb.pitcher_game_stats pgs",
            "FROM (SELECT * FROM mlb.pitcher_game_stats WHERE game_id NOT IN"
            " (SELECT game_id FROM mlb.pitcher_rolling_stats)) pgs"
        )
    return _bulk_upsert(
        sql=sql,
        table="mlb.pitcher_rolling_stats",
        exclude_cols={"start_n"},
        truncate=not incremental,
    )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Populate rolling stats tables")
    parser.add_argument("--incremental", action="store_true")
    parser.add_argument("--team-only", action="store_true")
    parser.add_argument("--pitcher-only", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    start = time.time()

    if not args.pitcher_only:
        t0 = time.time()
        n = populate_team_rolling(incremental=args.incremental)
        logger.info("Team rolling: %d rows in %.1fs", n, time.time() - t0)

    if not args.team_only:
        t0 = time.time()
        n = populate_pitcher_rolling(incremental=args.incremental)
        logger.info("Pitcher rolling: %d rows in %.1fs", n, time.time() - t0)

    logger.info("Total: %.1fs", time.time() - start)


if __name__ == "__main__":
    main()
