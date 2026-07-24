"""
NBA Cumulative Game Stats

Pre-computes backward-looking cumulative team statistics for the NBA,
stored in nba.cumulative_game_stats.  Each row represents one team in one
game, with all season-to-date (excluding the current game) cumulative
statistics.

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

    PRIMARY KEY (game_id, team_side)
);
"""

# ── SQL: per-game team box-score view ───────────────────────────────────────

GET_TEAM_GAME_SQL = """
WITH team_games AS (
    SELECT
        g.id           AS game_id,
        g.home_team_id AS team_id,
        'home'         AS team_side,
        g.season_id    AS season_id,
        g.date         AS game_date,
        g.home_score   AS points,
        g.away_score   AS points_allowed,
        g.home_field_goals_made        AS fgm,
        g.home_field_goals_attempted   AS fga,
        g.home_three_points_made       AS fgm3,
        g.home_three_points_attempted  AS fga3,
        g.home_free_throws_made        AS ftm,
        g.home_free_throws_attempted   AS fta,
        g.home_rebounds                AS reb,
        g.home_assists                 AS ast,
        COALESCE(g.home_steals, 0)     AS stl,
        COALESCE(g.home_blocks, 0)     AS blk,
        COALESCE(g.home_turnovers, 0)  AS tov,
        COALESCE(g.home_fouls, 0)      AS pf,
        g.away_field_goals_made        AS opp_fgm,
        g.away_field_goals_attempted   AS opp_fga,
        g.away_three_points_made       AS opp_fgm3,
        g.away_three_points_attempted  AS opp_fga3,
        g.away_free_throws_made        AS opp_ftm,
        g.away_free_throws_attempted   AS opp_fta,
        g.away_rebounds                AS opp_reb,
        g.away_assists                 AS opp_ast,
        COALESCE(g.away_steals, 0)     AS opp_stl,
        COALESCE(g.away_blocks, 0)     AS opp_blk,
        COALESCE(g.away_turnovers, 0)  AS opp_tov,
        COALESCE(g.away_fouls, 0)      AS opp_pf,
        (g.home_score - g.away_score)  AS margin
    FROM nba.games g
    WHERE g.status = 'FINAL'
      AND g.season_id IS NOT NULL

    UNION ALL

    SELECT
        g.id           AS game_id,
        g.away_team_id AS team_id,
        'away'         AS team_side,
        g.season_id    AS season_id,
        g.date         AS game_date,
        g.away_score   AS points,
        g.home_score   AS points_allowed,
        g.away_field_goals_made        AS fgm,
        g.away_field_goals_attempted   AS fga,
        g.away_three_points_made       AS fgm3,
        g.away_three_points_attempted  AS fga3,
        g.away_free_throws_made        AS ftm,
        g.away_free_throws_attempted   AS fta,
        g.away_rebounds                AS reb,
        g.away_assists                 AS ast,
        COALESCE(g.away_steals, 0)     AS stl,
        COALESCE(g.away_blocks, 0)     AS blk,
        COALESCE(g.away_turnovers, 0)  AS tov,
        COALESCE(g.away_fouls, 0)      AS pf,
        g.home_field_goals_made        AS opp_fgm,
        g.home_field_goals_attempted   AS opp_fga,
        g.home_three_points_made       AS opp_fgm3,
        g.home_three_points_attempted  AS opp_fga3,
        g.home_free_throws_made        AS opp_ftm,
        g.home_free_throws_attempted   AS opp_fta,
        g.home_rebounds                AS opp_reb,
        g.home_assists                 AS opp_ast,
        COALESCE(g.home_steals, 0)     AS opp_stl,
        COALESCE(g.home_blocks, 0)     AS opp_blk,
        COALESCE(g.home_turnovers, 0)  AS opp_tov,
        COALESCE(g.home_fouls, 0)      AS opp_pf,
        (g.away_score - g.home_score)  AS margin
    FROM nba.games g
    WHERE g.status = 'FINAL'
      AND g.season_id IS NOT NULL
)
SELECT * FROM team_games
ORDER BY season_id, team_id, game_date, game_id
"""

# ── Columns used for cumulative sums ────────────────────────────────────────

