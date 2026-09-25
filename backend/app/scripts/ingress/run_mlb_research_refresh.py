"""Daily refresh of external MLB research sources (Baseball Savant + Baseball-Reference WAR).

Refreshes only the current and previous season (those are the only ones that change day-to-day).
Scheduled task: `mlb-research-refresh`.  Newest-first, idempotent.
"""
from __future__ import annotations

import asyncio
import datetime
import logging

from app.ingestion.mlb_research import ingest_bwar
from app.ingestion.mlb_savant import ingest_savant

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run.mlb_research_refresh")


async def main() -> None:
    y = datetime.date.today().year
    seasons = [y, y - 1]  # newest-first
    log.info("refreshing MLB research sources for seasons %s", seasons)
    b = await ingest_bwar(y - 1, y)
    log.info("bwar refreshed: %s", b)
    s = await ingest_savant(seasons)
    log.info("savant refreshed: %s", s)


if __name__ == "__main__":
    asyncio.run(main())
