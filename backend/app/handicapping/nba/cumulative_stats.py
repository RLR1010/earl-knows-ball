"""
NBA Cumulative Game Stats

Pre-computes backward-looking cumulative team statistics for the NBA,
stored in nba.cumulative_game_stats.  Each row represents one team in one
game, with all season-to-date cumulative statistics for games 1..N
(**including the current game** — these are post-game running totals).

Consumers that need pre-game stats for a game (e.g. prediction features)
must exclude the current game themselves, e.g. via a LATERAL JOIN with
``game_id != g.id`` (see GAME_QUERY in data_loader.py).

Tiers
-----
1. Raw cumulative counters  (integers — sum of box-score columns)
2. Per-game averages        (floats — raw / games_played)
3. Advanced efficiency      (floats — ORTG, DRTG, pace, eFG%, etc.)

Following the pattern established in mlb/cumulative_stats.py and
nfl/cumulative_stats.py.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import Engine, create_engine, text as sa_text

logger = logging.getLogger(__name__)

# ── Table identity ───────────────────────────────────────────────────────────

CUM_TABLE = "nba.cumulative_game_stats"

# ── DDL ──────────────────────────────────────────────────────────────────────

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {CUM_TABLE} (
    game_id     INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    team_side   TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id   INTEGER NOT NULL,
    game_date   DATE    NOT NULL,

    -- Prior-game pointers (LAG; NULL on a team's first appearance)
    prev_game_id            INTEGER,
    prev_game_date          DATE,
    prev_game_id_season     INTEGER,
    prev_game_date_season   DATE,

    -- ── Tier 1: Raw cumulative counters ──────────────────────────────
    games_played           INTEGER DEFAULT 0,

    cum_points             INTEGER DEFAULT 0,
    cum_points_allowed     INTEGER DEFAULT 0,
    cum_margin             INTEGER DEFAULT 0,

    cum_fgm                INTEGER DEFAULT 0,
    cum_fga                INTEGER DEFAULT 0,
    cum_fgm3               INTEGER DEFAULT 0,
    cum_fga3               INTEGER DEFAULT 0,
    cum_ftm                INTEGER DEFAULT 0,
    cum_fta                INTEGER DEFAULT 0,

    cum_reb                INTEGER DEFAULT 0,
    cum_ast                INTEGER DEFAULT 0,
    cum_stl                INTEGER DEFAULT 0,
    cum_blk                INTEGER DEFAULT 0,
    cum_tov                INTEGER DEFAULT 0,
    cum_pf                 INTEGER DEFAULT 0,

    cum_opp_fgm            INTEGER DEFAULT 0,
    cum_opp_fga            INTEGER DEFAULT 0,
    cum_opp_fgm3           INTEGER DEFAULT 0,
    cum_opp_fga3           INTEGER DEFAULT 0,
    cum_opp_ftm            INTEGER DEFAULT 0,
    cum_opp_fta            INTEGER DEFAULT 0,
    cum_opp_reb            INTEGER DEFAULT 0,
    cum_opp_ast            INTEGER DEFAULT 0,
    cum_opp_stl            INTEGER DEFAULT 0,
    cum_opp_blk            INTEGER DEFAULT 0,
    cum_opp_tov            INTEGER DEFAULT 0,
    cum_opp_pf             INTEGER DEFAULT 0,

    -- ── Tier 2: Per-game averages ────────────────────────────────────
    cum_ppg                DOUBLE PRECISION,
    cum_oppg               DOUBLE PRECISION,
    cum_margin_pg          DOUBLE PRECISION,
    cum_fg_pct             DOUBLE PRECISION,
    cum_fg3_pct            DOUBLE PRECISION,
    cum_ft_pct             DOUBLE PRECISION,
    cum_reb_pg             DOUBLE PRECISION,
    cum_ast_pg             DOUBLE PRECISION,
    cum_stl_pg             DOUBLE PRECISION,
    cum_blk_pg             DOUBLE PRECISION,
    cum_tov_pg             DOUBLE PRECISION,
    cum_pf_pg              DOUBLE PRECISION,

    -- ── Tier 3: Advanced efficiency metrics ──────────────────────────
    cum_ortg               DOUBLE PRECISION,
    cum_drtg               DOUBLE PRECISION,
    cum_net_ortg           DOUBLE PRECISION,
    cum_pace               DOUBLE PRECISION,
    cum_efg_pct            DOUBLE PRECISION,
    cum_opp_efg_pct        DOUBLE PRECISION,
    cum_tov_rate           DOUBLE PRECISION,
    cum_opp_tov_rate       DOUBLE PRECISION,
    cum_ft_rate            DOUBLE PRECISION,
    cum_3pa_rate           DOUBLE PRECISION,
    cum_ast_ratio          DOUBLE PRECISION,
    cum_stl_rate           DOUBLE PRECISION,
    cum_blk_rate           DOUBLE PRECISION,

    -- ── Tier 5: Team quality ───────────────────────────────────────
    cum_win_pct            DOUBLE PRECISION,
    -- Venue-scoped season win pct: this team's win pct playing AT this row's
    -- venue (team_side). Read by the loader as h_home_win_pct_season
    -- (home team at home) / a_away_win_pct_season (away team on road).
    venue_win_pct_season   DOUBLE PRECISION,

    PRIMARY KEY (game_id, team_side)
);
CREATE INDEX IF NOT EXISTS idx_nba_cgs_team_date_game
    ON nba.cumulative_game_stats (team_id, game_date DESC, game_id DESC);

-- Prior-game pointer columns (idempotent)
ALTER TABLE nba.cumulative_game_stats ADD COLUMN IF NOT EXISTS prev_game_id INTEGER;
ALTER TABLE nba.cumulative_game_stats ADD COLUMN IF NOT EXISTS prev_game_date DATE;
ALTER TABLE nba.cumulative_game_stats ADD COLUMN IF NOT EXISTS prev_game_id_season INTEGER;
ALTER TABLE nba.cumulative_game_stats ADD COLUMN IF NOT EXISTS prev_game_date_season DATE;
"""

