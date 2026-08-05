"""
BetMGM season-prop / futures scraper runner.

Mirrors daily_run.py but targets BetMGM (API-based) instead of FanDuel (DOM).
Runs once per day: fetches BetMGM's CDS futures/awards fixtures for all sports,
parses into normalized models, and saves to the per-sport DB tables.

Uses the same persistent headed Firefox session as the FanDuel scraper (the
shared Cloudflare/geo cookies in the profile let us reach BetMGM's CDS API).

Usage:
    python -m backend.app.scrapers.betmgm_run
"""

import asyncio
import logging
import re
import sys
import time

from playwright.async_api import Page
from sqlalchemy import create_engine

from app.core.config import settings
from app.scrapers.browser import get_browser, stop_browser
from app.scrapers.books import betmgm
from app.scrapers.db import save_team_props, save_player_season_props
from app.scrapers.sports import get_active_configs

logger = logging.getLogger("earl.scrapers.betmgm_run")

# Sync DB engine for standalone scraper
sync_db_url = settings.database_url.replace("+asyncpg", "+psycopg2")
engine = create_engine(sync_db_url, pool_pre_ping=True)

# BetMGM CDS sport ids
SPORT_IDS = {"mlb": 23, "nfl": 11, "nba": 7}

# Sport id -> landing page path (to establish the right session origin)
LANDING = {
    "mlb": "/en/sports/baseball-23",
    "nfl": "/en/sports/american-football-11",
    "nba": "/en/sports/basketball-7",
}


async def _capture_accessid(page: Page, base: str) -> str:
    """Return BetMGM's x-bwin-accessid token seen in a live request."""
    accessid = {"v": None}

    def on_req(request):
        m = re.search(r"x-bwin-accessid=([A-Za-z0-9]+)", request.url)
        if m and accessid["v"] is None:
            accessid["v"] = m.group(1)

    page.on("request", on_req)
    try:
        await page.goto(base + LANDING["mlb"], wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(6000)
    except Exception as e:
        logger.warning(f"betmgm base landing issue (will retry in loop): {e}")
    page.remove_listener("request", on_req)
    if not accessid["v"]:
        raise RuntimeError("Could not capture BetMGM x-bwin-accessid token")
    return accessid["v"]


async def run_sport(page: Page, config, accessid: str, stats: dict) -> None:
    """Scrape one sport from BetMGM via the CDS API and save to DB."""
    sport = config.name
    sport_id = SPORT_IDS.get(sport)
    if sport_id is None:
        logger.warning(f"[betmgm] unknown sport {sport}")
        return

    logger.info(f"[betmgm {sport.upper()}] fetching CDS fixtures (sportId={sport_id})")
    t0 = time.time()
    try:
        data = await betmgm.fetch_sport_fixtures(page, accessid, sport_id)
        team_props, season_props = betmgm.parse_sport_fixtures(config, accessid, data)
    except Exception as e:
        logger.error(f"[betmgm {sport.upper()}] fetch/parse failed: {e}")
        return
    logger.info(f"[betmgm {sport.upper()}] parsed {len(team_props)} team props, "
                f"{len(season_props)} season props in {time.time()-t0:.1f}s")

    c = 0
    if team_props:
        try:
            c = save_team_props(engine, team_props)
        except Exception as e:
            logger.error(f"[betmgm {sport.upper()}] save_team_props failed: {e}")
    stats["team_props"] += c

    c2 = 0
    if season_props:
        try:
            c2 = save_player_season_props(engine, season_props)
        except Exception as e:
            logger.error(f"[betmgm {sport.upper()}] save_player_season_props failed: {e}")
    stats["season_props"] += c2


async def run_betmgm_scrape() -> dict:
    stats = {"team_props": 0, "season_props": 0}
    configs = get_active_configs()

    logger.info("Starting BetMGM scrape")

    browser = await get_browser()
    page = await browser.context.new_page()
    start_time = time.time()
    try:
        # Establish session + capture accessid once
        accessid = await _capture_accessid(page, betmgm.BASE)
        logger.info(f"Captured BetMGM accessid (len={len(accessid)})")

        for config in configs:
            if not (config.scrape_team_props or config.scrape_awards):
                continue
            # Ensure the page origin is the right sport before the in-page fetch
            landing = LANDING.get(config.name)
            if landing:
                try:
                    await page.goto(betmgm.BASE + landing, wait_until="domcontentloaded",
                                    timeout=45000)
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    logger.warning(f"[betmgm] landing {config.name} failed: {e}")
            await run_sport(page, config, accessid, stats)
    finally:
        await page.close()

    elapsed = time.time() - start_time
    logger.info(f"BetMGM scrape complete in {elapsed:.1f}s. Stats: {stats}")
    return stats


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logger.info("=" * 60)
    logger.info("BetMGM Season-Prop Scraper — Starting")
    logger.info("=" * 60)

    asyncio.run(run_betmgm_scrape())
    asyncio.run(stop_browser())


if __name__ == "__main__":
    main()
