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
  * WIN vs arm   : this side wins the ballgame (home: home_score>away_score;
      away: away_score>home_score) when the opposing starter threw that arm.
  * grain        : (team_id, season_id, game_id, team_side, arm)

Usage:
    ../venv/bin/python app/scripts/populate_mlb_team_ops_vs_arm.py [--season N]
      (no --season  => rebuild the ENTIRE table; --season N => rebuild just that season)
"""
import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text  # noqa: E402

from app.database import async_session  # noqa: E402  (kept for convention / future async)
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402


# ---------------------------------------------------------------------------
# The one set-based pass over batting_game_stats → per-game contributor rows,
# then window-accumulated (CURRENT ROW) into the final per-game cumulative.
# ---------------------------------------------------------------------------
def BUILD_SQL(season):
    season_where = "AND g2.season_id = :season" if season is not None else ""
    return f"""
INSERT INTO mlb.team_ops_vs_arm
    (game_id, team_id, season_id, team_side, arm,
     ab, h, bb, hbp, sf, tb, ops_vs_arm, wins_vs_arm, games_vs_arm)
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
        -- with N batter rows must count as ONE win, not N. MAX collapses the
        -- per-batter rows to a 0/1 per (game, side, arm).
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
""".replace("--SEASON_WHERE--", season_where)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None,
                    help="Only rebuild this season_id; omit to rebuild the whole table.")
    args = ap.parse_args()

    from sqlalchemy import create_engine
    eng = create_engine(PSYCOPG2_DATABASE_URL,
                        connect_args={"options": "-c jit=off"})

    t0 = time.time()
    with eng.begin() as c:
        if args.season is not None:
            print(f"Deleting season {args.season} from mlb.team_ops_vs_arm ...", flush=True)
            c.execute(text("DELETE FROM mlb.team_ops_vs_arm WHERE season_id = :s"),
                      {"s": args.season})
        else:
            print("Truncating mlb.team_ops_vs_arm ...", flush=True)
            c.execute(text("TRUNCATE mlb.team_ops_vs_arm"))

        print("Building per-game + cumulative (CURRENT ROW) rows ...", flush=True)
        params_build = {"season": args.season} if args.season is not None else {}
        res = c.execute(text(BUILD_SQL(args.season)).bindparams(**params_build))
        inserted = res.rowcount
        total = c.execute(text("SELECT count(*) FROM mlb.team_ops_vs_arm")).scalar()

    print(f"inserted={inserted}  total_rows={total}  elapsed={time.time()-t0:.1f}s", flush=True)

    # ---- sanity: how many rows should we expect? teams in table ----
    with eng.connect() as c:
        by_arm = c.execute(text(
            "SELECT arm, count(*) FROM mlb.team_ops_vs_arm GROUP BY arm ORDER BY arm")).fetchall()
        print("rows by arm:", [(a, n) for a, n in by_arm])
        uniq = c.execute(text(
            "SELECT count(DISTINCT (team_id, season_id, team_side, arm)) FROM mlb.team_ops_vs_arm")).scalar()
        print("distinct (team,season,side,arm) groups:", uniq)
    return 0


if __name__ == "__main__":
    sys.exit(main())
