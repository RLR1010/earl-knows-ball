"""Surgical repair of nfl.player_weekly_stats (2016-2020) + rebuild of
nfl.qb_cumulative_stats.

Fixes: the games table previously lacked relocated-team + playoff games, so the
nflverse player ingester stored game_id=NULL for those rows. This:
  1. Deletes stale-duplicate NULL rows (claimed by another row OR duplicate
     NULL rows for the same player+game).
  2. Backfills game_id for surviving NULL rows by matching
     (team_id, opponent_id, week) against the now-fixed games table.
     Playoff rows match at shifted week (stored week+1) since games use
     canonical 19-22 while nflverse stored 18-21.
  3. Shifts stored week +1 for playoff rows (18/19/20/21 -> 19/20/21/22).
  4. Rebuilds nfl.qb_cumulative_stats from the cleaned player_weekly_stats.

Safe: backups exist (player_weekly_stats_backup_20260804,
qb_cumulative_stats_backup_20260804). Transactional; --apply commits.

Usage:
  python scripts/repair_player_weekly_stats.py          # dry-run (reports counts)
  python scripts/repair_player_weekly_stats.py --apply  # write + rebuild
"""
import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
load_dotenv(os.path.join(_BACKEND, ".env"))
sys.path.insert(0, _BACKEND)

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

SEASONS = (2016, 2017, 2018, 2019, 2020)
SNAMES = ",".join(str(y) for y in SEASONS)


