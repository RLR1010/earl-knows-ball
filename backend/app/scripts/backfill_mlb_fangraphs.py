"""Backfill FanGraphs season leaderboards (batting/pitching/fielding; players + teams).

Newest-first, per-season, idempotent (DELETE season -> INSERT). Dev-first.

Usage:
    PYTHONPATH=$PWD <venv>/bin/python app/scripts/backfill_mlb_fangraphs.py [--start 2002] [--end 2026]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.ingestion.mlb_fangraphs import ensure_all, load_season

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill.mlb_fg")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=2002)
    ap.add_argument("--end", type=int, default=2026)
    ap.add_argument("--only", default="bat,pit,fld")
    args = ap.parse_args()

    stats = tuple(x.strip() for x in args.only.split(",") if x.strip())
    await ensure_all()
    for year in range(args.end, args.start - 1, -1):  # newest -> oldest
        counts = await load_season(year, stats=stats)
        log.info("season %d done: %s", year, counts)


if __name__ == "__main__":
    asyncio.run(main())
