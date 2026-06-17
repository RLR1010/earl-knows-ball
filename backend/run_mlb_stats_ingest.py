"""
MLB Stats Ingestion — all data from 2006-present.
Loads teams, players, games, batting stats, and pitching stats.
Uses the free public MLB Stats API (no key required).
"""
import asyncio
import logging
import sys
sys.path.insert(0, "/app")

from app.ingestion.mlb_stats import load_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.mlb_ingest")


async def run():
    logger.info("=" * 60)
    logger.info("MLB Data Ingestion (full)")
    logger.info("=" * 60)

    results = await load_all()

    logger.info("\n" + "=" * 60)
    logger.info("MLB INGESTION COMPLETE")
    logger.info("=" * 60)
    for key, value in results.items():
        logger.info(f"  {key}: {value}")

    logger.info("\nDone.")


if __name__ == "__main__":
    asyncio.run(run())
