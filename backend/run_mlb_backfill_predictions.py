#!/usr/bin/env python3
"""
MLB Prediction Backfill — generates pick cards for completed 2026 games.

Usage in container:
    python3 run_mlb_backfill_predictions.py                              # all
    python3 run_mlb_backfill_predictions.py --from-date 2026-04-12       # specific range
    python3 run_mlb_backfill_predictions.py --from-date 2026-05-01 --to-date 2026-05-15
    python3 run_mlb_backfill_predictions.py --dry-run --from-date 2026-06-01
"""
import asyncio
import logging
import sys
import os
from datetime import datetime, timezone, date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("APP_ENV", "production")

from app.core.config import settings
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text
from app.handicapping.mlb.mlb_engine import MLBHandicapper

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("earl.mlb_backfill")

DRY_RUN = "--dry-run" in sys.argv

# Parse date args
FROM_DATE = None
TO_DATE = None
for i, arg in enumerate(sys.argv):
    if arg == "--from-date" and i + 1 < len(sys.argv):
        FROM_DATE = date.fromisoformat(sys.argv[i + 1])
    elif arg == "--to-date" and i + 1 < len(sys.argv):
        TO_DATE = date.fromisoformat(sys.argv[i + 1])


async def get_dates_needing_predictions(engine, from_dt: date = None, to_dt: date = None) -> list[date]:
    """Get distinct dates that have completed games missing predictions."""
    conditions = "s.year = 2026 AND g.status = 'FINAL' AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL"
    if from_dt:
        conditions += f" AND g.date::date >= '{from_dt.isoformat()}'"
    if to_dt:
        conditions += f" AND g.date::date <= '{to_dt.isoformat()}'"

    async with engine.connect() as conn:
        r = await conn.execute(
            text(f"""
                SELECT DISTINCT g.date::date as game_date
                FROM mlb.games g
                JOIN mlb.seasons s ON s.id = g.season_id
                WHERE {conditions}
                  AND NOT EXISTS (
                      SELECT 1 FROM mlb.game_predictions gp
                      WHERE gp.game_id = g.id AND gp.source = 'api'
                  )
                ORDER BY g.date::date
            """)
        )
        return [row[0] for row in r.fetchall()]


async def count_missing_for_date(engine, gd: date) -> int:
    """Count games on a date that need predictions."""
    async with engine.connect() as conn:
        r = await conn.execute(
            text("""
                SELECT COUNT(*)
                FROM mlb.games g
                JOIN mlb.seasons s ON s.id = g.season_id
                WHERE s.year = 2026 AND g.date::date = :gd AND g.status = 'FINAL'
                  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM mlb.game_predictions gp
                      WHERE gp.game_id = g.id AND gp.source = 'api'
                  )
            """),
            {"gd": gd},
        )
        return r.scalar() or 0


async def main():
    db_url = settings.database_url
    engine = create_async_engine(db_url)

    game_dates = await get_dates_needing_predictions(engine, FROM_DATE, TO_DATE)
    if FROM_DATE:
        logger.info(f"Backfilling from {FROM_DATE.isoformat()}")
    if TO_DATE:
        logger.info(f"Backfilling to {TO_DATE.isoformat()}")
    logger.info(f"Dates with gaps: {len(game_dates)}")

    if not game_dates:
        logger.info("Nothing to do — all completed games have predictions!")
        await engine.dispose()
        return

    total_needed = 0
    for gd in game_dates:
        total_needed += await count_missing_for_date(engine, gd)
    logger.info(f"Total game predictions needed: {total_needed}")

    if DRY_RUN:
        logger.info(f"\nDry run: would save {total_needed} predictions across {len(game_dates)} dates")
        await engine.dispose()
        return

    total_saved = 0
    for gd in game_dates:
        date_str = gd.isoformat()
        needed = await count_missing_for_date(engine, gd)
        if needed == 0:
            continue

        logger.info(f"  {date_str}: {needed} games")
        try:
            async with AsyncSession(engine) as session:
                handicapper = MLBHandicapper(session)
                cards = await handicapper.handicap_date(date_str, num_games=10)
                await session.commit()
                saved = len([c for c in cards if c.game_id])
                total_saved += saved
                logger.info(f"    Saved {saved} predictions")
        except Exception as e:
            await session.close()
            logger.error(f"    Error on {date_str}: {e}")

    logger.info(f"\nDone! Saved {total_saved} of {total_needed} predictions")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
