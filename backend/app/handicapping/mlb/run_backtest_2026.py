import asyncio, logging, sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.handicapping.mlb.mlb_engine import backtest_season

DATABASE_URL = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"

async def main():
    year = 2026

    engine = create_async_engine(DATABASE_URL, pool_size=5, max_overflow=10)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    logger = logging.getLogger("earl.mlb_backtest_runner")
    logger.info("=" * 60)
    logger.info(f"Starting MLB {year} backtest (fresh)")
    logger.info(f"Started at: {datetime.now().isoformat()}")
    logger.info("=" * 60)

    async with SessionLocal() as db:
        results = await backtest_season(db, year=year, resume=False)

    logger.info("=" * 60)
    logger.info(f"Backtest complete at: {datetime.now().isoformat()}")
    logger.info(f"Results: {results}")
    logger.info("=" * 60)

    await engine.dispose()

asyncio.run(main())