CUM_SUM_COLS = [
    "points", "points_allowed", "margin",
    "fgm", "fga", "fgm3", "fga3", "ftm", "fta",
    "reb", "ast", "stl", "blk", "tov", "pf",
    "opp_fgm", "opp_fga", "opp_fgm3", "opp_fga3",
    "opp_ftm", "opp_fta", "opp_reb", "opp_ast",
    "opp_stl", "opp_blk", "opp_tov", "opp_pf",
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

    # Estimated possessions (team = offensive half of the formula)
    # Poss = FGA + 0.44*FTA - ORB + TOV
    # Without ORB, approximate: FGA + 0.44*FTA + TOV
    # For opponent: opp_FGA + 0.44*opp_FTA + opp_TOV
    poss = fga + 0.44 * fta + tov
    opp_poss = opp_fga + 0.44 * opp_fta + opp_tov
    avg_poss = (poss + opp_poss) / 2.0 if gs > 0 else 0

    # Est possessions per game = (avg_poss / games_played) / 2 per game
    # Pace = 48 * (poss + opp_poss) / (2 * games_played * minutes)
    # Without actual minutes, estimate via avg per-game possession count
    est_pace_poss = _div(poss, gs)
    est_opp_pace_poss = _div(opp_poss, gs, 2)
    # Pace formula: possessions per 48 minutes.
    # Without actual game minutes (no total_minutes column in nba.games),
    # compute estimated per-game pace as an approximation.
    # Standard NBA: ~100 possessions/game ≈ ORTG of ~110.
    # We'll store per-game average of estimated possessions * 2 / 1 team
    # as a simplified pace proxy.
    est_pace = _div(poss + opp_poss, gs, 2)

    ortg = _div(pts, _div(poss, 100, 2))
    drtg = _div(opp, _div(opp_poss, 100, 2), 2)
    net_ortg = round((ortg or 0.0) - (drtg or 0.0), 2)

    # eFG% = (FGM + 0.5*3PM) / FGA
    efg = _div(fgm + 0.5 * fgm3, fga, 4)
    opp_efg = _div(opp_fgm + 0.5 * opp_fgm3, opp_fga, 4)

    # Turnover rate = TOV / (FGA + 0.44*FTA + TOV)
    tov_rate = _div(tov, poss, 4)
    opp_tov_rate = _div(opp_tov, opp_poss, 4)

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

    # Block rate = BLK / opp_FGA
    blk_rate = _div(row.get("cum_blk", 0) or 0, opp_fga, 4)

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

    # ── Ensure table exists ──
    with engine.begin() as conn:
        conn.execute(sa_text(CREATE_TABLE_SQL))
        logger.info("Table %s ready.", CUM_TABLE)

    if force_rebuild:
        with engine.begin() as conn:
            conn.execute(sa_text(f"TRUNCATE {CUM_TABLE}"))
            logger.info("Truncated %s (force_rebuild=True).", CUM_TABLE)

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
        mask = df.apply(
            lambda r: (int(r["game_id"]), str(r["team_side"])) not in existing,
            axis=1,
        )
        df = df[mask].copy()
        logger.info("Remaining new rows to process: %d", len(df))
        if df.empty:
            logger.info("Nothing new to process.")
            return summary

    # ── Sort by (team, season, date, game_id) for cumulative computation ──
    df.sort_values(["team_id", "season_id", "game_date", "game_id"], inplace=True)

    # ── Compute cumulative sums (shift(1) = backward-looking) ──
    # Cumulative: for game N, we want stats from games 1..N-1.
    # cumsum() gives games 1..N, shift(1) gives 1..N-1.
    grouped = df.groupby(["team_id", "season_id"], sort=False)
    cum_sum_cols = CUM_SUM_COLS

    df[cum_sum_cols] = grouped[cum_sum_cols].cumsum()
    df[cum_sum_cols] = df.groupby(["team_id", "season_id"], sort=False)[cum_sum_cols].shift(1).fillna(0)
    df["games_played"] = grouped.cumcount()

    # ── Build result rows ──
    rows: list[dict] = []
    for _, row in df.iterrows():
        gs = int(row["games_played"])

        # Build dict with cum_ prefixes from raw DataFrame columns
        r = {
            "game_id":             int(row["game_id"]),
            "team_id":             int(row["team_id"]),
            "team_side":           str(row["team_side"]),
            "season_id":           int(row["season_id"]),
            "game_date":           row["game_date"].isoformat() if hasattr(row["game_date"], "isoformat") else row["game_date"],
            "games_played":        gs,
        }
        for col in cum_sum_cols:
            r[f"cum_{col}"] = int(row[col]) if col in row else 0

        tier2 = _compute_tier2(gs, r)
        tier3 = _compute_tier3(gs, r)
        r.update(tier2)
        r.update(tier3)
        rows.append(r)

    logger.info("Prepared %d cumulative rows for upsert.", len(rows))

    # ── Bulk upsert ──
    _bulk_upsert(engine, rows)
    summary["rows_processed"] = len(rows)
    return summary
