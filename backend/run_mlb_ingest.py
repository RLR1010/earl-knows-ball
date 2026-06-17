"""
MLB Stats Ingestion — focused on 2021-present.
Runs inside the backend-api container.
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
    logger.info("=== MLB Data Ingestion (2021-present) ===")
    
    results = await load_all()
    
    logger.info("\n=== MLB INGESTION COMPLETE ===")
    for key, value in results.items():
        logger.info(f"  {key}: {value}")

    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run())
