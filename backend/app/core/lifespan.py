"""FastAPI lifespan hooks — manage persistent resources."""

import logging

from app.scrapers.browser import get_browser, stop_browser

logger = logging.getLogger("earl.lifespan")


async def lifespan_setup(app):
    """Start persistent browser on API boot."""
    logger.info("Lifespan setup: starting persistent browser...")
    try:
        await get_browser()
        logger.info("Persistent browser started")
    except Exception as e:
        logger.error(f"Failed to start persistent browser: {e}")


async def lifespan_teardown(app):
    """Shut down persistent browser on API stop."""
    logger.info("Lifespan teardown: shutting down browser...")
    try:
        await stop_browser()
        logger.info("Persistent browser shut down")
    except Exception as e:
        logger.error(f"Error shutting down browser: {e}")
