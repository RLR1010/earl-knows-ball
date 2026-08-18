"""
Post-wiring differential check: the NEW data_loader (reading mlb.team_ops_vs_arm)
must produce the SAME platoon OPS columns (h_ops_vs_lhp/h_ops_vs_rhp/a_ops_vs_lhp/
a_ops_vs_rhp, and the derived h/a_ops_vs_opp_hand) as the OLD per-row LATERAL SQL.

We run the new loader, then for a sample of rows recompute each platoon OPS the
OLD way (through-date from batting_game_stats) and compare.

Usage:
    ../venv/bin/python app/scripts/validate_mlb_loader_plato.py [--limit N]
"""
import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from app.db_urls import PSYCOPG2_DATABASE_URL  # noqa: E402
from app.handicapping.mlb.data_loader import get_data_loader  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=80)
    args = ap.parse_args()

    # 1) new loader
    t0 = time.time()
    df = get_data_loader().load_games(status="FINAL")
    print(f"new load_games: {time.time()-t0:.1f}s  rows={len(df)}", flush=True)

    eng = create_engine(PSYCOPG2_DATABASE_URL, connect_args={"options": "-c jit=off"})
    cols_plato = ["h_ops_vs_lhp", "h_ops_vs_rhp", "a_ops_vs_lhp", "a_ops_vs_rhp"]
    need = [c for c in cols_plato if c in df.columns]
    if not need:
        print("❌ loader output missing platoon cols:", df.columns[:40].tolist())
        return 1

    # target rows to compare: seasons 15-21 in the *calendar-year* sense = 2020-2026.
    # (loader season_year is the calendar year, e.g. 2026 for season 21; arm data
    # exists only for those trailing seasons)
    sample = df[df["season_year"].between(2020, 2026)].sample(args.limit, random_state=1)
    mismatches = []
    checked = 0
    with eng.connect() as c:
        for _, row in sample.iterrows():
            gid = row["game_id"]
            season = int(row["season_year"])
            ht = int(row["home_team_id"])
            at = int(row["away_team_id"])
            # need the real season_id (loader season_year=calendar year, e.g.
            # 2026 -> season 21) + target game date for the old-LATERAL 30-min bound
            gmeta = c.execute(text(
                "SELECT season_id, date FROM mlb.games WHERE id=:g").bindparams(g=gid)).fetchone()
            season = int(gmeta[0])
            gdate = gmeta[1]
            for side, team_col, opp_col, out in (
                ("home", "home_team_id", "away_pitcher_name", "h_ops_vs_lhp"),
                ("home", "home_team_id", "away_pitcher_name", "h_ops_vs_rhp"),
                ("away", "away_team_id", "home_pitcher_name", "a_ops_vs_lhp"),
                ("away", "away_team_id", "home_pitcher_name", "a_ops_vs_rhp"),
            ):
                arm = 'L' if out.endswith('lhp') else 'R'
                tid = ht if side == "home" else at
                # old-way value
                oldv = c.execute(text(f"""
                    SELECT ROUND((SUM(bg.hits + bg.base_on_balls + bg.hit_by_pitch + bg.total_bases)::numeric)
                        / NULLIF(SUM(bg.at_bats + bg.base_on_balls + bg.hit_by_pitch + bg.sacrifice_flies),0),4)
                    FROM mlb.batting_game_stats bg
                    JOIN mlb.games g2 ON g2.id = bg.game_id
                    LEFT JOIN mlb.players pl2 ON pl2.name = g2.{opp_col}
                    WHERE bg.team_side = :side AND g2.{team_col} = :tid
                      AND g2.status='FINAL' AND g2.season_id = :season
                      AND g2.date < :ts - INTERVAL '30 minutes'
                      AND pl2.throws = :arm
                """).bindparams(side=side, tid=tid, season=season, ts=gdate, arm=arm)).scalar()
                newv = row[out] if out in row else None
                # pandas stores SQL NULL as NaN; old scalar query returns None.
                # Treat both as equivalent 'no value' representations.
                import math
                def _is_blank(v):
                    return v is None or (isinstance(v, float) and math.isnan(v))
                newv_f = None if _is_blank(newv) else float(newv)
                oldv_f = None if oldv is None else float(oldv)
                checked += 1
                if newv_f is None and oldv_f is None:
                    continue  # both blank -> match
                if (newv_f is None) != (oldv_f is None):
                    mismatches.append((gid, out, newv_f, oldv_f, "blank-mismatch"))
                elif newv_f is not None and abs(newv_f - oldv_f) > 0.00005:
                    mismatches.append((gid, out, newv_f, oldv_f, f"diff={newv_f-oldv_f:.5f}"))

    print(f"\nchecked {checked} platoon cells  mismatches={len(mismatches)}")
    if mismatches:
        print("❌ MISMATCHES:")
        for m in mismatches[:30]:
            print("  ", m)
        return 1
    print("✅ NEW loader platoon OPS == OLD LATERAL OPS (no mismatches).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
