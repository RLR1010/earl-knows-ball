#!/usr/bin/env python3
"""Weekly power-rankings refresh (compute ratings -> generate blurbs).

Idempotent. Recomputes the current season's weekly rating snapshots and then
regenerates the per-team blurbs for the latest played week.

Runs as a compute-role scheduler task (``power-rankings-nfl``).

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/run_power_rankings_weekly.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.db_urls import ASYNC_DATABASE_URL  # noqa: E402
from app.ingestion import compute_power_ratings as cpr  # noqa: E402
from app.ingestion import generate_power_ranking_blurbs as gpb  # noqa: E402
from app.ingestion import load_injuries as li  # noqa: E402
from app.ingestion import compute_injury_adjustments as cia  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("power_rankings_weekly")


async def latest_season_week():
    """(season, latest played week) for NFL REG FINAL games."""
    engine = create_async_engine(ASYNC_DATABASE_URL)
    try:
        async with engine.connect() as c:
            return (await c.execute(text("""
                SELECT s.year, MAX(g.week) AS wk
                FROM nfl.games g JOIN nfl.seasons s ON s.id = g.season_id
                WHERE g.game_type = 'REG' AND g.status = 'FINAL'
                  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                GROUP BY s.year ORDER BY s.year DESC LIMIT 1
            """))).first()
    finally:
        await engine.dispose()


async def main() -> None:
    row = await latest_season_week()
    if not row:
        log.warning("no FINAL NFL games found; nothing to do")
        return
    season, week = int(row[0]), int(row[1])
    log.info("refreshing power ratings for season=%s week<=%s", season, week)

    await li.load([season])                    # refresh this season's injury reports
    await cia.compute([season])                # injury -> points adjustment per team/week
    await cpr.main(season)                     # recompute snapshots (season + prior)
    await gpb.main(season, week)               # regenerate blurbs for latest week
    # render the weekly social card (best-effort; never fail the job on render)
    try:
        from app.social.power_rankings_card import generate as gen_card
        path, url = await asyncio.to_thread(gen_card, "nfl", season, week)
        log.info("social card -> %s (%s)", url, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("social card generation failed: %s", exc)
    log.info("power-rankings refresh complete (season=%s week=%s)", season, week)


if __name__ == "__main__":
    asyncio.run(main())
