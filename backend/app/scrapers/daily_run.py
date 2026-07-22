"""
Daily sportsbook scraper orchestrator.

Runs once per day: scrapes FanDuel for team props, awards, and player props
across all sports. Saves results to the database.

Uses a persistent headed Firefox session — the browser stays open between
scrapes so cookies and session trust accumulate naturally.

Usage:
    python -m backend.app.scrapers.daily_run
"""

import asyncio
import logging
import sys
import time

from playwright.async_api import Page
from sqlalchemy import create_engine

from app.core.config import settings
from app.scrapers.browser import get_browser, stop_browser
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
    page: Page,
    config,
    stats: dict,
) -> None:
    """Scrape one sport from FanDuel using a single shared page.

    Navigates the same page through all three FD tabs (futures -> awards ->
    games) via page.goto(). The page is created by the caller so that ALL
    sports reuse the SAME Firefox tab across the entire scraper run.
    """
    sport_name = config.name.upper()
    sport_had_data = False

    try:
        # --- Team Props (futures) ---
        if config.scrape_team_props:
            t0 = time.time()
            try:
                team_props = await fanduel.scrape_team_props(page, config.name)
                if team_props:
                    count = save_team_props(engine, team_props)
                    stats["team_props"] += count
                    sport_had_data = True
                    logger.info(
                        f"[FD {sport_name}] saved {count} team props "
                        f"in {time.time()-t0:.1f}s"
                    )
                else:
                    logger.info(f"[FD {sport_name}] no team props found")
            except Exception as e:
                logger.error(f"[FD {sport_name}] team props failed: {e}")

        # --- Awards ---
        if config.scrape_awards:
            t0 = time.time()
            try:
                award_props = await fanduel.scrape_awards(page, config.name)
                if award_props:
                    count = save_player_season_props(engine, award_props)
                    stats["season_props"] += count
                    sport_had_data = True
                    logger.info(
                        f"[FD {sport_name}] saved {count} award props "
                        f"in {time.time()-t0:.1f}s"
                    )
                else:
                    logger.info(f"[FD {sport_name}] no award props found")
            except Exception as e:
                logger.error(f"[FD {sport_name}] awards failed: {e}")

        # --- Player Daily Props ---
        if config.scrape_player_props:
            t0 = time.time()
            try:
                player_props = await fanduel.scrape_player_props(page, config.name)
                if player_props:
                    count = save_player_daily_props(engine, player_props)
                    stats["daily_props"] += count
                    sport_had_data = True
                    logger.info(
                        f"[FD {sport_name}] saved {count} player daily props "
                        f"in {time.time()-t0:.1f}s"
                    )
                else:
                    logger.info(f"[FD {sport_name}] no player daily props found")
            except Exception as e:
                logger.error(f"[FD {sport_name}] player props failed: {e}")

    except Exception as e:
        logger.error(f"[FD {sport_name}] fatal: {e}")


async def run_daily_scrape() -> dict:
    """Main orchestrator. Gets the persistent browser and scrapes all sports.

    Creates ONE single page/tab for the ENTIRE run. Every sport, every tab
    navigation happens via page.goto() on this same page. No new tabs, no
    windows, no captcha explosion.
    """
    stats = {"team_props": 0, "season_props": 0, "daily_props": 0}
    configs = get_active_configs()

    logger.info(
        f"Starting daily scrape: {len(configs)} sport(s), 1 book (fanduel)"
    )

    browser = await get_browser()
    page = await browser.context.new_page()
    start_time = time.time()

    try:
        for config in configs:
            logger.info(f"=== Scraping FanDuel {config.name} ===")
            await run_sport(page, config, stats)
    finally:
        await page.close()

    elapsed = time.time() - start_time
    logger.info(
        f"Daily scrape complete in {elapsed:.1f}s. "
        f"Stats: {stats}"
    )
    return stats


def main():
    """Sync entry point for cron — standalone mode."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    logger.info("=" * 60)
    logger.info("Daily Sportsbook Scraper — Starting")
    logger.info("=" * 60)

    asyncio.run(run_daily_scrape())

    # Shut down the persistent browser since this is standalone
    asyncio.run(stop_browser())


if __name__ == "__main__":
    main()
