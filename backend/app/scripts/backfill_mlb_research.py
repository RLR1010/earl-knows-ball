"""Backfill external MLB research sources on dev.

Usage:
    PYTHONPATH=$PWD <venv>/bin/python app/scripts/backfill_mlb_research.py [--what all|bwar|savant]
        [--start 2002] [--end 2026]
"""
from __future__ import annotations

import argparse
import asyncio
import logging

from app.ingestion.mlb_research import ingest_bwar, ingest_savant_expected
from app.ingestion.mlb_savant import ingest_savant, ingest_savant_framing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("backfill.mlb_research")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--what", default="all")
    ap.add_argument("--start", type=int, default=2002)
    ap.add_argument("--end", type=int, default=2026)
    args = ap.parse_args()
    what = args.what

    if what in ("all", "bwar"):
        res = await ingest_bwar(args.start, args.end)
        log.info("bwar done: %s", res)

    if what in ("framing",):
        lo = max(args.start, 2015)
        res = await ingest_savant_framing(list(range(args.end, lo - 1, -1)))
        log.info("framing done: %s", res)

    if what in ("all", "savant", "savant_all"):
        lo = max(args.start, 2015)
        seasons = list(range(args.end, lo - 1, -1))
        res = await ingest_savant(seasons)
        log.info("savant done: %s", res)


if __name__ == "__main__":
    asyncio.run(main())
