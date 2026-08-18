"""
Differential accuracy check: mlb.team_ops_vs_arm vs the current platoon LATERALs
in mlb.data_loader.

For a sample of FINAL target games, we:
  1) compute the CURRENT loader plato_lhp/plato_rhp OPS (home + away sides) via
     the exact same SQL the data_loader uses (per-target-game subquery), and
  2) read the PREVIOUS Final row from team_ops_vs_arm for that (team, side, arm),
     i.e. the value the loader WOULD eventually read for that target.

Assert they match to 4dp (the loader ROUND(...,4)).

Usage:
    ../venv/bin/python app/scripts/validate_mlb_team_ops_vs_arm.py [--season N] [--limit M]
"""
import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    eng = create_engine(PSYCOPG2_DATABASE_URL, connect_args={"options": "-c jit=off"})

    season_where = "g.season_id = :season" if args.season else "TRUE"
    params = {"limit": args.limit}
    if args.season:
        params["season"] = args.season

    with eng.connect() as c:
        targets = c.execute(text(f"""
            SELECT g.id AS game_id, g.home_team_id, g.away_team_id,
                   g.season_id, g.date,
                   g.home_pitcher_name, g.away_pitcher_name
            FROM mlb.games g
            WHERE g.status='FINAL' AND {season_where}
            ORDER BY g.date
            LIMIT :limit
        """).bindparams(**params)).mappings().all()
        print(f"checking {len(targets)} target games", flush=True)

        mismatches = []
        for t in targets:
            for side, team_col, opp_col in (
                ("home", "g2.home_team_id", "g2.away_pitcher_name"),
                ("away", "g2.away_team_id", "g2.home_pitcher_name"),
            ):
                team_id = t["home_team_id"] if side == "home" else t["away_team_id"]
                for arm in ("L", "R"):
                    # 1) current loader lateral value (column names are trusted
                    #    constants from the tuple above — not user input)
                    lateral = c.execute(text(f"""
                        SELECT ROUND((SUM(bg.hits + bg.base_on_balls + bg.hit_by_pitch + bg.total_bases)::numeric)
                            / NULLIF(SUM(bg.at_bats + bg.base_on_balls + bg.hit_by_pitch + bg.sacrifice_flies),0),4) AS ops
                        FROM mlb.batting_game_stats bg
                        JOIN mlb.games g2 ON g2.id = bg.game_id
                        LEFT JOIN mlb.players pl2 ON pl2.name = {opp_col}
                        WHERE bg.team_side = :side AND {team_col} = :tid
                          AND g2.status='FINAL' AND g2.season_id = :season
                          AND g2.date < :ts - INTERVAL '30 minutes'
                          AND pl2.throws = :arm
                    """).bindparams(side=side, tid=team_id, season=t["season_id"],
                                    ts=t["date"], arm=arm)).scalar()

                    # 1b) loader-equivalent WINS vs arm, same window/predicates.
                    #     MAX (not SUM): per batting row would over-count; a win
                    #     must count once per (game, side, arm).
                    lateral_wins = c.execute(text(f"""
                        SELECT COALESCE(MAX(CASE
                            WHEN :side = 'home' AND g2.home_score > g2.away_score THEN 1
                            WHEN :side = 'away' AND g2.away_score > g2.home_score THEN 1
                            ELSE 0 END), 0)
                        FROM mlb.batting_game_stats bg
                        JOIN mlb.games g2 ON g2.id = bg.game_id
                        LEFT JOIN mlb.players pl2 ON pl2.name = {opp_col}
                        WHERE bg.team_side = :side AND {team_col} = :tid
                          AND g2.status='FINAL' AND g2.season_id = :season
                          AND g2.date < :ts - INTERVAL '30 minutes'
                          AND pl2.throws = :arm
                    """).bindparams(side=side, tid=team_id, season=t["season_id"],
                                    ts=t["date"], arm=arm)).scalar() or 0

                    # 2) the previous Final row in team_ops_vs_arm (through-date value the
                    #    loader would read as the PREVIOUS row before this target)
                    prev_row = c.execute(text("""
                        SELECT t.ops_vs_arm, t.wins_vs_arm
                        FROM mlb.team_ops_vs_arm t
                        JOIN mlb.games g ON g.id = t.game_id
                        WHERE t.team_id = :tid AND t.team_side = :side
                          AND t.arm = :arm AND t.season_id = :season
                          AND g.status='FINAL'
                          AND g.date < :ts - INTERVAL '30 minutes'
                        ORDER BY g.date DESC, g.id DESC
                        LIMIT 1
                    """).bindparams(tid=team_id, side=side, arm=arm, season=t["season_id"],
                                    ts=t["date"])).fetchone()
                    prev = prev_row[0] if prev_row else None
                    prev_wins = prev_row[1] if prev_row else None

                    if (lateral is None) != (prev is None):
                        mismatches.append((t["game_id"], side, arm, lateral, prev, "NULL-mismatch"))
                    elif lateral is not None:
                        # 4dp compare (both ROUND(...,4))
                        if abs(lateral - prev) > 0.00005:
                            mismatches.append((t["game_id"], side, arm, lateral, prev,
                                               f"diff={lateral-prev:.5f}"))
                        if lateral_wins != (prev_wins or 0):
                            mismatches.append((t["game_id"], side, arm,
                                               lateral_wins, prev_wins, "wins-mismatch"))

        print(f"\n{'RESULTS':=^70}")
        if not mismatches:
            print(f"✅ ALL {len(targets)} target games match the loader LATERALs "
                  f"(home+away × L/R) within 0.00005.")
        else:
            print(f"❌ {len(mismatches)} mismatches:")
            for m in mismatches[:30]:
                print("  ", m)
        print(f"{'':=^70}")
        return 0 if not mismatches else 1


if __name__ == "__main__":
    sys.exit(main())