# ── SQL: per-game team box-score view ───────────────────────────────────────

GET_TEAM_GAME_SQL = """
WITH team_games AS (
    SELECT
        g.id           AS game_id,
        g.home_team_id AS team_id,
        'home'         AS team_side,
        g.season_id    AS season_id,
        (g.date AT TIME ZONE 'America/New_York')::date AS game_date,
        g.home_score   AS points,
        g.away_score   AS points_allowed,
        g.home_field_goals_made        AS fgm,
        g.home_field_goals_attempted   AS fga,
        g.home_three_points_made       AS fgm3,
        g.home_three_points_attempted  AS fga3,
        g.home_free_throws_made        AS ftm,
        g.home_free_throws_attempted   AS fta,
        g.home_rebounds                AS reb,
        g.home_offensive_rebounds      AS off_reb,
        g.home_assists                 AS ast,
        COALESCE(g.home_steals, 0)     AS stl,
        COALESCE(g.home_blocks, 0)     AS blk,
        COALESCE(g.home_total_turnovers, 0)  AS tov,
        COALESCE(g.home_fouls, 0)      AS pf,
        g.away_field_goals_made        AS opp_fgm,
        g.away_field_goals_attempted   AS opp_fga,
        g.away_three_points_made       AS opp_fgm3,
        g.away_three_points_attempted  AS opp_fga3,
        g.away_free_throws_made        AS opp_ftm,
        g.away_free_throws_attempted   AS opp_fta,
        g.away_rebounds                AS opp_reb,
        g.away_offensive_rebounds      AS opp_off_reb,
        g.away_assists                 AS opp_ast,
        COALESCE(g.away_steals, 0)     AS opp_stl,
        COALESCE(g.away_blocks, 0)     AS opp_blk,
        COALESCE(g.away_total_turnovers, 0)  AS opp_tov,
        COALESCE(g.away_fouls, 0)      AS opp_pf,
        -- basketball-reference game possessions: the single symmetric weight
        -- (0.5*(TmPoss+OppPoss)) computed per game, identical for both team rows.
        -- The official BBRef constant is 1.07 (not 1.08/Dean-Oliver).
        (  ( CASE WHEN g.home_offensive_rebounds IS NOT NULL
                     AND (g.home_offensive_rebounds + (g.away_rebounds - g.away_offensive_rebounds)) > 0
                THEN g.home_field_goals_attempted
                     + 0.4 * g.home_free_throws_attempted
                     - 1.07 * (g.home_offensive_rebounds::float
                               / (g.home_offensive_rebounds + (g.away_rebounds - g.away_offensive_rebounds)))
                     * (g.home_field_goals_attempted - g.home_field_goals_made)
                     + COALESCE(g.home_total_turnovers, 0)
                ELSE g.home_field_goals_attempted + 0.4 * g.home_free_throws_attempted + COALESCE(g.home_total_turnovers, 0)
           END )
         + ( CASE WHEN g.away_offensive_rebounds IS NOT NULL
                     AND (g.away_offensive_rebounds + (g.home_rebounds - g.home_offensive_rebounds)) > 0
                THEN g.away_field_goals_attempted
                     + 0.4 * g.away_free_throws_attempted
                     - 1.07 * (g.away_offensive_rebounds::float
                               / (g.away_offensive_rebounds + (g.home_rebounds - g.home_offensive_rebounds)))
                     * (g.away_field_goals_attempted - g.away_field_goals_made)
                     + COALESCE(g.away_total_turnovers, 0)
                ELSE g.away_field_goals_attempted + 0.4 * g.away_free_throws_attempted + COALESCE(g.away_total_turnovers, 0)
           END )
        ) * 0.5 AS bbr_poss,
        (g.home_score - g.away_score)  AS margin
    FROM nba.games g
    WHERE g.status = 'FINAL'
      AND g.season_id IS NOT NULL
      AND g.game_type IN ('REG','POST','PLAYIN')

    UNION ALL

    SELECT
        g.id           AS game_id,
        g.away_team_id AS team_id,
        'away'         AS team_side,
        g.season_id    AS season_id,
        (g.date AT TIME ZONE 'America/New_York')::date AS game_date,
        g.away_score   AS points,
        g.home_score   AS points_allowed,
        g.away_field_goals_made        AS fgm,
        g.away_field_goals_attempted   AS fga,
        g.away_three_points_made       AS fgm3,
        g.away_three_points_attempted  AS fga3,
        g.away_free_throws_made        AS ftm,
        g.away_free_throws_attempted   AS fta,
        g.away_rebounds                AS reb,
        g.away_offensive_rebounds      AS off_reb,
        g.away_assists                 AS ast,
        COALESCE(g.away_steals, 0)     AS stl,
        COALESCE(g.away_blocks, 0)     AS blk,
        COALESCE(g.away_total_turnovers, 0)  AS tov,
        COALESCE(g.away_fouls, 0)      AS pf,
        g.home_field_goals_made        AS opp_fgm,
        g.home_field_goals_attempted   AS opp_fga,
        g.home_three_points_made       AS opp_fgm3,
        g.home_three_points_attempted  AS opp_fga3,
        g.home_free_throws_made        AS opp_ftm,
        g.home_free_throws_attempted   AS opp_fta,
        g.home_rebounds                AS opp_reb,
        g.home_offensive_rebounds      AS opp_off_reb,
        g.home_assists                 AS opp_ast,
        COALESCE(g.home_steals, 0)     AS opp_stl,
        COALESCE(g.home_blocks, 0)     AS opp_blk,
        COALESCE(g.home_total_turnovers, 0)  AS opp_tov,
        COALESCE(g.home_fouls, 0)      AS opp_pf,
        -- basketball-reference game possessions (single symmetric value, same as
        -- the home row; constant 1.07).
        (  ( CASE WHEN g.away_offensive_rebounds IS NOT NULL
                     AND (g.away_offensive_rebounds + (g.home_rebounds - g.home_offensive_rebounds)) > 0
                THEN g.away_field_goals_attempted
                     + 0.4 * g.away_free_throws_attempted
                     - 1.07 * (g.away_offensive_rebounds::float
                               / (g.away_offensive_rebounds + (g.home_rebounds - g.home_offensive_rebounds)))
                     * (g.away_field_goals_attempted - g.away_field_goals_made)
                     + COALESCE(g.away_total_turnovers, 0)
                ELSE g.away_field_goals_attempted + 0.4 * g.away_free_throws_attempted + COALESCE(g.away_total_turnovers, 0)
           END )
         + ( CASE WHEN g.home_offensive_rebounds IS NOT NULL
                     AND (g.home_offensive_rebounds + (g.away_rebounds - g.away_offensive_rebounds)) > 0
                THEN g.home_field_goals_attempted
                     + 0.4 * g.home_free_throws_attempted
                     - 1.07 * (g.home_offensive_rebounds::float
                               / (g.home_offensive_rebounds + (g.away_rebounds - g.away_offensive_rebounds)))
                     * (g.home_field_goals_attempted - g.home_field_goals_made)
                     + COALESCE(g.home_total_turnovers, 0)
                ELSE g.home_field_goals_attempted + 0.4 * g.home_free_throws_attempted + COALESCE(g.home_total_turnovers, 0)
           END )
        ) * 0.5 AS bbr_poss,
        (g.away_score - g.home_score)  AS margin
    FROM nba.games g
    WHERE g.status = 'FINAL'
      AND g.season_id IS NOT NULL
      AND g.game_type IN ('REG','POST','PLAYIN')
)
SELECT * FROM team_games
ORDER BY season_id, team_id, game_date, game_id
"""

