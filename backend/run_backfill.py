"""
Standalone SB Nation backfill runner.
Scrapes ALL 32 blogs from 2024 to present with NO article limit.
Run via:  ./backfill.sh
"""
import asyncio
import logging
import sys
sys.path.insert(0, "/app")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.sbnation_archives import scrape_all_blogs, SBNATION_BLOGS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.backfill")


async def run():
    logger.info("=== SB Nation Full Backfill ===")
    logger.info(f"Blogs: {len(SBNATION_BLOGS)}")
    logger.info("Parameters: start_year=2024, max_per_blog=no limit, delay=0.5s, embed=False")
    
    engine = create_async_engine(settings.database_url)
    SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with SessionLocal() as db:
        result = await scrape_all_blogs(
            db=db,
            start_year=2024,
            max_per_blog=None,
            delay=0.5,
            embed=False,
        )

    logger.info("=== FINAL RESULTS ===")
    total = 0
    for blog, stats in result.get("blogs", {}).items():
        if isinstance(stats, dict):
            n = stats.get("articles_scraped", 0)
            if n > 0:
                total += n
                logger.info(f"  {blog}: {n} new articles")
    logger.info(f"Total new articles: {total}")
    
    # Also count grand total
    async with SessionLocal() as db2:
        from sqlalchemy import select, func
        from app.models import Article
        r = await db2.execute(select(func.count()).select_from(Article).where(Article.source_type == "sbnation"))
        grand = r.scalar()
        logger.info(f"Grand total SB Nation articles in DB: {grand}")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
