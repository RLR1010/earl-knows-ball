#!/usr/bin/env python3
"""
NBA player splits + career stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task. Populates
``nba.player_splits`` (per-game-derived splits + career rows) for:

  - home / away
  - vs_east / vs_west (opponent conference)
  - starter / bench
  - rest0 (back-to-back) / rest_ge1 (1+ days rest)
  - month_<abbr> (per-season only)

... plus a career aggregate row per split (season_id NULL) computed from the
entire game-log history. No external data source — derived from
``nba.player_game_stats`` x ``nba.games``.

These back Earl's chat research (``get_player_split_stats``) so Earl can quote
e.g. "Jokic averages 28.9 ppg at home, rests on back-to-backs" from real data.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nba_splits_refresh.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("earl.nba_splits_refresh")


async def main() -> int:
    from app.ingestion.nba_splits import build_player_splits

    async with async_session() as db:
        result = await build_player_splits(db)
        # build_player_splits does DELETE + batched INSERT but does NOT commit;
        # without an explicit commit the whole refresh silently rolls back on
        # session close (async_sessionmaker auto-rollback on close).
        await db.commit()
    logger.info(
        f"NBA splits refresh done: players={result['players']}, "
        f"rows_written={result['rows_written']}, season_ids={result['season_ids']}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("NBA splits refresh fatal error")
        sys.exit(2)