# ── Columns used for cumulative sums ────────────────────────────────────────

CUM_SUM_COLS = [
    "points", "points_allowed", "margin",
    "fgm", "fga", "fgm3", "fga3", "ftm", "fta",
    "reb", "ast", "stl", "blk", "tov", "pf",
    "off_reb", "bbr_poss",
    "opp_fgm", "opp_fga", "opp_fgm3", "opp_fga3",
    "opp_ftm", "opp_fta", "opp_reb", "opp_ast",
    "opp_stl", "opp_blk", "opp_tov", "opp_pf",
    "opp_off_reb",
]

# ── Derived rate formulas (applied per-row after cumulative sums) ───────────


def _compute_tier2(gs: int, row: dict) -> dict:
    """Per-game averages from raw cumulatives."""
    pts   = row.get("cum_points", 0) or 0
    opp   = row.get("cum_points_allowed", 0) or 0
    margin = row.get("cum_margin", 0) or 0
    fgm   = row.get("cum_fgm", 0) or 0
    fga   = row.get("cum_fga", 0) or 0
    fgm3  = row.get("cum_fgm3", 0) or 0
    fga3  = row.get("cum_fga3", 0) or 0
    ftm   = row.get("cum_ftm", 0) or 0
    fta   = row.get("cum_fta", 0) or 0
    reb   = row.get("cum_reb", 0) or 0
    ast   = row.get("cum_ast", 0) or 0
    stl   = row.get("cum_stl", 0) or 0
    blk   = row.get("cum_blk", 0) or 0
    tov   = row.get("cum_tov", 0) or 0
    pf    = row.get("cum_pf", 0) or 0

    return {
        "cum_ppg":       _div(pts, gs, 2),
        "cum_oppg":      _div(opp, gs, 2),
        "cum_margin_pg": _div(margin, gs, 2),
        "cum_fg_pct":    _div(fgm, fga, 4),
        "cum_fg3_pct":   _div(fgm3, fga3, 4),
        "cum_ft_pct":    _div(ftm, fta, 4),
        "cum_reb_pg":    _div(reb, gs, 2),
        "cum_ast_pg":    _div(ast, gs, 2),
        "cum_stl_pg":    _div(stl, gs, 2),
        "cum_blk_pg":    _div(blk, gs, 2),
        "cum_tov_pg":    _div(tov, gs, 2),
        "cum_pf_pg":     _div(pf, gs, 2),
    }


