"""
Populate mlb.team_ops_vs_arm — cumulative per-game table of a team's OFFENSE
OPS and WINS vs opposing starter hand (LHP / RHP), over a through-date window.

This is the leak-safe, precomputable replacement for the platoon LATERAL
subqueries in mlb.data_loader (plato_h_lhp / plato_h_rhp / plato_a_lhp /
plato_a_rhp), which are the dominant cost of load_games() (~145-290s).

!================================================================================!
!  GAME ROWS INCLUDE THE RESULT OF THE GAME!  (CURRENT ROW semantics)            !
!================================================================================!
!  EACH row stores the cumulative stats THROUGH that game (its own result IS     !
!  included): e.g. a team facing its 40th lefty of the season includes that      !
!  game's own plate appearances. The loader must read the PREVIOUS Final row     !
!  strictly before the target — correct AND leak-safe (same contract as          !
!  mlb.team_rolling_stats; do NOT switch to "... AND 1 PRECEDING").              !
!--------------------------------------------------------------------------------!

ACCURACY / LEAK CONTRACT (MUST stay byte-identical to the current plato LATERALs):
  * OPS formula   : (H + BB + HBP + TB) / (AB + BB + HBP + SF)
  * Arm attribution: OPPOSING starter's hand:
        - home offense (team_side='home') -> games.away_pitcher_name -> players.throws
        - away offense (team_side='away') -> games.home_pitcher_name -> players.throws
      Only throws IN ('L','R') collect. 'S' (switch, 3 rows) and NULL throws are
      EXCLUDED from both buckets — identical to the loader.
  * Team attribution : home offense -> games.home_team_id; away offense -> games.away_team_id
      (batting_game_stats has NO team_id; team resolves through games.)
  * Leak-safety  : window ordered by (g2.date, g2.id) ACROSS the whole table,
      grouped per (team_id, season_id, team_side, arm). Rows only from FINAL
      games. The loader later reads the PREVIOUS Final row strictly before the
      target with date < g.date - 30min, so the 30-minute same-day bound is
      preserved at READ time (this table just stores exact through-date values).
  * WIN vs arm   : MAX(CASE ...) — counted ONCE per (game, side, arm), NOT per
      batter row (per_game is grouped by game_id; SUM would over-count).
  * grain        : (team_id, season_id, game_id, team_side, arm)

Rebuild strategies (both compute the FULL windowed query so windows see all
history — never filter the source to only new games, or windowed stats go NULL):
  * incremental=True  : emit only rows whose (game_id, team_side, arm) is not yet
      in the table. Fast, but does NOT pick up retroactive corrections to already
      stored games.
  * full / season=... : DELETE that season and rebuild it completely. Correct for
      the current (in-progress) season, which is what mlb-stats-refresh uses.

Usage:
    ../venv/bin/python -m backend.app.handicapping.mlb.populate_team_ops_vs_arm
    --incremental | --season N
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys  # noqa: E402

if str(REPO_ROOT) not in sys.path:  # noqa: E402
    sys.path.insert(0, str(REPO_ROOT))  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402


# One set-based pass over batting_game_stats → per-game contributor rows, then
# window-accumulated (CURRENT ROW) into the final per-game cumulative.
def _build_sql(season: int | None, incremental: bool) -> str:
    season_where = "AND g2.season_id = :season" if season is not None else ""

    insert_cols = (
        "game_id, team_id, season_id, team_side, arm,"
        " ab, h, bb, hbp, sf, tb, ops_vs_arm, wins_vs_arm, games_vs_arm"
    )
    if incremental:
        insert_stmt = (
            "INSERT INTO mlb.team_ops_vs_arm (" + insert_cols + ")\n"
            "SELECT game_id, team_id, season_id, team_side, arm,"
            " ab, h, bb, hbp, sf, tb, ops_vs_arm, wins_vs_arm, games_vs_arm\n"
            "FROM (\n"
        )
        tail = (
            "\n) AS full_calc\n"
            "WHERE (game_id, team_side, arm) NOT IN"
            " (SELECT game_id, team_side, arm FROM mlb.team_ops_vs_arm)"
        )
    else:
        insert_stmt = "INSERT INTO mlb.team_ops_vs_arm (" + insert_cols + ")\n"
        tail = ""

    body = f"""
