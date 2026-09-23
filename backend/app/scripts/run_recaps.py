#!/usr/bin/env python3
"""Scheduled task: generate post-game recap articles.

A recap is a short, free, SEO-rich article written the next morning after a game
that grades our pre-game premium pick and prop plays. Recaps are stored as
``original_articles`` rows (``section='recap'``) so they appear in the sport and
team Article listings.

Runs as a `subprocess` task (cron "10 7 * * *", tz America/New_York): that is
off-peak for DeepSeek pricing, after the overnight article scrapes, and before
the next day's preview writeups run at 8:00 AM ET.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/run_recaps.py [sport|all]
    # options: --lookback-hours N  --limit N  --dry-run
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

from app.writeups.recaps import SPORTS, run_all_sports, run_recaps  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("run_recaps")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Generate post-game recap articles.")
    parser.add_argument(
        "sport",
        nargs="?",
        default="all",
        help="mlb | nfl | nba | all (default: all)",
    )
    parser.add_argument("--lookback-hours", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    targets = sorted(SPORTS) if args.sport == "all" else [args.sport]
    for sport in targets:
        if sport not in SPORTS:
            logger.error("unknown sport %r; expected one of %s or 'all'", sport, sorted(SPORTS))
            return 2

    kwargs = dict(
        lookback_hours=args.lookback_hours,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    if args.sport == "all":
        created = asyncio.run(run_all_sports(**kwargs))
        total = sum(len(v) for v in created.values())
        for sport, recs in created.items():
            logger.info("%s: %d recap(s) created", sport, len(recs))
        logger.info("done: %d recap(s) total", total)
    else:
        recs = asyncio.run(run_recaps(args.sport, **kwargs))
        logger.info("done: %d recap(s) created", len(recs))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
