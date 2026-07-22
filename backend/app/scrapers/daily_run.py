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

from sqlalchemy import create_engine

from app.core.config import settings
from app.scrapers.browser import BrowserManager, get_browser, stop_browser
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
    """Scrape one sport from FanDuel via the persistent browser context.

    All three tabs (futures -> awards -> games) share the same long-lived
    browser context so that cookies persist naturally across navigations.
    """
    sport_name = config.name.upper()
    ctx = browser.context  # shared persistent context
    sport_had_data = False

    try:
        # --- Team Props (futures) ---
        if config.scrape_team_props:
            t0 = time.time()
            try:
                team_props = await fanduel.scrape_team_props(ctx, config.name)
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

        # Brief cooldown between tabs
        await asyncio.sleep(5)

        # --- Awards ---
        if config.scrape_awards:
            t0 = time.time()
            try:
                award_props = await fanduel.scrape_awards(ctx, config.name)
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

        await asyncio.sleep(5)

        # --- Player Daily Props ---
        if config.scrape_player_props:
            t0 = time.time()
            try:
                player_props = await fanduel.scrape_player_props(ctx, config.name)
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
    """Main orchestrator. Gets the persistent browser and scrapes all sports."""
    stats = {"team_props": 0, "season_props": 0, "daily_props": 0}
    configs = get_active_configs()

    logger.info(
        f"Starting daily scrape: {len(configs)} sport(s), 1 book (fanduel)"
    )

    browser = await get_browser()
    start_time = time.time()

    for config in configs:
        logger.info(f"=== Scraping FanDuel {config.name} ===")
        await run_sport(browser, config, stats)

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
