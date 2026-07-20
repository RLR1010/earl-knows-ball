"""
Shared Playwright browser setup for FanDuel scraping.

Uses storage state persistence to maintain a FanDuel session:
  1. First run: inject bootstrap cookies → FD accepts → Playwright gets its
     own session cookies → saved to storage_state.json
  2. Subsequent runs: load saved storage state — no manual cookies needed.

Playwright's storage_state captures all cookies + localStorage, which
is tied to Playwright's browser fingerprint after bootstrap.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext
from playwright_stealth import Stealth

logger = logging.getLogger("earl.scrapers.browser")

# Location for persistent storage state
STORAGE_STATE_PATH = Path(__file__).parent / "storage_state.json"


class BrowserManager:
    """Manages a single headless Chromium instance with stealth + session persistence."""

    def __init__(self):
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._stealth = Stealth()

    async def start(self) -> None:
        """Launch Playwright and create the browser."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        logger.info("Browser launched")

    async def stop(self) -> None:
        """Shut everything down."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser shut down")

    async def new_context(
        self, bootstrap_cookies: Optional[list[dict]] = None
    ) -> BrowserContext:
        """Create a fresh browser context with stealth and session persistence.

        Args:
            bootstrap_cookies: Optional list of cookie dicts to inject for the
                               first run. After a successful scrape, the session
                               is saved and reused automatically.

        Cookie dict format: {"name": str, "value": str, "domain": str, "path": str}
        """
        if not self._browser:
            raise RuntimeError("Browser not started")

        # Check if we have a saved storage state
        if STORAGE_STATE_PATH.exists():
            try:
                context = await self._browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/130.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1920, "height": 1080},
                    locale="en-US",
                    timezone_id="America/New_York",
                    storage_state=str(STORAGE_STATE_PATH),
                )
                await context.add_init_script(self._stealth.script_payload)
                logger.info(
                    "Loaded saved FD session from "
                    f"{STORAGE_STATE_PATH.name}"
                )
                return context
            except Exception as e:
                logger.warning(
                    f"Failed to load storage state, falling back: {e}"
                )

        # No saved state → new context with optional bootstrap cookies
        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script(self._stealth.script_payload)

        if bootstrap_cookies:
            await context.add_cookies(bootstrap_cookies)
            logger.info("Injected bootstrap cookies")

        return context

    async def save_storage_state(self, context: BrowserContext) -> bool:
        """Save the current context's storage state for future use."""
        try:
            state = await context.storage_state()
            with open(STORAGE_STATE_PATH, "w") as f:
                json.dump(state, f, indent=2)
            logger.info(f"Saved FD session to {STORAGE_STATE_PATH}")
            return True
        except Exception as e:
            logger.warning(f"Failed to save storage state: {e}")
            return False