async def _count(db, sql):
    return (await db.execute(text(sql))).scalar()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    engine = create_async_engine(DATABASE_URL)
    async with AsyncSession(engine) as db:

        # ---- Step 1: delete stale-duplicate NULL rows ----
        # 1a: NULL row whose matched game is already claimed by any other row (same player/game)
        # 1b: duplicate NULL rows for the same (player, matched game) -> keep lowest id
        if args.apply:
            await db.execute(text(f"""
                DELETE FROM nfl.player_weekly_stats pw
                USING nfl.seasons s, nfl.games g
                WHERE s.id=pw.season_id AND s.year IN ({SNAMES})
                  AND pw.game_id IS NULL AND pw.week<=17
                  AND g.game_type='REG' AND g.week=pw.week
                  AND ((g.home_team_id=pw.team_id AND g.away_team_id=pw.opponent_id)
                       OR (g.away_team_id=pw.team_id AND g.home_team_id=pw.opponent_id))
                  AND (EXISTS (SELECT 1 FROM nfl.player_weekly_stats c
                               WHERE c.player_id=pw.player_id AND c.game_id=g.id)
                       OR EXISTS (SELECT 1 FROM nfl.player_weekly_stats d
                                  WHERE d.player_id=pw.player_id AND d.game_id IS NULL
                                    AND d.id < pw.id
                                    AND EXISTS (SELECT 1 FROM nfl.games dg
                                                WHERE dg.game_type='REG' AND dg.week=pw.week
                                                  AND ((dg.home_team_id=d.team_id AND dg.away_team_id=d.opponent_id)
                                                       OR (dg.away_team_id=d.team_id AND dg.home_team_id=d.opponent_id)))))
            """))
            await db.execute(text(f"""
                DELETE FROM nfl.player_weekly_stats pw
                USING nfl.seasons s, nfl.games g
                WHERE s.id=pw.season_id AND s.year IN ({SNAMES})
                  AND pw.game_id IS NULL AND pw.week BETWEEN 18 AND 21
                  AND g.game_type='REG' AND g.week=pw.week+1
                  AND ((g.home_team_id=pw.team_id AND g.away_team_id=pw.opponent_id)
                       OR (g.away_team_id=pw.team_id AND g.home_team_id=pw.opponent_id))
                  AND (EXISTS (SELECT 1 FROM nfl.player_weekly_stats c
                               WHERE c.player_id=pw.player_id AND c.game_id=g.id)
                       OR EXISTS (SELECT 1 FROM nfl.player_weekly_stats d
                                  WHERE d.player_id=pw.player_id AND d.game_id IS NULL
                                    AND d.id < pw.id
                                    AND EXISTS (SELECT 1 FROM nfl.games dg
                                                WHERE dg.game_type='REG' AND dg.week=pw.week+1
                                                  AND ((dg.home_team_id=d.team_id AND dg.away_team_id=d.opponent_id)
                                                       OR (dg.away_team_id=d.team_id AND dg.home_team_id=d.opponent_id)))))
            """))
            await db.commit()
            print("Step1: dupes deleted")
        nulls_before = await _count(db, f"""
            SELECT count(*) FROM nfl.player_weekly_stats pw
            JOIN nfl.seasons s ON s.id=pw.season_id WHERE s.year IN ({SNAMES}) AND pw.game_id IS NULL
        """)
        print(f"Step1: NULL rows after dup-removal = {nulls_before}")

        # ---- Step 2: backfill game_id for remaining NULL rows ----
        links_reg = links_po = 0
        if args.apply:
            links_reg = (await db.execute(text(f"""
                UPDATE nfl.player_weekly_stats pw
                SET game_id = g.id
                FROM nfl.seasons s, nfl.games g
                WHERE s.id=pw.season_id AND s.year IN ({SNAMES})
                  AND pw.game_id IS NULL AND pw.week<=17
                  AND g.game_type='REG' AND g.week=pw.week
                  AND ((g.home_team_id=pw.team_id AND g.away_team_id=pw.opponent_id)
                       OR (g.away_team_id=pw.team_id AND g.home_team_id=pw.opponent_id))
            """))).rowcount or 0
            links_po = (await db.execute(text(f"""
                UPDATE nfl.player_weekly_stats pw
                SET game_id = g.id
                FROM nfl.seasons s, nfl.games g
                WHERE s.id=pw.season_id AND s.year IN ({SNAMES})
                  AND pw.game_id IS NULL AND pw.week BETWEEN 18 AND 21
                  AND g.game_type='REG' AND g.week=pw.week+1
                  AND ((g.home_team_id=pw.team_id AND g.away_team_id=pw.opponent_id)
                       OR (g.away_team_id=pw.team_id AND g.home_team_id=pw.opponent_id))
            """))).rowcount or 0
            await db.commit()
        print(f"Step2: game_id backfilled (reg={links_reg}, playoff={links_po})")

        # ---- Step 3: shift playoff week +1 ----
        shifted = 0
        if args.apply:
            for src, dst in ((21, 22), (20, 21), (19, 20), (18, 19)):
                r = await db.execute(text(f"""
                    UPDATE nfl.player_weekly_stats SET week=:dst
                    FROM nfl.seasons s
                    WHERE s.id=player_weekly_stats.season_id AND s.year IN ({SNAMES}) AND week=:src
                """), {"dst": dst, "src": src})
                shifted += r.rowcount or 0
            await db.commit()
        print(f"Step3: playoff week shifted = {shifted}")

        remain = await _count(db, f"""
            SELECT count(*) FROM nfl.player_weekly_stats pw
            JOIN nfl.seasons s ON s.id=pw.season_id
            WHERE s.year IN ({SNAMES}) AND pw.game_id IS NULL
        """)
        print(f"Remaining NULL game_ids after repair = {remain}")

        # ---- Step 4: rebuild qb_cumulative_stats ----
        if args.apply:
            await db.execute(text(f"DELETE FROM nfl.qb_cumulative_stats WHERE season IN ({SNAMES})"))
            await db.commit()
            print("Step4: cleared 2016-2020 qb_cumulative rows; rebuilding (sync)...")
            import concurrent.futures
            from app.handicapping.nfl.populate_qb_rolling_stats import populate_qb_tables

            def _build():
                return populate_qb_tables(seasons=[2016, 2017, 2018, 2019, 2020])
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                res = await asyncio.get_event_loop().run_in_executor(pool, _build)
            print("Step4 rebuild result:", res)
        else:
            print("(dry-run — no writes made. Re-run with --apply.)")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