def _compute_tier3(gs: int, row: dict) -> dict:
    """Advanced efficiency metrics."""
    pts  = row.get("cum_points", 0) or 0
    opp  = row.get("cum_points_allowed", 0) or 0
    fgm  = row.get("cum_fgm", 0) or 0
    fga  = row.get("cum_fga", 0) or 0
    fgm3 = row.get("cum_fgm3", 0) or 0
    fga3 = row.get("cum_fga3", 0) or 0
    ftm  = row.get("cum_ftm", 0) or 0
    fta  = row.get("cum_fta", 0) or 0
    tov  = row.get("cum_tov", 0) or 0
    reb  = row.get("cum_reb", 0) or 0

    opp_fgm = row.get("cum_opp_fgm", 0) or 0
    opp_fga = row.get("cum_opp_fga", 0) or 0
    opp_fgm3 = row.get("cum_opp_fgm3", 0) or 0
    opp_fga3 = row.get("cum_opp_fga3", 0) or 0
    opp_fta = row.get("cum_opp_fta", 0) or 0
    opp_tov = row.get("cum_opp_tov", 0) or 0
    opp_reb = row.get("cum_opp_reb", 0) or 0

    # basketball-reference game possessions, summed per game from the CTE.
    # BBRef uses ONE symmetric game possession (0.5*(TmPoss+OppPoss), constant
    # 1.07) for BOTH ORTG and DRTG, so the single cum_bbr_poss drives both.
    poss = row.get("cum_bbr_poss", 0) or 0
    opp_poss = max(poss, 1)  # same symmetric count for both ratings (BBRef)
    cum_orb   = row.get("cum_off_reb", 0) or 0
    cum_opp_orb = row.get("cum_opp_off_reb", 0) or 0
    if not (poss > 0):
        # Defensive fallback if BBRef column is somehow absent: Dean-Oliver.
        poss = max(fga + 0.44 * fta + tov - cum_orb, 1)
        opp_poss = max(opp_fga + 0.44 * opp_fta + opp_tov - cum_opp_orb, 1)
    avg_poss = poss if gs > 0 else 0

    # Pace Factor = possessions per 48 minutes (BBRef). TmMP/5 = the "five-man
    # unit" minutes, ~48 for regulation, higher in OT games. row carries the
    # cumulated team minutes (cum_team_min) when provided; fall back to games x 48.
    cum_min = row.get("cum_team_min", 0) or 0
    if cum_min > 0 and gs > 0:
        # PaceFactor = 48*(TmPoss+OppPoss)/(2*(TmMP/5)). Since avg_poss already
        # holds 0.5*(TmPoss+OppPoss), (TmPoss+OppPoss)=2*avg_poss and the 2s
        # cancel: Pace = 48*avg_poss/(TmMP/5).
        est_pace = _div(48.0 * avg_poss, cum_min / 5.0, 2)
    else:
        est_pace = _div(avg_poss, gs, 2)

    ortg = _div(pts, _div(poss, 100, 2))
    drtg = _div(opp, _div(opp_poss, 100, 2), 2)
    net_ortg = round((ortg or 0.0) - (drtg or 0.0), 2)

    # eFG% = (FGM + 0.5*3PM) / FGA
    efg = _div(fgm + 0.5 * fgm3, fga, 4)
    opp_efg = _div(opp_fgm + 0.5 * opp_fgm3, opp_fga, 4)

    # Turnover rate (BBRef TOV%): TOV per 100 plays, where a play =
    # FGA + 0.44*FTA + TOV (NOT the weighted possession).
    tov_plays = fga + 0.44 * fta + tov
    opp_tov_plays = opp_fga + 0.44 * opp_fta + opp_tov
    tov_rate = _div(tov, tov_plays, 4)
    opp_tov_rate = _div(opp_tov, opp_tov_plays, 4)

    # Free throw rate = FTA / FGA
    ft_rate = _div(fta, fga, 4)

    # 3PA rate = 3PA / FGA
    three_rate = _div(fga3, fga, 4)

    cum_ast_v = row.get("cum_ast", 0) or 0
    # Assist ratio = AST / (FGM) — what % of makes were assisted
    # (approximate; true assist ratio uses possessions)
    ast_ratio = _div(cum_ast_v, fgm, 4)

    # Steal rate = STL / opp_possessions
    stl_rate = _div(row.get("cum_stl", 0) or 0, opp_poss, 4)

    # Block rate (BBRef BLK%): blocks per opponent 2-POINT attempt
    # (BLK/(OppFGA - Opp3PA)).
    blk_rate = _div(row.get("cum_blk", 0) or 0, opp_fga - opp_fga3, 4)

    return {
        "cum_ortg":      ortg,
        "cum_drtg":      drtg,
        "cum_net_ortg":  net_ortg,
        "cum_pace":      est_pace,
        "cum_efg_pct":   efg,
        "cum_opp_efg_pct": opp_efg,
        "cum_tov_rate":  tov_rate,
        "cum_opp_tov_rate": opp_tov_rate,
        "cum_ft_rate":   ft_rate,
        "cum_3pa_rate":  three_rate,
        "cum_ast_ratio": ast_ratio,
        "cum_stl_rate":  stl_rate,
        "cum_blk_rate":  blk_rate,
    }


