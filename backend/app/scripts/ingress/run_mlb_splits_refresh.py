#!/usr/bin/env python3
"""
MLB per-player split stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task. Populates
``mlb.player_splits`` for:

  - API-driven splits: batter L/R (vs_lhp/vs_rhp), home/away, day/night,
    grass/turf ... from the MLB Stats API ``statSplits`` endpoint.
  - Derived city splits (city_<slug>) aggregated from the stored game log.

These back Earl's chat research (``get_player_split_stats``) and the premium
Prop Bets writeup so Earl can quote e.g. "Ramirez hits .322 vs LHP, .289 at
home" from real data.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_mlb_splits_refresh.py [YEAR]

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Keep per-request httpx noise out of journald; only our earl logger is INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("earl.mlb_splits_refresh")


def current_year() -> int:
    """Current calendar year used as the default split season year."""
    return datetime.now().year


async def main(year: int) -> int:
    from app.ingestion.mlb_splits import refresh_all

    async with async_session() as db:
        result = await refresh_all(db, year=year)
    logger.info(
        f"MLB splits refresh done: hitters={result['hitters']}, "
        f"api_splits={result['api_splits']}, city_keys={result['city_keys']}, team_lr={result.get('team_lr')}"
    )
    return 0


if __name__ == "__main__":
    try:
        year = int(sys.argv[1]) if len(sys.argv) > 1 else current_year()
    except ValueError:
        logger.error(f"Invalid year argument: {sys.argv[1]}")
        sys.exit(2)
    try:
        sys.exit(asyncio.run(main(year)))
    except Exception:
        logger.exception("MLB splits refresh fatal error")
        sys.exit(2)
