"""
DraftKings sportsbook scraper — NOTE: BLOCKED

DraftKings uses Akamai bot protection that blocks headless browsers.
Even with Playwright + stealth, the API endpoint returns 403.

The /api/v5/eventgroups/ endpoint returns 403 from raw HTTP.
The sportsbook-nash.draftkings.com data endpoint also 403s from raw HTTP.
Even from within a Playwright browser, these API calls fail.

This module is kept as a reference. Do not use in production until
a bypass is found (e.g., residential proxies, session reuse, etc.).
"""

import logging
from typing import Optional

from playwright.async_api import BrowserContext

from app.scrapers.models import TeamProp, PlayerSeasonProp, PlayerDailyProp

logger = logging.getLogger("earl.scrapers.draftkings")

# All scrape functions return empty lists — DK is blocked.

async def scrape_team_props(context: BrowserContext, sport: str) -> list[TeamProp]:
    logger.warning(f"DK {sport}: DraftKings API is blocked by Akamai. Returning empty.")
    return []


async def scrape_awards(context: BrowserContext, sport: str) -> list[PlayerSeasonProp]:
    logger.warning(f"DK {sport}: DraftKings API is blocked by Akamai. Returning empty.")
    return []


async def scrape_player_props(
    context: BrowserContext, sport: str
) -> list[PlayerDailyProp]:
    logger.warning(f"DK {sport}: DraftKings API is blocked by Akamai. Returning empty.")
    return []
