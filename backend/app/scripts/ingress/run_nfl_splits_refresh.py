"""Standalone subprocess runner for NFL player splits refresh.

Rebuilds nfl.player_splits from nfl.player_weekly_stats x nfl.games
(home/away, temperature, dome, grass/turf, division, primetime — career +
per-season). Idempotent full-replace. Intended to be run by the task scheduler
as a `subprocess` task, and also ad-hoc for backfills.

Usage:
    PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nfl_splits_refresh.py [SEASON_ID ...]
    (no args = refresh ALL seasons)
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
logger = logging.getLogger("earl.nfl_splits_runner")


async def _run():
    from app.database import async_session
    from app.ingestion.nfl_splits import build_player_splits

    season_ids = None
    if len(sys.argv) > 1:
        season_ids = [int(a) for a in sys.argv[1:] if a.isdigit()]

    async with async_session() as db:
        result = await build_player_splits(db, season_ids=season_ids)
        await db.commit()
        logger.info("nfl player splits refresh complete: %s", result)
        print(f"RESULT: {result}")


if __name__ == "__main__":
    asyncio.run(_run())
