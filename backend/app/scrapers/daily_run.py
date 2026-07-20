"""
Daily sportsbook scraper orchestrator.

Runs once per day: scrapes FanDuel for team props, awards, and player props
across all sports. Saves results to the database.

Usage:
    python -m backend.app.scrapers.daily_run
"""

import asyncio
import logging
import sys
import time

from sqlalchemy import create_engine

from app.core.config import settings
from app.scrapers.browser import BrowserManager
from app.scrapers.db import (
    save_team_props,
    save_player_season_props,
    save_player_daily_props,
)
from app.scrapers.sports import get_active_configs
from app.scrapers.books import fanduel

logger = logging.getLogger("earl.scrapers.daily_run")

# Sync DB engine for standalone scraper
sync_db_url = settings.database_url.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_db_url, pool_pre_ping=True)


async def run_sport(
    browser: BrowserManager,
    config,
    stats: dict,
) -> None:
    """Scrape one sport from FanDuel and save results."""
    sport_name = config.name.upper()

    try:
        # --- Team Props (futures) ---
        if config.scrape_team_props:
            t0 = time.time()
            context = await browser.new_context()
            try:
                team_props = await fanduel.scrape_team_props(context, config.name)
            finally:
                await context.close()
            if team_props:
                count = save_team_props(engine, team_props)
                stats["team_props"] += count
                logger.info(
                    f"[FD {sport_name}] saved {count} team props "
                    f"in {time.time()-t0:.1f}s"
                )
            else:
                logger.info(f"[FD {sport_name}] no team props found")

    except Exception as e:
        logger.error(f"[FD {sport_name}] team props failed: {e}")

    # Brief cooldown between tabs
    await asyncio.sleep(3)

    try:
        # --- Awards ---
        if config.scrape_awards:
            t0 = time.time()
            context = await browser.new_context()
            try:
                award_props = await fanduel.scrape_awards(context, config.name)
            finally:
                await context.close()
            if award_props:
                count = save_player_season_props(engine, award_props)
                stats["season_props"] += count
                logger.info(
                    f"[FD {sport_name}] saved {count} award props "
                    f"in {time.time()-t0:.1f}s"
                )
            else:
                logger.info(f"[FD {sport_name}] no award props found")

    except Exception as e:
        logger.error(f"[FD {sport_name}] awards failed: {e}")

    await asyncio.sleep(3)

    try:
        # --- Player Daily Props ---
        if config.scrape_player_props:
            t0 = time.time()
            context = await browser.new_context()
            try:
                player_props = await fanduel.scrape_player_props(
                    context, config.name
                )
            finally:
                await context.close()
            if player_props:
                count = save_player_daily_props(engine, player_props)
                stats["daily_props"] += count
                logger.info(
                    f"[FD {sport_name}] saved {count} player daily props "
                    f"in {time.time()-t0:.1f}s"
                )
            else:
                logger.info(f"[FD {sport_name}] no player daily props found")

    except Exception as e:
        logger.error(f"[FD {sport_name}] player props failed: {e}")


async def run_daily_scrape() -> dict:
    """Main orchestrator."""
    stats = {"team_props": 0, "season_props": 0, "daily_props": 0}
    configs = get_active_configs()

    logger.info(
        f"Starting daily scrape: {len(configs)} sport(s), 1 book (fanduel)"
    )

    browser = BrowserManager()
    start_time = time.time()

    try:
        await browser.start()

        for config in configs:
            logger.info(f"=== Scraping FanDuel {config.name} ===")
            await run_sport(browser, config, stats)

    except Exception as e:
        logger.error(f"Fatal scrape error: {e}")
        raise

    finally:
        await browser.stop()

    elapsed = time.time() - start_time
    logger.info(
        f"Daily scrape complete in {elapsed:.1f}s. "
        f"Stats: {stats}"
    )
    return stats


def main():
    """Sync entry point for cron."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    logger.info("=" * 60)
    logger.info("Daily Sportsbook Scraper — Starting")
    logger.info("=" * 60)

    asyncio.run(run_daily_scrape())


if __name__ == "__main__":
    main()
