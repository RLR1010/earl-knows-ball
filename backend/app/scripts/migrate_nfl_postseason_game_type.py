"""
Migrate mislabeled NFL postseason games: REG -> POST.

Background: until now the NFL ingest stored postseason games (weeks 19-22:
Wild Card / Divisional / Conference / Super Bowl) with game_type='REG', so the
"regular season" stats tables (player_weekly_stats, rolling tables built from
them) silently included playoff games — the same contamination we fixed for
MLB/NBA. Preseason (PRE) is already correct.

This script relabels the games and their player-weekly-stats rows to POST,
matching each season's true playoff game count (11 for 2016-2019, 13 for
2020-2025 — verified against real NFL playoff formats).

Rule (verified against DB): for every NFL season, week >= 19 == postseason.
All 122 such games are dated Jan/Feb, matching real playoff counts.

Run:  dry-run (default)  ->  print affected counts, change nothing
      --apply            ->  actually UPDATE both tables
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.database import admin_async_session

# weeks 19+ are always postseason across every modern NFL season (verified).
POST_WEEK_MIN = 19


async def _counts(db):
    games = (await db.execute(text("""
        SELECT COUNT(*) FROM nfl.games
        WHERE game_type='REG' AND week >= :w
    """), {"w": POST_WEEK_MIN})).scalar()

    stats_rows = (await db.execute(text("""
        SELECT COUNT(*) FROM nfl.player_weekly_stats pws
        JOIN nfl.games g ON g.id = pws.game_id
        WHERE g.game_type='REG' AND g.week >= :w
    """), {"w": POST_WEEK_MIN})).scalar()

    # cumulative_game_stats carries its own season_type (team-builder source).
    # It was NEVER relabeled, so playoff games there still say 'REG'.
    cum_rows = (await db.execute(text("""
        SELECT COUNT(*) FROM nfl.cumulative_game_stats c
        JOIN nfl.games g ON g.id = c.game_id
        WHERE g.game_type='POST' AND c.season_type='REG'
    """), {"w": POST_WEEK_MIN})).scalar()

    by_year = (await db.execute(text("""
        SELECT s.year, COUNT(*)
        FROM nfl.games g JOIN nfl.seasons s ON s.id=g.season_id
        WHERE g.game_type='REG' AND g.week >= :w
        GROUP BY s.year ORDER BY s.year
    """), {"w": POST_WEEK_MIN})).all()
    return games, stats_rows, cum_rows, [(r.year, r.count) for r in by_year]


async def apply(db):
    # games first (playoff rows lose their REG label)
    g = await db.execute(text("""
        UPDATE nfl.games SET game_type='POST'
        WHERE game_type='REG' AND week >= :w
    """), {"w": POST_WEEK_MIN})

    # then player_weekly_stats, matched by game (the game now says POST)
    s = await db.execute(text("""
        UPDATE nfl.player_weekly_stats pws
        SET game_type = 'POST'
        FROM nfl.games g
        WHERE g.id = pws.game_id
          AND g.game_type = 'POST'
          AND pws.game_type = 'REG'
          AND g.week >= :w
    """), {"w": POST_WEEK_MIN})

    # then cumulative_game_stats.season_type (team-builder source) — fix stale REG.
    cu = await db.execute(text("""
        UPDATE nfl.cumulative_game_stats c
        SET season_type = 'POST'
        FROM nfl.games g
        WHERE g.id = c.game_id
          AND g.game_type = 'POST'
          AND c.season_type = 'REG'
          AND g.week >= :w
    """), {"w": POST_WEEK_MIN})
    await db.commit()
    return g.rowcount, s.rowcount, cu.rowcount


async def main():
    apply_flag = "--apply" in sys.argv
    async with admin_async_session() as db:
        games, stats_rows, cum_rows, by_year = await _counts(db)
        print("Affected (current stale state):")
        print(f"  nfl.games week>=19 currently REG:      {games}")
        print(f"  player_weekly_stats (playoff, REG):      {stats_rows}")
        print(f"  cumulative_game_stats season_type=REG but games POST: {cum_rows}")
        print("  by season:", by_year)
        if not apply_flag:
            print("\nDry-run. Re-run with --apply to perform the migration.")
            return
        print("\nApplying migration...")
        gu, su, cu = await apply(db)
        print(f"  updated nfl.games:                     {gu}")
        print(f"  updated player_weekly_stats:           {su}")
        print(f"  updated cumulative_game_stats type:    {cu}")
        # verify
        g2, s2, c2, _ = await _counts(db)
        print("\nVerify after apply:")
        print(f"  remaining REG week>=19 games:          {g2} (expect 0)")
        print(f"  remaining REG week>=19 stats:          {s2} (expect 0)")
        print(f"  remaining stale cumulative season_type:{c2} (expect 0)")


if __name__ == "__main__":
    asyncio.run(main())
