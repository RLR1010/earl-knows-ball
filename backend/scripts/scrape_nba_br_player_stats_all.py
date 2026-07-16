"""
Scrape Basketball Reference for all missing NBA player game stats
across historical seasons (2020-2025).

This complements the ESPN ingestion by using BR as a fallback for games
where ESPN doesn't provide per-athlete data.

Usage:
    python -m scripts.scrape_nba_br_player_stats_all
"""

import asyncio
import sys
import logging

sys.path.insert(0, ".")

from app.ingestion.nba_br_player_stats import scrape_missing_games

logger = logging.getLogger("br-player-stats-bulk")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
logger.addHandler(handler)

logger.info("Starting multi-season BR scrape for NBA player game stats...")

async def main():
    years = [2020, 2021, 2022, 2023, 2024, 2025]
    for year in years:
        logger.info("=" * 70)
        logger.info(f"Scraping season {year}...")
        logger.info("=" * 70)
        await scrape_missing_games(year)
        logger.info(f"Finished season {year}")
    logger.info("Done! All seasons complete.")

if __name__ == "__main__":
    asyncio.run(main())