WITH per_game AS (
    -- One row per (game, offense side, opposing arm): this side's raw batting
    -- sums + win flag + whether the game counts toward the arm bucket.
    SELECT
        g2.id                                                        AS game_id,
        CASE WHEN bg.team_side = 'home' THEN g2.home_team_id
             ELSE g2.away_team_id END                                AS team_id,
        g2.season_id                                                 AS season_id,
        bg.team_side                                                 AS team_side,
        pl2.throws                                                   AS arm,
        SUM(bg.at_bats)                                              AS ab,
        SUM(bg.hits)                                                 AS h,
        SUM(bg.base_on_balls)                                        AS bb,
        SUM(bg.hit_by_pitch)                                         AS hbp,
        SUM(bg.sacrifice_flies)                                      AS sf,
        SUM(bg.total_bases)                                          AS tb,
        -- MAX (not SUM): per_game is grouped per game_id, so a single game
        -- with N batter rows must count as ONE win, not N.
        MAX(CASE
                WHEN bg.team_side = 'home' AND g2.home_score > g2.away_score THEN 1
                WHEN bg.team_side = 'away' AND g2.away_score > g2.home_score THEN 1
                ELSE 0
            END)                                                     AS wins,
        g2.date                                                      AS game_date,
        g2.id                                                        AS game_id_ord
    FROM mlb.batting_game_stats bg
    JOIN mlb.games g2                 ON g2.id = bg.game_id
    LEFT JOIN mlb.players pl2 ON pl2.name = CASE
        WHEN bg.team_side = 'home' THEN g2.away_pitcher_name
        ELSE g2.home_pitcher_name END
    WHERE g2.status = 'FINAL'
      AND pl2.throws IN ('L','R')
      --SEASON_WHERE--
    GROUP BY
        g2.id,
        CASE WHEN bg.team_side = 'home' THEN g2.home_team_id
             ELSE g2.away_team_id END,
        g2.season_id,
        bg.team_side,
        pl2.throws,
        g2.date
),
accum AS (
    -- CURRENT ROW cumulative through each game (own result included), ordered
    -- by (date, game_id) within (team, season, side, arm).
    SELECT
        game_id, team_id, season_id, team_side, arm, game_date, game_id_ord,
        SUM(ab) OVER w AS ab, SUM(h)  OVER w AS h,  SUM(bb) OVER w AS bb,
        SUM(hbp)OVER w AS hbp, SUM(sf) OVER w AS sf, SUM(tb) OVER w AS tb,
        SUM(wins) OVER w AS wins,
        SUM(1)    OVER w AS games
    FROM per_game
    WINDOW w AS (
        PARTITION BY team_id, season_id, team_side, arm
        ORDER BY game_date, game_id_ord
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    )
)
SELECT
    game_id, team_id, season_id, team_side, arm,
    ab, h, bb, hbp, sf, tb,
    ROUND(((h + bb + hbp + tb)::numeric)
          / NULLIF((ab + bb + hbp + sf), 0), 4) AS ops_vs_arm,
    wins, games
FROM accum
"""
    sql = insert_stmt + body + tail
    return sql.replace("--SEASON_WHERE--", season_where)


def populate_team_ops_vs_arm(engine=None, season: int | None = None,
                             incremental: bool = False) -> int:
    """Rebuild mlb.team_ops_vs_arm.

    - season=<int> : DELETE that season, then rebuild it (correct for the
      current/in-progress season on each stats refresh).
    - season=None & incremental=False : TRUNCATE + rebuild entire table.
    - incremental=True : insert only rows for games not already present
      (does NOT handle retroactive corrections).
    """
    eng = engine or create_engine(
        PSYCOPG2_DATABASE_URL or settings.database_url_sync,
        connect_args={"options": "-c jit=off"},
    )
    sql = _build_sql(season=season, incremental=incremental)

    t0 = time.time()
    with eng.begin() as c:
        if incremental:
            pass  # no delete; only insert missing
        else:
            if season is not None:
                c.execute(text("DELETE FROM mlb.team_ops_vs_arm WHERE season_id = :s"),
                          {"s": season})
            else:
                c.execute(text("TRUNCATE mlb.team_ops_vs_arm"))

        params = {"season": season} if season is not None else {}
        res = c.execute(text(sql).bindparams(**params))
        inserted = res.rowcount
        total = c.execute(text("SELECT count(*) FROM mlb.team_ops_vs_arm")).scalar()

    print(f"team_ops_vs_arm: inserted={inserted} total={total} "
          f"elapsed={time.time()-t0:.1f}s", flush=True)
    return total


def _main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--incremental", action="store_true")
    args = ap.parse_args()
    populate_team_ops_vs_arm(season=args.season, incremental=args.incremental)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
