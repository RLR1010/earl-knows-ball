"""Rebuild nba.cumulative_game_stats for seasons 28-34 with the corrected
possession/ORTG/DRTG formula (real offensive rebounds + ESPN estimated
possessions).

Safe approach: DELETE only the target seasons' rows, then run the incremental
populator scoped to those seasons. The incremental path treats the deleted rows
as "new", marks the affected team-seasons, and recomputes their FULL season
history — without touching seasons 1-27 (whose rows remain intact).

This avoids the destructive force_rebuild=True full-table drop.

NOTE (2026-08-30): now covers seasons 26-35 (2016-17 .. 2025-26) and ALSO runs
the adjusted-ratings step (cum_adj_ortg/cum_adj_drtg/cum_sos via
adjusted_ratings.py) after the cumulative rebuild. The prior version listed only
seasons 28-34, skipped season 27, and never ran the adjusted step — which left
cum_adj_*/cum_sos NaN for whole seasons. Now every derived column is populated.
"""
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL

from app.handicapping.nba.cumulative_stats import populate_cumulative_stats
from app.handicapping.nba.adjusted_ratings import write_adjusted_to_tables

# All relevant seasons, newest-to-oldest (Rich scope: 2016-17 .. 2025-26 = 26-35)
SEASONS = [35, 34, 33, 32, 31, 30, 29, 28, 27, 26]


def main():
    url = PSYCOPG2_DATABASE_URL.replace("+asyncpg", "+psycopg2")
    engine = create_engine(url)
    season_list = ", ".join(str(s) for s in SEASONS)
    with engine.begin() as conn:
        r = conn.execute(
            text(f"DELETE FROM nba.cumulative_game_stats WHERE season_id IN ({season_list})")
        )
        print(f"Deleted {r.rowcount} cumulative rows for seasons {SEASONS}")
    engine.dispose()

    summary = populate_cumulative_stats(url, seasons=SEASONS, force_rebuild=False)
    print("Rebuild SUMMARY:", summary)

    # Also rebuild the adjusted-ratings columns (cum_adj_ortg/cum_adj_drtg/cum_sos
    # + team_rolling_stats.adj_off_10/adj_def_10) so nothing is left NaN.
    adj = write_adjusted_to_tables(engine, season_filter=None)
    print("Adjusted-ratings write:", adj)

    # Verify HOU (and a couple teams) across a rebuilt season
    eng2 = create_engine(url)
    with eng2.connect() as c:
        rows = c.execute(text("""
            SELECT DISTINCT ON (t.abbreviation, c.season_id) t.abbreviation, c.season_id,
                   ROUND(cum_ortg::numeric,1) ORTG, ROUND(cum_drtg::numeric,1) DRTG, games_played
            FROM nba.cumulative_game_stats c
            JOIN nba.teams t ON t.id=c.team_id
            WHERE c.season_id IN (28,29,30,31,32,33,34) AND t.abbreviation IN ('HOU','BOS','OKC')
            ORDER BY t.abbreviation, c.season_id DESC, c.game_date DESC
        """)).fetchall()
        print("Sample verified rows (latest cumulative per team/season):")
        for r in rows:
            print(f"   {r[0]:4} s{r[1]}  ORTG {r[2]:>6}  DRTG {r[3]:>6}  ({r[4]} gm)")
    eng2.dispose()


if __name__ == "__main__":
    main()
