"""Build mlb.team_runs_vs_arm — cumulative runs-scored-per-game vs starter arm.

Replaces the 8 correlated rpg_vs_arm scalar subqueries in mlb.data_loader
(h_rpg_vs_lhp / _rhp, a_rpg_vs_lhp / _rhp, plus the vs_opp_hand duplicates).
Those subqueries re-scan the team's full game history per game row (~6.9s, ~21%
of every load_games call). This precomputes the same value ONCE so the loader
can LEFT JOIN the previous Final row instead.

Semantics (MUST match the loader's inline AVG exactly, leak-safe):
  * Home offense (team_side='home'): avg of g2.home_score over prior FINAL
    SAME-SEASON games where this team was home AND the OPPOSING starter
    (g2.away_pitcher_name -> players.throws) was 'L' or 'R'.
  * Away offense (team_side='away'): avg of g2.away_score where this team was
    away AND g2.home_pitcher_name throws was 'L' or 'R'.
  * Only throws='L' and throws='R' are collected ('S'/'NULL' excluded).
  * CURRENT ROW cumulation (through-date, own result included), mirroring
    mlb.team_rolling_stats and mlb.team_ops_vs_arm. The loader reads the
    PREVIOUS row, so the model sees the through-date value entering the target
    game -- leak-safe.
  * Leak guard preserved: 30-min cutoff, same season_id, status='FINAL' (the
    loader's g_prev LATERAL applies these on read).

Grain: (game_id, team_id, season_id, team_side, arm), PK(game_id, team_side, arm)
Columns: runs_vs_arm (win: cumulative runs scored), games_vs_arm (count),
         rpg_vs_arm = runs_vs_arm / games_vs_arm (round 4 dp).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys as _sys  # noqa: E402

if str(REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402


def _build_sql(season: int | None, incremental: bool) -> str:
    season_where = "AND g2.season_id = :season" if season is not None else ""

    insert_cols = (
        "game_id, team_id, season_id, team_side, arm,"
        " runs_vs_arm, games_vs_arm, rpg_vs_arm"
    )
    if incremental:
        insert_stmt = (
            "INSERT INTO mlb.team_runs_vs_arm (" + insert_cols + ")\n"
            "SELECT game_id, team_id, season_id, team_side, arm,"
            " runs_vs_arm, games_vs_arm, rpg_vs_arm\n"
            "FROM (\n"
        )
        tail = (
            "\n) AS full_calc\n"
            "WHERE (game_id, team_side, arm) NOT IN"
            " (SELECT game_id, team_side, arm FROM mlb.team_runs_vs_arm)"
        )
    else:
        insert_stmt = "INSERT INTO mlb.team_runs_vs_arm (" + insert_cols + ")\n"
        tail = ""

    body = f"""
WITH pitcher_arms AS (
    -- mlb.players can hold duplicate rows for the same pitcher name; collapse to
    -- one (name -> throws) via lowest id so the joins below never fan out.
    SELECT DISTINCT ON (name) name, throws
    FROM mlb.players
    WHERE throws IN ('L','R')
    ORDER BY name, id
),
legs AS (
    -- Home offense: this team was home; opposing starter = AWAY pitcher.
    SELECT
        g2.id                                      AS game_id,
        g2.home_team_id                            AS team_id,
        g2.season_id                               AS season_id,
        'home'                                     AS team_side,
        pa.throws                                  AS arm,
        g2.home_score                              AS runs,
        g2.date                                    AS game_date,
        g2.id                                      AS game_id_ord
    FROM mlb.games g2
    LEFT JOIN pitcher_arms pa ON pa.name = g2.away_pitcher_name
    WHERE g2.status = 'FINAL' AND pa.throws IS NOT NULL
      --SEASON_WHERE--

    UNION ALL

    -- Away offense: this team was away; opposing starter = HOME pitcher.
    SELECT
        g2.id                                      AS game_id,
        g2.away_team_id                            AS team_id,
        g2.season_id                               AS season_id,
        'away'                                     AS team_side,
        pa.throws                                  AS arm,
        g2.away_score                              AS runs,
        g2.date                                    AS game_date,
        g2.id                                      AS game_id_ord
    FROM mlb.games g2
    LEFT JOIN pitcher_arms pa ON pa.name = g2.home_pitcher_name
    WHERE g2.status = 'FINAL' AND pa.throws IS NOT NULL
      --SEASON_WHERE--
),
accum AS (
    -- CURRENT ROW cumulative through each game (own result included), ordered
    -- by (date, game_id) within (team, season, side, arm).
    SELECT
        game_id, team_id, season_id, team_side, arm, game_date, game_id_ord,
        SUM(runs) OVER w AS runs,
        SUM(1)    OVER w AS games
    FROM legs
    WINDOW w AS (
        PARTITION BY team_id, season_id, team_side, arm
        ORDER BY game_date, game_id_ord
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
)
SELECT
    game_id, team_id, season_id, team_side, arm,
    runs,
    games,
    ROUND(runs::numeric / NULLIF(games, 0), 4) AS rpg_vs_arm
FROM accum
"""
    sql = insert_stmt + body + tail
    return sql.replace("--SEASON_WHERE--", season_where)


def populate_team_runs_vs_arm(engine=None, season: int | None = None,
                              incremental: bool = False) -> int:
    """Rebuild mlb.team_runs_vs_arm.

    - season=<int> : DELETE that season, then rebuild it.
    - season=None & incremental=False : TRUNCATE + rebuild entire table.
    - incremental=True : insert only rows for games not already present.
    """
    eng = engine or create_engine(
        PSYCOPG2_DATABASE_URL or settings.database_url_sync,
        connect_args={"options": "-c jit=off"},
    )
    sql = _build_sql(season, incremental)
    t0 = time.time()
    with eng.begin() as c:
        if incremental:
            pass
        elif season is not None:
            c.execute(text("DELETE FROM mlb.team_runs_vs_arm WHERE season_id = :s"),
                      {"s": season})
        else:
            c.execute(text("TRUNCATE mlb.team_runs_vs_arm"))
        params = {"season": season} if season is not None else {}
        res = c.execute(text(sql).bindparams(**params))
        inserted = res.rowcount
        total = c.execute(text("SELECT count(*) FROM mlb.team_runs_vs_arm")).scalar()
    print(f"team_runs_vs_arm: inserted={inserted} total={total} "
          f"elapsed={time.time()-t0:.1f}s", flush=True)
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--incremental", action="store_true")
    args = ap.parse_args()
    populate_team_runs_vs_arm(season=args.season, incremental=args.incremental)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
