#!/usr/bin/env python3
"""
NBA team splits refresh (home/away, vs East/West team form + ATS/O/U).

Standalone subprocess job for the Earl task scheduler. Populates
``nba.team_splits`` from finalized ``nba.games`` (scores + team box stats)
x ``nba.betting_lines`` (consensus closing spread/total). No new external
data source.

Split types: home | away | vs_east | vs_west, plus a career row per
team/split (season_id NULL). Includes wins/losses, points for/against,
shooting rates, and against-the-spread / over-under records — the form
Earl needs to handicap NBA team matchups.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nba_team_splits_refresh.py

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
logger = logging.getLogger("earl.nba_team_splits_refresh")


async def main() -> int:
    from app.ingestion.nba_team_splits import build_team_splits

    async with async_session() as db:
        result = await build_team_splits(db)
    logger.info(
        f"NBA team splits refresh done: teams={result['teams']}, "
        f"rows_written={result['rows_written']}, season_ids={result['season_ids']}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        logger.exception("NBA team splits refresh fatal error")
        sys.exit(2)
