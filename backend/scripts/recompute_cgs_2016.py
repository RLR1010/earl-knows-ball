"""One-off: recompute nfl.cumulative_game_stats for 2016 after gap-1 fixes.

Fixes applied before running this:
  1. Loader now derives season_type from games.game_type (authoritative),
     so 2016 week-5 real REG games are labeled REG instead of PRE.
  2. recompute() cleans stale rows (pre-season games mislabeled as REG
     cumulative rows no longer map to a REG game in the games table).
  3. games row 400927752 (Super Bowl LI) moved week 5 -> 22, and its
     garbage cumulative rows were deleted.

Run:  venv/bin/python scripts/recompute_cgs_2016.py
"""
import asyncio
import logging
import os

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load backend/.env for DATABASE_URL
_load = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_load)
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)


async def main() -> None:
    from app.handicapping.nfl.cumulative_stats import recompute

    engine = create_async_engine(DATABASE_URL)
    async with AsyncSession(engine) as db:
        results = await recompute(db, [2016])
        logger.info("recompute results: %s", results)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
