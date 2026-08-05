"""Daily BetMGM season-prop / futures ingestion.

Standalone script run by the admin task system (``betmgm-season-props``).

Fetches BetMGM's CDS futures fixtures for MLB/NFL/NBA and saves:
  - player season props (awards: MVP, Cy Young, ROY, DPOY, etc.)
    to {sport}.player_season_props
  - team props (championship odds, make/miss playoffs, win totals)
    to {sport}.team_props
Bookmaker is 'betmgm'. Runs once daily (uses the shared headed Firefox session,
like the FanDuel scraper) and should not overlap the fanDuel daily run.

Run:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/run_betmgm_season_props.py
"""
import asyncio
import logging
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("betmgm_season_props")


async def _run() -> dict:
    from app.scrapers.betmgm_run import run_betmgm_scrape
    from app.scrapers.browser import stop_browser

    logger.info("=" * 60)
    logger.info("BetMGM season-prop scraper — Starting")
    logger.info("=" * 60)
    t0 = time.time()
    try:
        stats = await run_betmgm_scrape()
    finally:
        # Give the headed browser a moment to release the display before exit.
        try:
            await stop_browser()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"browser shutdown issue: {e}")
    logger.info(f"BetMGM season-prop scrape finished in {time.time()-t0:.1f}s: {stats}")
    return stats


def main() -> None:
    stats = asyncio.run(_run())
    logger.info(f"DONE {stats}")


if __name__ == "__main__":
    main()
    sys.exit(0)
