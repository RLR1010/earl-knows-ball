"""
Shared Playwright browser setup for FanDuel scraping.

ONE persistent Firefox session (headed, on :0) that lives as long as the API.
No more open/close cycles — the session accumulates cookies naturally, and
if DataDome ever serves a captcha, Rich can answer it in the visible browser.

Use get_browser() / stop_browser() at module level — the singleton pattern
ensures one shared instance across the entire application.
"""

import logging
from typing import Optional

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright_stealth import Stealth

logger = logging.getLogger("earl.scrapers.browser")

# ── Module-level singleton ──────────────────────────────────────────────
_BROWSER: Optional["BrowserManager"] = None


async def get_browser() -> "BrowserManager":
    """Return the persistent browser singleton, starting it if needed."""
    global _BROWSER
    if _BROWSER is None:
        _BROWSER = BrowserManager()
        await _BROWSER.start()
    return _BROWSER


async def stop_browser() -> None:
    """Shut down the persistent browser singleton."""
    global _BROWSER
    if _BROWSER:
        await _BROWSER.stop()
        _BROWSER = None


# ── Browser Manager class ───────────────────────────────────────────────

class BrowserManager:
    """Manages ONE persistent headed Firefox session."""

    def __init__(self):
        self._playwright = None
        self._context: Optional[BrowserContext] = None
        self._stealth = Stealth()

    async def start(self) -> None:
        """Launch Firefox (headed on :0) using a persistent profile.

        Uses launch_persistent_context with a real Firefox profile directory
        so the browser looks like a normal user's Firefox, not a Playwright-
        launched temp profile. This avoids DataDome's headless/automation
        detection.
        """
        import tempfile
        from pathlib import Path

        logger.info("Starting persistent browser (headed on :0)...")

        # Use a stable profile directory so cache/cookies survive restarts
        profile_dir = Path.home() / ".openclaw" / "fd-profile"
        profile_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.firefox.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/Chicago",
        )

        # Apply stealth patches to disguise automation signals.
        # DataDome's JS checks for navigator.webdriver, Playwright-specific
        # properties, and other automation fingerprints. Stealth patches these
        # on every page load via route interception.
        await self._stealth.apply_stealth_async(self._context)

        logger.info(f"Persistent browser ready (profile: {profile_dir})")

    async def stop(self) -> None:
        """Shut down the persistent browser."""
        if self._context:
            await self._context.close()
            self._context = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Persistent browser shut down")

    @property
    def context(self) -> BrowserContext:
        """Return the persistent browser context."""
        if not self._context:
            raise RuntimeError("Browser not started")
        return self._context

    async def new_page(self) -> Page:
        """Create a new page/tab in the persistent context."""
        return await self.context.new_page()
