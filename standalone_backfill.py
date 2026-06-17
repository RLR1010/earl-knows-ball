#!/usr/bin/env python3
"""
Standalone SB Nation backfill runner.
Run directly on the host:  python3 standalone_backfill.py
NOT inside the Docker container — so Docker rebuilds won't kill it.
"""
import asyncio, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/backend")

os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"

import logging
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.ingestion.sbnation_archives import scrape_all_blogs, SBNATION_BLOGS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("standalone_backfill")

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with SessionLocal() as db:
        result = await scrape_all_blogs(
            db=db, start_year=2024, max_per_blog=None, delay=0.5, embed=False,
        )
    total = sum(s.get("articles_scraped", 0) for s in result.get("blogs", {}).values() if isinstance(s, dict))
    logger.info(f"Done! Total new: {total}")
    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(run())