def _div(a: float, b: float, precision: int = 4) -> float:
    """Safe division returning 0.0 when divisor is zero."""
    return round(a / b, precision) if b else 0.0


# ── Bulk upsert helper ──────────────────────────────────────────────────────

ALL_COLS = [
    "game_id", "team_id", "team_side", "season_id", "game_date",
    "prev_game_id", "prev_game_date", "prev_game_id_season", "prev_game_date_season",
    "games_played",
    "cum_points", "cum_points_allowed", "cum_margin",
    "cum_fgm", "cum_fga", "cum_fgm3", "cum_fga3",
    "cum_ftm", "cum_fta",
    "cum_reb", "cum_ast", "cum_stl", "cum_blk", "cum_tov", "cum_pf",
    "cum_opp_fgm", "cum_opp_fga", "cum_opp_fgm3", "cum_opp_fga3",
    "cum_opp_ftm", "cum_opp_fta", "cum_opp_reb",
    "cum_opp_ast", "cum_opp_stl", "cum_opp_blk", "cum_opp_tov", "cum_opp_pf",
    "cum_ppg", "cum_oppg", "cum_margin_pg",
    "cum_fg_pct", "cum_fg3_pct", "cum_ft_pct",
    "cum_reb_pg", "cum_ast_pg", "cum_stl_pg", "cum_blk_pg",
    "cum_tov_pg", "cum_pf_pg",
    "cum_ortg", "cum_drtg", "cum_net_ortg", "cum_pace",
    "cum_efg_pct", "cum_opp_efg_pct",
    "cum_tov_rate", "cum_opp_tov_rate",
    "cum_ft_rate", "cum_3pa_rate",
    "cum_ast_ratio", "cum_stl_rate", "cum_blk_rate",

    # Tier 5: Team quality
    "cum_win_pct",
    "venue_win_pct_season",
]

UPSERT_COLS = [c for c in ALL_COLS if c not in ("game_id", "team_side")]


def _bulk_upsert(engine: Engine, rows: list[dict]) -> None:
    """Upsert rows into cumulative_game_stats via insert … on conflict."""
    if not rows:
        return
    col_names = ", ".join(ALL_COLS)
    update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPSERT_COLS)
    upsert_sql = f"""
        INSERT INTO {CUM_TABLE} ({col_names})
        VALUES ({", ".join(f":{c}" for c in ALL_COLS)})
        ON CONFLICT (game_id, team_side) DO UPDATE SET {update_set}
    """
    with engine.begin() as conn:
        conn.execute(sa_text(upsert_sql), rows)
    logger.info("Upserted %d rows into %s.", len(rows), CUM_TABLE)


