"""Backfill nba.games team boxscore columns from nba.player_game_stats.

Root cause: nba.games home_*/away_* boxscore columns (steals, blocks, turnovers,
rebounds, assists, fouls, field goals, 3pts, free throws) are stale/undercounted
legacy data — no current ingest writes them, and steals/blocks/turnovers sum to
well below the authoritative real values. player_game_stats is the correct,
complete per-player source. This backfill recomputes each team's per-game boxscore
by SUM()ing player_game_stats, then rebuilds the derived stat tables.

Safety guard: only overwrite a game/team when the player_game_stats points-sum for
that team equals the team's actual score (i.e., pgs is complete for that side).
Games/sides where pgs is incomplete are left untouched.

Usage: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_nba_games_boxscores.py
"""
import sys, os, time
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from sqlalchemy import create_engine, text
from app.core.config import settings

DATABASE_URL = settings.database_url_sync

# pgs column -> nba.games suffix (without home_/away_ prefix)
# nba.games team boxscore columns (verified from DB schema):
#   field_goals_made/attempted, three_points_made/attempted,
#   free_throws_made/attempted, rebounds, assists, steals, blocks, turnovers, fouls
PGS_TO_GAME = {
    "field_goals_made": "field_goals_made",
    "field_goals_attempted": "field_goals_attempted",
    "three_pointers_made": "three_points_made",
    "three_pointers_attempted": "three_points_attempted",
    "free_throws_made": "free_throws_made",
    "free_throws_attempted": "free_throws_attempted",
    "rebounds_total": "rebounds",
    "rebounds_offensive": "offensive_rebounds",
    "rebounds_defensive": "defensive_rebounds",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "fouls_personal": "fouls",
    "points": "points",
}
# columns that nba.games actually stores (exclude points — no points column)
STORE_COLS = [c for c in PGS_TO_GAME.values() if c != "points"]
# Reverse: nba.games column -> pgs key (to read from the agg dict correctly)
GAME_TO_PGS = {v: k for k, v in PGS_TO_GAME.items() if v != "points"}


def main():
    engine = create_engine(DATABASE_URL)
    t0 = time.time()

    sum_exprs = []
    for pgs_col in PGS_TO_GAME:
        sum_exprs.append(f"SUM(pg.{pgs_col}) AS {pgs_col}")
    sum_sql = ",\n        ".join(sum_exprs)

    with engine.connect() as conn:
        print("Aggregating player_game_stats -> per-game-per-team totals...")
        rows = conn.execute(text(f"""
            SELECT
                pg.game_id, pg.team_id,
                SUM(pg.points) AS points,
                {sum_sql}
            FROM nba.player_game_stats pg
            GROUP BY pg.game_id, pg.team_id
        """)).fetchall()

        by_game = {}
        for row in rows:
            d = dict(row._mapping)
            gid = d.pop("game_id")
            tid = d.pop("team_id")
            by_game.setdefault(gid, {})[tid] = d

        games = conn.execute(text("""
            SELECT id, home_team_id, away_team_id, home_score, away_score
            FROM nba.games
        """)).fetchall()
        game_map = {g.id: g for g in games}

        cols_sql_h = ", ".join(f"home_{c} = :h_{c}" for c in STORE_COLS)
        cols_sql_a = ", ".join(f"away_{c} = :a_{c}" for c in STORE_COLS)
        # (per-side dynamic UPDATE built in the loop below)

        updated = 0
        skipped_incomplete = 0
        no_side = 0
        # write block in its own transaction (engine.begin handles autocommit after reads)
        with engine.begin() as wconn:
            for gid, sides in by_game.items():
                g = game_map.get(gid)
                if g is None:
                    continue
                # Per-side update: update each side independently when ITS OWN
                # pgs points match that side's score (completeness). Both sides
                # done in one UPDATE when possible; otherwise whichever is complete.
                h_tid, a_tid = g.home_team_id, g.away_team_id
                h_agg = sides.get(h_tid)
                a_agg = sides.get(a_tid)

                set_h = []
                set_a = []
                params = {"gid": gid}

                if h_agg is not None:
                    h_ok = (g.home_score is not None and h_agg.get("points") == g.home_score)
                    if h_ok:
                        for c in STORE_COLS:
                            set_h.append(f"home_{c} = :h_{c}")
                            params[f"h_{c}"] = h_agg.get(GAME_TO_PGS[c])
                    else:
                        skipped_incomplete += 1
                else:
                    no_side += 1

                if a_agg is not None:
                    a_ok = (g.away_score is not None and a_agg.get("points") == g.away_score)
                    if a_ok:
                        for c in STORE_COLS:
                            set_a.append(f"away_{c} = :a_{c}")
                            params[f"a_{c}"] = a_agg.get(GAME_TO_PGS[c])
                    else:
                        skipped_incomplete += 1
                else:
                    no_side += 1

                if not set_h and not set_a:
                    continue

                sets = set_h + set_a
                if not sets:
                    continue
                upd = f"UPDATE nba.games SET {', '.join(sets)} WHERE id = :gid"
                wconn.execute(text(upd), params)
                updated += 1

        print(f"Backfilled {updated} games, skipped {skipped_incomplete} (pgs incomplete side), "
              f"{no_side} (missing a side in pgs) [{time.time()-t0:.0f}s]")


if __name__ == "__main__":
    main()
