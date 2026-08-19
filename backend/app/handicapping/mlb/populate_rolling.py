"""
Populate mlb.team_rolling_stats and mlb.pitcher_rolling_stats.

!=======================================================================================!
!  GAME ROWS INCLUDE THE RESULT OF THE GAME!                                            !
!=======================================================================================!
!  EACH row in team_rolling_stats stores stats THROUGH that game (its own result        !
!  IS included): e.g. a team entering game X at 73-49 that wins X stores 74-49.         !
!  The data_loader (trs_h/trs_a LATERALs) reads the PREVIOUS Final row, so the model    !
!  sees the record entering the target game — correct AND leak-safe.                    !
!  Do NOT change the windows back to "... AND 1 PRECEDING" — that causes an off-by-one   !
!  (the previous-row read would double-subtract the most recent game).
!=======================================================================================!

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

from app.core.config import settings

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
        cgs.game_timestamp AS game_date,
        g.home_team_id = cgs.team_id AS is_home,
        g.status = 'FINAL' AS is_final,

        -- Cumulative stats (season to date entering this game)
        cgs.bat_runs, cgs.bat_hits, cgs.bat_at_bats,
        cgs.bat_walks, cgs.bat_strikeouts, cgs.bat_home_runs, cgs.bat_total_bases,
        cgs.pitch_ip, cgs.pitch_er,
        cgs.pitch_hits_allowed, cgs.pitch_walks_allowed,
        cgs.pitch_strikeouts, cgs.pitch_home_runs_allowed,
        g.venue_id,
        g.home_score, g.away_score,
        -- 1 if this team won this game (leak-safe: only used in 1 PRECEDING windows)
        CASE WHEN (g.home_team_id = cgs.team_id AND g.home_score > g.away_score)
                  OR (g.away_team_id = cgs.team_id AND g.away_score > g.home_score)
             THEN 1 ELSE 0 END AS won,
        blc.closing_ou,
        cgs.cum_avg, cgs.cum_obp, cgs.cum_slg, cgs.cum_ops,
        cgs.cum_era, cgs.cum_whip, cgs.cum_k9, cgs.cum_bb9,
        cgs.cum_babip, cgs.cum_k_rate, cgs.cum_bb_rate,

        -- Per-game bullpen relief IP-outs + ER for THIS team in THIS game
        bp.bp_ip_outs,
        bp.bp_er,

        ROW_NUMBER() OVER (
            PARTITION BY cgs.team_id, cgs.season_id
            ORDER BY cgs.game_timestamp, cgs.game_id
        ) AS game_n,

        ROW_NUMBER() OVER (
            PARTITION BY cgs.team_id, cgs.season_id, cgs.team_side
            ORDER BY cgs.game_timestamp, cgs.game_id
        ) AS side_game_n,

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
    LEFT JOIN mlb.betting_lines_consolidated blc ON blc.game_id = cgs.game_id
    -- Per-game bullpen (relief) IP-outs + ER for THIS team in THIS game, from
    -- bullpen_game_stats (one row per game,team). NULL when no relief logged.
    LEFT JOIN (
        SELECT bg.game_id, bg.team_id,
               SUM(bg.bullpen_ip_outs) AS bp_ip_outs,
               SUM(bg.bullpen_er)    AS bp_er
        FROM mlb.bullpen_game_stats bg
        GROUP BY bg.game_id, bg.team_id
    ) bp ON bp.game_id = cgs.game_id AND bp.team_id = cgs.team_id
    WINDOW w AS (PARTITION BY cgs.team_id, cgs.season_id
                 ORDER BY cgs.game_timestamp, cgs.game_id)
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
        -- ra = RUNS ALLOWED (total, incl. unearned) this game — defense matters.
        -- Equals the OPPONENT's score: home team allows away_score, away team
        -- allows home_score. (ERA stays earned-based via era_this / pitch_er.)
        CASE WHEN is_final
             THEN CASE WHEN is_home THEN away_score ELSE home_score END
        END AS ra,
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
    -- 5-game rolling averages THROUGH this game (w5 = 4 PRECEDING..CURRENT ROW).
    -- data_loader reads the PREVIOUS Final row, so the model sees the last 5 games
    -- entering the target game (leak-safe). w_bp5 is the same width for bullpen.
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
    AVG(whip_this) OVER w20 AS whip20,

    -- Venue-conditional last-10 (only this team's games at this row's venue).
    -- venue_rf_r10 = avg runs scored; venue_win_pct_r10 = win rate. Loader
    -- projects h_home_rf_r10 / h_home_win_pct_r10 for the home team and
    -- a_away_rf_r10 / a_away_win_pct_r10 for the away team.
    AVG(rf)         OVER w10_venue AS venue_rf_r10,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score > away_score)::int
                 ELSE (away_score > home_score)::int END
        END)        OVER w10_venue AS venue_win_pct_r10,

    -- Pre-computed game counts (replaces correlated subqueries in GAME_QUERY)
    CASE WHEN team_side = 'home' THEN GREATEST(0, side_game_n - 1) END AS home_games_sofar,
    CASE WHEN team_side = 'away' THEN GREATEST(0, side_game_n - 1) END AS away_games_sofar,
    CASE WHEN team_side = 'away' THEN (
        SELECT CASE WHEN COUNT(*) = 0 THEN 0.5
                 ELSE SUM(CASE WHEN g2.away_score > g2.home_score THEN 1.0 ELSE 0.0 END)::float / COUNT(*)::float
                 END
        FROM mlb.games g2
        WHERE g2.venue_id = pgr.venue_id
          AND g2.away_team_id = pgr.team_id
          AND g2.date < pgr.game_date
          AND g2.status = 'FINAL'
    ) END AS game_away_venue_pct,

    -- Pre-computed season-wide win/over/spread percentages
    -- (window frames exclude current row: ROWS ... AND 1 PRECEDING)
    -- win_this / over_this / spread_this computed inline because
    -- SQL can't reference same-level aliases in window functions
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score > away_score)::int
                 ELSE (away_score > home_score)::int END
        END) OVER w_full  AS win_pct,
    AVG(CASE WHEN is_final AND closing_ou IS NOT NULL THEN
            ((home_score + away_score) > closing_ou)::int
        END) OVER w_full  AS over_pct,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score - away_score)::float
                 ELSE (away_score - home_score)::float END
        END) OVER w_full  AS spread_pct,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score > away_score)::int
                 ELSE (away_score > home_score)::int END
        END) OVER w5      AS win_pct5,
    AVG(CASE WHEN is_final AND closing_ou IS NOT NULL THEN
            ((home_score + away_score) > closing_ou)::int
        END) OVER w5      AS over_pct5,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score - away_score)::float
                 ELSE (away_score - home_score)::float END
        END) OVER w5      AS spread_pct5,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score > away_score)::int
                 ELSE (away_score > home_score)::int END
        END) OVER w10     AS win_pct10,
    AVG(CASE WHEN is_final AND closing_ou IS NOT NULL THEN
            ((home_score + away_score) > closing_ou)::int
        END) OVER w10     AS over_pct10,
    AVG(CASE WHEN is_final THEN
            CASE WHEN is_home THEN (home_score > away_score)::int
                 ELSE (away_score > home_score)::int END
        END) OVER w15     AS win_pct15,
    AVG(CASE WHEN is_final AND closing_ou IS NOT NULL THEN
            ((home_score + away_score) > closing_ou)::int
        END) OVER w15     AS over_pct15,

    -- Season expanding averages (through this game: w_full includes CURRENT ROW;)
    -- reader looks back to the PREVIOUS Final row so scheduled games stay leak-safe)
    AVG(rf) OVER w_full  AS rf_avg,
    AVG(ra) OVER w_full  AS ra_avg,

    -- Season-total W/L record THROUGH this game (w_full includes CURRENT ROW).
    -- The data_loader reads the PREVIOUS Final row, so the value the model sees
    -- is the record entering the target game (leak-safe). Only FINAL games count.
    SUM(CASE WHEN is_final AND won=1 THEN 1 ELSE 0 END) OVER w_full AS wins,
    SUM(CASE WHEN is_final AND won=0 THEN 1 ELSE 0 END) OVER w_full AS losses,

    -- Last-10 win / loss counts (through this game: w10 includes CURRENT ROW;
    -- reader uses the PREVIOUS Final row. Only FINAL games count so scheduled
    -- games never inflate the denominators)
    SUM(CASE WHEN is_final THEN won ELSE 0 END)   OVER w10 AS wins_l10,
    SUM(CASE WHEN is_final THEN 1 - won ELSE 0 END) OVER w10 AS losses_l10,

    -- Last-5 bullpen relief IP-outs + ER (through this game: w_bp5 includes
    -- CURRENT ROW; reader uses the PREVIOUS Final row = the bullpen's last 5
    -- appearances entering the target game). NULL/gated so scheduled+no-relief
    -- games don't inflate the window.
    SUM(CASE WHEN is_final THEN COALESCE(bp_ip_outs,0) ELSE 0 END) OVER w_bp5 AS bullpen_ip_l5,
    SUM(CASE WHEN is_final THEN COALESCE(bp_er,0)     ELSE 0 END) OVER w_bp5 AS bullpen_er_l5

FROM per_game_rate pgr
WINDOW
    w_full AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
    -- w5/w10/w15/w20 are N-game windows THROUGH the current row (N-1 preceding +
    -- current). This matches the documented "through this game" convention and
    -- w_bp5 (4 PRECEDING..CURRENT = 5 games). The data_loader reads the PREVIOUS
    -- Final row, so these equal the team's last N games entering the target game.
    w5     AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w_bp5  AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w10    AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
    w15    AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN 14 PRECEDING AND CURRENT ROW),
    w20    AS (PARTITION BY team_id, season_id ORDER BY game_date, game_id
               ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
    -- Venue-conditional last-10: partitioned by team_side too, so it only sees
    -- this team's games AT this row's venue (home-only on team_side='home',
    -- road-only on team_side='away'). Loader projects as h_home_*.r10 for the
    -- home team and a_away_*.r10 for the away team.
    w10_venue AS (PARTITION BY team_id, season_id, team_side
                  ORDER BY game_date, game_id
                  ROWS BETWEEN 9 PRECEDING AND CURRENT ROW)
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
        # IMPORTANT: The window functions (cumulative/rolling stats) need the
        # FULL game history to compute correct values. Filtering the source
        # table to only new games made every windowed stat come out NULL
        # (bug found 2026-08-01: all team rolling stats NULL since ~Jul 29).
        # Instead: compute the full query (windows see all history), then
        # only emit rows for games not yet in the table.
        sql = (
            "SELECT * FROM (\n"
            + TEAM_ROLLING_SQL.rstrip().rstrip(";")
            + "\n) AS full_calc\n"
            "WHERE game_id NOT IN (SELECT game_id FROM mlb.team_rolling_stats)"
        )
    return _bulk_upsert(
        sql=sql,
        table="mlb.team_rolling_stats",
        exclude_cols={"is_home", "is_final", "game_n", "side_game_n", "won", 
                     "bp_ip_outs", "bp_er",
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

        -- IP in outs. pgs.ip is DECIMAL INNINGS (6.333 = 6 1/3 IP, 0.333 = 1 out).
        -- outs = ROUND(ip * 3): 6.333*3=19, 0.333*3=1. Do NOT split on '.' (that
        -- assumed baseball notation 6.1=6 1/3, mis-parses decimal rows → inflated IP).
        ROUND(pgs.ip * 3) AS ip_outs,
        pgs.er,
        pgs.h AS hits_allowed,
        pgs.bb AS walks_allowed,
        pgs.k AS strikeouts,
        pgs.hr AS home_runs_allowed,

        -- Situational flags and rest days
        CASE WHEN pgs.team_abbr = (SELECT abbreviation FROM mlb.teams WHERE id = g.home_team_id)
             THEN TRUE ELSE FALSE END AS is_home_pitcher,
        CASE WHEN g.day_night = 'day' THEN TRUE ELSE FALSE END AS is_day_game,
        LAG(g.date) OVER (
            PARTITION BY pgs.pitcher_mlb_id, g.season_id
            ORDER BY g.date, pgs.game_id
        ) AS prev_start_date,

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

    -- Cumulative (season to date, THROUGH this start). Match the team tables'
    -- CURRENT ROW convention (see header warning): the data loader reads the
    -- PREVIOUS Final row, so each row must include its own start's line.
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

    -- Home/road splits
    CASE WHEN SUM(ip_outs) FILTER (WHERE is_home_pitcher) OVER w > 0
        THEN 9.0 * SUM(er) FILTER (WHERE is_home_pitcher) OVER w
             / (SUM(ip_outs) FILTER (WHERE is_home_pitcher) OVER w / 3.0)
    END AS home_era_ytd,
    CASE WHEN SUM(ip_outs) FILTER (WHERE NOT is_home_pitcher) OVER w > 0
        THEN 9.0 * SUM(er) FILTER (WHERE NOT is_home_pitcher) OVER w
             / (SUM(ip_outs) FILTER (WHERE NOT is_home_pitcher) OVER w / 3.0)
    END AS road_era_ytd,

    -- Day/night splits
    CASE WHEN SUM(ip_outs) FILTER (WHERE is_day_game) OVER w > 0
        THEN 9.0 * SUM(er) FILTER (WHERE is_day_game) OVER w
             / (SUM(ip_outs) FILTER (WHERE is_day_game) OVER w / 3.0)
    END AS day_era_ytd,
    CASE WHEN SUM(ip_outs) FILTER (WHERE NOT is_day_game) OVER w > 0
        THEN 9.0 * SUM(er) FILTER (WHERE NOT is_day_game) OVER w
             / (SUM(ip_outs) FILTER (WHERE NOT is_day_game) OVER w / 3.0)
    END AS night_era_ytd,

    -- Rest days
    EXTRACT(DAY FROM (game_date - prev_start_date))::INTEGER AS rest_days,

    -- 5-start rolling (COALESCE to 0 to handle NULL strikeout/walk data in source)
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN 9.0 * SUM(er) OVER w5::DOUBLE PRECISION / (SUM(ip_outs) OVER w5::DOUBLE PRECISION / 3) END AS era_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN (COALESCE(SUM(hits_allowed) OVER w5, 0) + COALESCE(SUM(walks_allowed) OVER w5, 0))::DOUBLE PRECISION
             / (COALESCE(SUM(ip_outs) OVER w5, 0)::DOUBLE PRECISION / 3) END AS whip_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN COALESCE(SUM(strikeouts) OVER w5, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w5, 0)::DOUBLE PRECISION / 3) * 9 END AS k9_5,
    CASE WHEN SUM(ip_outs) OVER w5 > 0
        THEN COALESCE(SUM(walks_allowed) OVER w5, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w5, 0)::DOUBLE PRECISION / 3) * 9 END AS bb9_5,
    CASE WHEN COALESCE(SUM(walks_allowed) OVER w5, 0) > 0
        THEN COALESCE(SUM(strikeouts) OVER w5, 0)::DOUBLE PRECISION / COALESCE(SUM(walks_allowed) OVER w5, 0) END AS kbb_5,

    -- 10-start rolling
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN 9.0 * SUM(er) OVER w10::DOUBLE PRECISION / (SUM(ip_outs) OVER w10::DOUBLE PRECISION / 3) END AS era_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN (COALESCE(SUM(hits_allowed) OVER w10, 0) + COALESCE(SUM(walks_allowed) OVER w10, 0))::DOUBLE PRECISION
             / (COALESCE(SUM(ip_outs) OVER w10, 0)::DOUBLE PRECISION / 3) END AS whip_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN COALESCE(SUM(strikeouts) OVER w10, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w10, 0)::DOUBLE PRECISION / 3) * 9 END AS k9_10,
    CASE WHEN SUM(ip_outs) OVER w10 > 0
        THEN COALESCE(SUM(walks_allowed) OVER w10, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w10, 0)::DOUBLE PRECISION / 3) * 9 END AS bb9_10,
    CASE WHEN COALESCE(SUM(walks_allowed) OVER w10, 0) > 0
        THEN COALESCE(SUM(strikeouts) OVER w10, 0)::DOUBLE PRECISION / COALESCE(SUM(walks_allowed) OVER w10, 0) END AS kbb_10,

    -- 15-start rolling
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN 9.0 * SUM(er) OVER w15::DOUBLE PRECISION / (SUM(ip_outs) OVER w15::DOUBLE PRECISION / 3) END AS era_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN (COALESCE(SUM(hits_allowed) OVER w15, 0) + COALESCE(SUM(walks_allowed) OVER w15, 0))::DOUBLE PRECISION
             / (COALESCE(SUM(ip_outs) OVER w15, 0)::DOUBLE PRECISION / 3) END AS whip_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN COALESCE(SUM(strikeouts) OVER w15, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w15, 0)::DOUBLE PRECISION / 3) * 9 END AS k9_15,
    CASE WHEN SUM(ip_outs) OVER w15 > 0
        THEN COALESCE(SUM(walks_allowed) OVER w15, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w15, 0)::DOUBLE PRECISION / 3) * 9 END AS bb9_15,

    -- 20-start rolling (for 20-game consistency)
    CASE WHEN SUM(ip_outs) OVER w20 > 0
        THEN 9.0 * SUM(er) OVER w20::DOUBLE PRECISION / (SUM(ip_outs) OVER w20::DOUBLE PRECISION / 3) END AS era_20,
    CASE WHEN SUM(ip_outs) OVER w20 > 0
        THEN (COALESCE(SUM(hits_allowed) OVER w20, 0) + COALESCE(SUM(walks_allowed) OVER w20, 0))::DOUBLE PRECISION
             / (COALESCE(SUM(ip_outs) OVER w20, 0)::DOUBLE PRECISION / 3) END AS whip_20,
    CASE WHEN SUM(ip_outs) OVER w20 > 0
        THEN COALESCE(SUM(strikeouts) OVER w20, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w20, 0)::DOUBLE PRECISION / 3) * 9 END AS k9_20,
    CASE WHEN SUM(ip_outs) OVER w20 > 0
        THEN COALESCE(SUM(walks_allowed) OVER w20, 0)::DOUBLE PRECISION / (COALESCE(SUM(ip_outs) OVER w20, 0)::DOUBLE PRECISION / 3) * 9 END AS bb9_20

FROM per_start
WINDOW
    w   AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
    w5  AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 5 PRECEDING AND CURRENT ROW),
    w10 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 10 PRECEDING AND CURRENT ROW),
    w15 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 15 PRECEDING AND CURRENT ROW),
    w20 AS (PARTITION BY player_id, season_id ORDER BY game_date, game_id
            ROWS BETWEEN 20 PRECEDING AND CURRENT ROW)
ORDER BY player_id, season_id, game_date, game_id
;
"""


def populate_pitcher_rolling(engine=None, incremental=False) -> int:
    sql = PITCHER_ROLLING_SQL
    if incremental:
        # IMPORTANT: The window functions (cumulative/rolling stats) need the
        # FULL game history to compute correct values. Filtering the source
        # table to only new games made every windowed stat come out NULL
        # (bug found 2026-08-01: all pitcher rolling stats NULL since ~Jul 29).
        # Instead: compute the full query (windows see all history), then
        # only emit rows for games not yet in the table.
        sql = (
            "SELECT * FROM (\n"
            + PITCHER_ROLLING_SQL.rstrip().rstrip(";")
            + "\n) AS full_calc\n"
            "WHERE game_id NOT IN (SELECT game_id FROM mlb.pitcher_rolling_stats)"
        )
    return _bulk_upsert(
        sql=sql,
        table="mlb.pitcher_rolling_stats",
        exclude_cols={"start_n", "is_home_pitcher", "is_day_game", "prev_start_date"},
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
