"""
Ingest NBA player_game_stats from ESPN for multiple historical seasons.

Runs nba_player_game_stats.ingest_season() for each year from 2020 to 2025.

Usage:
    python -m scripts.ingest_nba_player_game_stats_all
"""

import asyncio
import sys
import logging

# Add backend to path
sys.path.insert(0, ".")

from app.ingestion.nba_player_game_stats import ingest_season, logger

logger.info("Starting multi-season NBA player game stats ingestion...")

async def main():
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    total = 0
    for year in years:
        logger.info("=" * 70)
        logger.info(f"Ingesting season {year} ({year}-{year-1999})...")
        logger.info("=" * 70)
        count = await ingest_season(year, "REG")
        total += count
        logger.info(f"Finished season {year}: {count} rows (running total: {total})")

    logger.info("=" * 70)
    logger.info(f"Done! Total rows ingested across all seasons: {total}")
    logger.info("=" * 70)

if __name__ == "__main__":
    asyncio.run(main())
