"""
Multi-sport SB Nation backfill runner.
Scrapes all NFL, NBA, and MLB blogs from 2021 to present.
Run via:  docker run --rm --network host -v $(pwd)/backend:/app backend-api python3 /app/run_sbnation_all.py
"""
import asyncio
import logging
import sys
sys.path.insert(0, "/app")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.sbnation_archives import scrape_all_blogs, SBNATION_BLOGS, NBA_BLOGS, MLB_BLOGS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.sbnation_all")


async def run_sport(name: str, blogs: list, start_year: int):
    """Run SB Nation scraping for one sport."""
    logger.info(f"=== {name} SB Nation Backfill (from {start_year}) ===")
    logger.info(f"Blogs: {len(blogs)}")
    
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as db:
        result = await scrape_all_blogs(
            db=db,
            blogs=blogs,
            start_year=start_year,
            max_per_blog=None,
            delay=0.5,
            embed=False,
        )

        logger.info(f"=== {name} RESULTS ===")
        total = 0
        for blog, stats in result.get("blogs", {}).items():
            if isinstance(stats, dict):
                n = stats.get("articles_scraped", 0)
                if n > 0:
                    total += n
                    logger.info(f"  {blog}: {n} new articles")
        logger.info(f"Total new articles for {name}: {total}")

    await engine.dispose()


async def run():
    logger.info("=== Multi-Sport SB Nation Backfill (2021-present) ===")
    
    await asyncio.gather(
        run_sport("NFL", SBNATION_BLOGS, 2021),
        run_sport("NBA", NBA_BLOGS, 2021),
        run_sport("MLB", MLB_BLOGS, 2021),
    )

    logger.info("=== ALL SPORTS COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(run())
