#!/usr/bin/env python3
"""Weekly power-rankings refresh, per sport (NFL / NBA / MLB).

Pipeline (all idempotent):
    1. derive the latest played season
    2. (NFL only) refresh injury reports + injury->points adjustments
    3. recompute the season's weekly rating snapshots
    4. regenerate the per-team blurbs for the latest played week
    5. render the weekly social card (best-effort)
    6. write the weekly POWER RANKINGS COLUMN as a published article

Runs as a compute-role scheduler task (``power-rankings-<sport>``).

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <venv>/bin/python app/scripts/run_power_rankings_weekly.py --sport nfl
    ... --sport nba|mlb
    ... --article-only            # skip ratings/blurbs, just (re)write the column
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.db_urls import ASYNC_DATABASE_URL  # noqa: E402
from app.ingestion import generate_power_ranking_blurbs as gpb  # noqa: E402
from app.ingestion import power_ratings_engine as pre  # noqa: E402
from app.writeups import power_rankings as pr  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("power_rankings_weekly")

# schema -> game_type value used for "regular season" in that sport's games table
SPORT_GAME_TYPE = {"nfl": "REG", "nba": "REG", "mlb": "R"}


async def latest_season(sport: str):
    """Newest season with a completed regular-season game."""
    gt = SPORT_GAME_TYPE.get(sport, "REG")
    engine = create_async_engine(ASYNC_DATABASE_URL)
    try:
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(
                        f"""
                        SELECT s.year
                        FROM {sport}.games g JOIN {sport}.seasons s ON s.id = g.season_id
                        WHERE g.game_type = :gt
                          AND g.status = 'FINAL'
                          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
                        GROUP BY s.year ORDER BY s.year DESC LIMIT 1
                        """
                    ),
                    {"gt": gt},
                )
            ).first()
            return int(row[0]) if row else None
    finally:
        await engine.dispose()


async def latest_week(sport: str, season: int):
    engine = create_async_engine(ASYNC_DATABASE_URL)
    try:
        async with engine.connect() as c:
            row = (
                await c.execute(
                    text(f"SELECT max(week) FROM {sport}.power_ratings WHERE season = :s"),
                    {"s": season},
                )
            ).first()
            return int(row[0]) if row and row[0] is not None else None
    finally:
        await engine.dispose()


async def refresh_ratings(sport: str, season: int) -> None:
    if sport == "nfl":
        # keep the live NFL engine + its injury inputs
        from app.ingestion import compute_injury_adjustments as cia  # noqa: E402
        from app.ingestion import compute_power_ratings as cpr  # noqa: E402
        from app.ingestion import load_injuries as li  # noqa: E402

        await li.load([season])
        await cia.compute([season])
        await cpr.main(season)
    else:
        await pre.main(sport, season)


async def write_article(sport: str) -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with Session() as db:
            res = await pr.run(db, [sport], force=True)
            for r in res:
                log.info("weekly column: %s", r)
    finally:
        await engine.dispose()


async def main(sport: str, article_only: bool = False) -> None:
    if not article_only:
        season = await latest_season(sport)
        if not season:
            log.warning("%s: no completed games found; nothing to do", sport)
            return
        log.info("%s: refreshing power ratings for season=%s", sport, season)

        await refresh_ratings(sport, season)

        week = await latest_week(sport, season)
        if not week:
            log.warning("%s: no rating snapshot after compute; skipping blurbs", sport)
            return
        await gpb.main(season, week, sport)
        log.info("%s: blurbs rebuilt for season=%s week=%s", sport, season, week)

        try:
            from app.social.power_rankings_card import generate as gen_card

            path, url = await asyncio.to_thread(gen_card, sport, season, week)
            log.info("%s: social card -> %s", sport, url)
        except Exception as exc:  # noqa: BLE001 - never fail the job on render
            log.warning("%s: social card generation failed: %s", sport, exc)

    await write_article(sport)
    log.info("%s: power-rankings weekly job complete", sport)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Weekly power-rankings refresh for one sport")
    ap.add_argument("--sport", default="nfl", choices=sorted(pr.SPORT_LABELS))
    ap.add_argument("--article-only", action="store_true",
                    help="skip ratings/blurbs; just write/refresh the weekly column")
    args = ap.parse_args()
    asyncio.run(main(args.sport, article_only=args.article_only))
