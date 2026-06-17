"""
Runner for MLB 2024 backtest — resumes where we left off.

Usage:
    docker exec earl-knows-football-api-1 python -m app.handicapping.mlb.run_backtest

Resumes with resume=True so already-processed games are skipped.
"""
import asyncio
import logging
import sys
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("earl.mlb_backtest_runner")

# Silence noisy libs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("sqlalchemy").setLevel(logging.WARNING)

DATABASE_URL = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"


async def main():
    year = 2024

    engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    from app.handicapping.mlb.mlb_engine import backtest_season

    logger.info("=" * 60)
    logger.info(f"Starting MLB {year} backtest (resume mode)")
    logger.info(f"Started at: {datetime.now().isoformat()}")
    logger.info("=" * 60)

    async with SessionLocal() as db:
        results = await backtest_season(db, year=year, resume=True)

    logger.info("=" * 60)
    logger.info(f"Backtest complete at: {datetime.now().isoformat()}")
    logger.info(f"Results: {results}")
    logger.info("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
