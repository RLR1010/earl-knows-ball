"""Full clean rebuild of all MLB derived stat tables after the pgs.ip repair.

Dependency order:
  1. cumulative_game_stats   (source of per-game boxscore aggregates)  — force_rebuild (truncate)
  2. bullpen_game_stats      (from pgs / game boxscores)               — full_backfill
  3. pitcher_rolling_stats   (from cumulative/pgs)                     — non-incremental (truncate)
  4. team_rolling_stats      (from cumulative)                          — non-incremental (truncate)

Run: cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/rebuild_mlb_derived.py
"""
import asyncio
import logging
import time

import sqlalchemy as sa

from app.core.config import settings
from app.database import async_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("earl.mlb_rebuild")


async def rebuild() -> None:
    t0 = time.time()

    # 1) Cumulative (sync path, force_rebuild truncates)
    from app.handicapping.mlb.cumulative_stats import populate_cumulative_stats
    s = time.time()
    res = populate_cumulative_stats(settings.database_url_sync, force_rebuild=True)
    log.info(f"[1/4] cumulative_game_stats rebuilt in {time.time()-s:.0f}s -> {res}")

    # 2) Bullpen (full backfill truncates)
    from app.handicapping.mlb.populate_bullpen_stats import populate_bullpen_stats
    s = time.time()
    engine = sa.create_engine(settings.database_url_sync)
    rows = populate_bullpen_stats(engine, full_backfill=True)
    engine.dispose()
    log.info(f"[2/4] bullpen_game_stats rebuilt in {time.time()-s:.0f}s -> {rows} rows")

    # 3+4) Pitcher + team rolling (non-incremental => each truncates its table)
    from app.handicapping.mlb.populate_rolling import populate_team_rolling, populate_pitcher_rolling
    s = time.time()
    n = populate_team_rolling(incremental=False)
    log.info(f"[3/4] team_rolling_stats rebuilt in {time.time()-s:.0f}s -> {n} rows")
    s = time.time()
    n = populate_pitcher_rolling(incremental=False)
    log.info(f"[4/4] pitcher_rolling_stats rebuilt in {time.time()-s:.0f}s -> {n} rows")

    log.info(f"ALL DONE in {(time.time()-t0)/60:.1f} min")


if __name__ == "__main__":
    asyncio.run(rebuild())