# ── Main populator ──────────────────────────────────────────────────────────


def populate_cumulative_stats(
    db_url: str,
    seasons: Optional[list[int]] = None,
    force_rebuild: bool = False,
) -> dict[str, int]:
    """Populate nba.cumulative_game_stats from scratch or incrementally.

    Parameters
    ----------
    db_url :
        PostgreSQL connection string (sync).
    seasons :
        If set, only process these season years.
        If None, all FINAL games are processed.
    force_rebuild :
        If True, drop and re-create the table completely.
        If False, only process games not yet in the table (incremental).

    Returns
    -------
    dict
        Summary of rows processed.
    """
    engine = create_engine(db_url)
    try:
        return _populate(engine, seasons=seasons, force_rebuild=force_rebuild)
    finally:
        engine.dispose()


def _populate(
    engine: Engine,
    seasons: Optional[list[int]] = None,
    force_rebuild: bool = False,
) -> dict[str, int]:
    """Internal implementation."""
    summary: dict[str, int] = {"rows_processed": 0}

    if force_rebuild:
        with engine.begin() as conn:
            conn.execute(sa_text(f"DROP TABLE IF EXISTS {CUM_TABLE}"))
            logger.info("Dropped %s (force_rebuild=True).", CUM_TABLE)

    # ── Ensure table exists ──
    with engine.begin() as conn:
        conn.execute(sa_text(CREATE_TABLE_SQL))
        # Idempotent migration for venue-scoped season win pct on existing DBs
        conn.execute(
            sa_text(
                "ALTER TABLE nba.cumulative_game_stats "
                "ADD COLUMN IF NOT EXISTS venue_win_pct_season DOUBLE PRECISION"
            )
        )
        logger.info("Table %s ready.", CUM_TABLE)

    # ── Load per-game team box scores ──
    team_game_sql = GET_TEAM_GAME_SQL
    if seasons:
        season_list = ", ".join(str(s) for s in seasons)
        team_game_sql = team_game_sql.replace(
            "WHERE g.status = 'FINAL'\n      AND g.season_id IS NOT NULL",
            f"WHERE g.status = 'FINAL'\n      AND g.season_id IN ({season_list})",
        )
    df = pd.read_sql(team_game_sql, engine)
    logger.info("Loaded %d per-game team rows.", len(df))

    # ── Per-game team minutes (for the BBRef per-48 Pace Factor) ──
    # TmMP = total player-minutes for the row's team in that game (~240 reg,
    # more in OT). Parsed from player_game_stats "MM:SS" strings.
    mins_sql = """
        SELECT pgs.game_id, pgs.team_id, pgs.minutes
        FROM nba.player_game_stats pgs
        JOIN nba.games g ON g.id = pgs.game_id
        WHERE g.status = 'FINAL'
          AND pgs.minutes IS NOT NULL AND pgs.minutes != ''
    """
    if seasons:
        season_list = ", ".join(str(s) for s in seasons)
        mins_sql = mins_sql.replace("WHERE g.status = 'FINAL'",
                                    f"WHERE g.status = 'FINAL' AND g.season_id IN ({season_list})")
    pgs = pd.read_sql(mins_sql, engine)
    if not pgs.empty:
        tm = pgs["minutes"].astype(str).str.extract(r"^(\d+):?(\d*)$")
        tm[0] = pd.to_numeric(tm[0], errors="coerce").fillna(0)
        tm[1] = pd.to_numeric(tm[1], errors="coerce").fillna(0)
        pgs["team_minutes"] = tm[0] + tm[1] / 60.0
        pgs = pgs.groupby(["game_id", "team_id"], as_index=False)["team_minutes"].sum()
        df = df.merge(
            pgs[["game_id", "team_id", "team_minutes"]],
            on=["game_id", "team_id"], how="left",
        )
        df["team_minutes"] = df["team_minutes"].fillna(0.0)
    else:
        df["team_minutes"] = 0.0
    logger.info("Per-game team minutes loaded (%d rows).", len(pgs))

    if df.empty:
        logger.warning("No team-game data found — nothing to process.")
        return summary

    # ── Load existing keys for incremental skip ──
    existing: set[tuple[int, str]] = set()
    if not force_rebuild:
        existing_df = pd.read_sql(
            f"SELECT game_id, team_side FROM {CUM_TABLE}", engine
        )
        existing = set(
            (int(row["game_id"]), str(row["team_side"]))
            for _, row in existing_df.iterrows()
        )
        logger.info("Already have %d cumulative rows — will skip them.", len(existing))
        is_new = df.apply(
            lambda r: (int(r["game_id"]), str(r["team_side"])) not in existing,
            axis=1,
        )
        if not is_new.any():
            logger.info("Nothing new to process.")
            return summary
        # Affected team-seasons = team-seasons that have at least one new game.
        # We must recompute cumulative stats over the FULL team-season history
        # (existing rows + new rows), otherwise the new rows would get running
        # totals that restart at 1 (e.g. a team's 31st game would show
        # games_played=1). Keep every row for affected team-seasons and let the
        # upsert refresh them all (this also heals any corrected box scores).
        affected = set(
            (int(r["team_id"]), int(r["season_id"]))
            for _, r in df[is_new].iterrows()
        )
        keep = df.apply(
            lambda r: (int(r["team_id"]), int(r["season_id"])) in affected,
            axis=1,
        )
        df = df[keep].copy()
        logger.info(
            "Recomputing %d rows across %d affected team-seasons (%d new games).",
            len(df), len(affected), int(is_new.sum()),
        )

    # ── Sort by (team, season, date, game_id) for cumulative computation ──
    df.sort_values(["team_id", "season_id", "game_date", "game_id"], inplace=True)

    # ── Prior-game pointers (LAG; NULL on a team's first appearance) ──
    # Computed in the SORTED order so the previous row is the immediately
    # prior game. _season = within the same season (NULL on first game of a
    # season); plain prev_game_id = across seasons (carries over the seam).
    df["prev_game_id_season"] = df.groupby(["team_id", "season_id"], sort=False)["game_id"].shift(1)
    df["prev_game_date_season"] = df.groupby(["team_id", "season_id"], sort=False)["game_date"].shift(1)
    df["prev_game_id"] = df.groupby("team_id", sort=False)["game_id"].shift(1)
    df["prev_game_date"] = df.groupby("team_id", sort=False)["game_date"].shift(1)

    # ── Keep a copy of per-game data for momentum/recency stats ──
    # We need single-game values before cumsum overwrites them.
    df_raw = df.copy()

    # Compute per-game advanced metrics from single-game box scores
    df_raw["won"] = (df_raw["points"] > df_raw["points_allowed"]).astype(int)

    def _per_game_ortg(r):
        r_pts = r.get("points", 0) or 0
        # basketball-reference refined possessions for this game's team.
        r_poss = r.get("bbr_poss", 0) or 0
        if r_poss <= 0:
            # Fallback: Dean-Oliver possessions (subtract offensive rebounds so
            # offensive boards don't inflate the count).
            r_fga = r.get("fga", 0) or 0
            r_fta = r.get("fta", 0) or 0
            r_tov = r.get("tov", 0) or 0
            r_orb = r.get("off_reb", 0) or 0
            r_poss = max(r_fga + 0.44 * r_fta + r_tov - r_orb, 1)
        return r_pts / r_poss * 100

    df_raw["pg_ortg"] = df_raw.apply(_per_game_ortg, axis=1)

    def _per_game_drtg(r):
        r_pts = r.get("points_allowed", 0) or 0
        # BBRef uses the SAME symmetric game possession for ORTG and DRTG.
        r_poss = r.get("bbr_poss", 0) or 0
        if r_poss <= 0:
            # Fallback via opponent box-score possessions.
            r_opp_fga = r.get("opp_fga", 0) or 0
            r_opp_fta = r.get("opp_fta", 0) or 0
            r_opp_tov = r.get("opp_tov", 0) or 0
            r_opp_orb = r.get("opp_off_reb", 0) or 0
            r_poss = max(r_opp_fga + 0.44 * r_opp_fta + r_opp_tov - r_opp_orb, 1)
        return r_pts / r_poss * 100

    df_raw["pg_drtg"] = df_raw.apply(_per_game_drtg, axis=1)
    df_raw["pg_net_rtg"] = df_raw["pg_ortg"] - df_raw["pg_drtg"]

    def _per_game_efg(r):
        fgm = r.get("fgm", 0) or 0
        fgm3 = r.get("fgm3", 0) or 0
        fga = r.get("fga", 0) or 0
        return (fgm + 0.5 * fgm3) / fga if fga > 0 else 0.0

    df_raw["pg_efg_pct"] = df_raw.apply(_per_game_efg, axis=1)

    # ── Compute backward-looking cumulative team quality per team/season ──
    #    (Rolling momentum/recency stats now live in nba.team_rolling_stats.)
    grouped_raw = df_raw.groupby(["team_id", "season_id"], sort=False)

    # ── Tier 5: Team quality ──
    df_raw["cum_win_pct"] = grouped_raw["won"].transform(
        lambda s: s.expanding(min_periods=1).mean()
    ).fillna(0.0)
    # Round to 4 decimals
    df_raw["cum_win_pct"] = df_raw["cum_win_pct"].round(4)

    # ── Venue-scoped season win pct (win pct AT this row's venue) ──────────
    # Same expanding mean as cum_win_pct, but partitioned by venue so it
    # measures only home play (team_side='home') or only road play
    # (team_side='away'). Loader projects this as h_home_win_pct_season /
    # a_away_win_pct_season depending on the target game's home/away team.
    grouped_venue = df_raw.groupby(["team_id", "season_id", "team_side"], sort=False)
    df_raw["venue_win_pct_season"] = grouped_venue["won"].transform(
        lambda s: s.expanding(min_periods=1).mean()
    ).fillna(0.0)
    df_raw["venue_win_pct_season"] = df_raw["venue_win_pct_season"].round(4)

    # ── Compute cumulative sums (post-game, includes current game) ──
    # Cumulative: for game N, cumsum() gives stats for games 1..N.
    # The LATERAL JOIN in GAME_QUERY excludes the current game (game_id != g.id)
    # to get pre-game cumulative stats for prediction.
    grouped = df.groupby(["team_id", "season_id"], sort=False)
    cum_sum_cols = CUM_SUM_COLS

    # Treat missing box scores as 0 contribution (like SQL SUM OVER) so the
    # running total carries forward instead of collapsing to 0 at games whose
    # source box score is NULL in nba.games.
    df[cum_sum_cols] = df[cum_sum_cols].fillna(0)
    df[cum_sum_cols] = grouped[cum_sum_cols].cumsum()
    df["games_played"] = grouped.cumcount() + 1

    # Cumulative team minutes (BBRef Pace Factor denominator). Include own row.
    df["cum_team_min"] = grouped["team_minutes"].cumsum()

    # Running count of games with a REAL BBRef possession value, per team-season.
    # Computed from df_raw (raw bbr_poss still has NaN for any uncovered game,
    # since df at this point has had bbr_poss filled to 0). With the CTE box-derived
    # formula this is ~always complete; kept only as a safety gate for the fallback.
    df["poss_est_games"] = df_raw.groupby(["team_id", "season_id"], sort=False)["bbr_poss"].transform(
        lambda s: s.notna().astype(int).cumsum()
    )

    # ── Define Tier 4/5 column names for merge (cumulative only; rolling
    #    stats live in nba.team_rolling_stats) ──
    tier45_cols = [
        "cum_win_pct",
        "venue_win_pct_season",
    ]

    # ── Build result rows ──
    rows: list[dict] = []
    for idx, row in df.iterrows():
        gs = int(row["games_played"])

        # Build dict with cum_ prefixes from raw DataFrame columns
        r = {
            "game_id":             int(row["game_id"]),
            "team_id":             int(row["team_id"]),
            "team_side":           str(row["team_side"]),
            "season_id":           int(row["season_id"]),
            "game_date":           row["game_date"].isoformat() if hasattr(row["game_date"], "isoformat") else row["game_date"],
            "prev_game_id":        int(row["prev_game_id"]) if pd.notna(row.get("prev_game_id")) else None,
            "prev_game_date":      (row["prev_game_date"].isoformat() if pd.notna(row.get("prev_game_date")) else None),
            "prev_game_id_season": int(row["prev_game_id_season"]) if pd.notna(row.get("prev_game_id_season")) else None,
            "prev_game_date_season": (row["prev_game_date_season"].isoformat() if pd.notna(row.get("prev_game_date_season")) else None),
            "games_played":        gs,
        }
        for col in cum_sum_cols:
            val = row[col] if col in row else 0
            # Possessions are fractional; keep them float. Everything else rounds
            # to int (count-based stats).
            if col in ("bbr_poss",):
                r[f"cum_{col}"] = float(val) if val is not None else 0.0
            else:
                r[f"cum_{col}"] = int(val) if val is not None else 0

        r["poss_est_games"] = int(row["poss_est_games"]) if "poss_est_games" in row else gs
        r["cum_team_min"] = float(row.get("cum_team_min", 0) or 0)

        tier2 = _compute_tier2(gs, r)
        tier3 = _compute_tier3(gs, r)
        r.update(tier2)
        r.update(tier3)

        # Look up Tier 4/5 values from df_raw (same sort order, same index)
        raw_row = df_raw.loc[idx]
        for col in tier45_cols:
            val = raw_row.get(col, None)
            if val is not None and not (isinstance(val, float) and np.isnan(val)):
                r[col] = round(float(val), 4) if isinstance(val, (float, np.floating)) else val
            else:
                r[col] = None

        rows.append(r)

    logger.info("Prepared %d cumulative rows for upsert.", len(rows))

    # ── Bulk upsert ──
    _bulk_upsert(engine, rows)
    summary["rows_processed"] = len(rows)
    return summary
