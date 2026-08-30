"""Standalone subprocess runner for the NFL depth-chart refresh.

Scrapes current Ourlads depth charts for all 32 teams into nfl.depth_charts
(via app.ingestion.depth_charts.scrape_all_teams). Each team's chart is a
full-replace (old entries deleted, new ones inserted) committed per team by
the scraper, so the runner is idempotent and safe to run on a schedule.

Intended to be run by the task scheduler as a `subprocess` task, and also
ad-hoc. The per-team scraper is resilient: transient per-team failures are
captured as entries in the result dict and do not abort the rest of the run.

Usage:
    PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nfl_depth_charts.py
"""
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("earl.nfl_depth_charts_runner")


async def _run():
    from app.database import async_session
    from app.ingestion.depth_charts import scrape_all_teams

    async with async_session() as db:
        result = await scrape_all_teams(db)
        # scraper commits per team; no extra commit needed here.
        logger.info("nfl depth chart refresh complete: %s", result)
        print(f"RESULT: {result}")


if __name__ == "__main__":
    asyncio.run(_run())
