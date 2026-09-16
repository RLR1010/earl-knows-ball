"""Full rebuild of NFL derived rolling/cumulative tables from the corrected
nfl.player_weekly_stats. Mirrors the scheduler's builder order:
team -> defensive -> (qb cumulative+rolling) -> (skill+kicker) -> cumulative_game_stats.

Usage: PYTHONPATH=. venv/bin/python app/scripts/rebuild_nfl_derived_stats.py
"""
import asyncio
from sqlalchemy import create_engine, text
from app.db_urls import PSYCOPG2_DATABASE_URL
from app.handicapping.nfl import populate_team_rolling_stats as T
from app.handicapping.nfl import populate_qb_rolling_stats as Q
from app.handicapping.nfl import populate_skill_rolling_stats as S
from app.handicapping.nfl import populate_defensive_rolling_stats as D
from app.handicapping.nfl import cumulative_stats as C

UPSERT_TABLES = ["qb_rolling_stats", "qb_cumulative_stats"]


async def _cumulative():
    from app.database import async_session
    async with async_session() as db:
        for gt in ("REG", "POST"):
            try:
                r = await C.recompute(db, seasons=None, game_type=gt)
                print("cumulative_game_stats", gt, r, flush=True)
            except Exception as e:
                print("cumulative_game_stats", gt, "ERR", e, flush=True)


def main():
    engine = create_engine(PSYCOPG2_DATABASE_URL, pool_pre_ping=True)
    print("team_rolling_stats ...", flush=True); T.run()
    print("defensive_rolling_stats ...", flush=True); D.populate_defensive_rolling_stats(engine, None)
    with engine.begin() as cn:
        for t in UPSERT_TABLES:
            cn.execute(text(f"delete from nfl.{t}"))
    print("qb_cumulative_stats + qb_rolling_stats ...", flush=True); Q.populate_qb_tables(engine, None, "REG")
    print("skill_rolling_stats + kicker_rolling_stats ...", flush=True); S.populate_skill_rolling_tables(engine, None, "REG")
    asyncio.run(_cumulative())
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
