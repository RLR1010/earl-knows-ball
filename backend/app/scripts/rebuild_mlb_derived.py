"""
Rebuild MLB derived rolling/cumulative tables after the boxscore backfill.

Order (dependency chain):
  1. cumulative_game_stats      (team cumulative, from batting+pitcher boxscores)
  2. team_rolling_stats         (from cumulative + bullpen)
  3. pitcher_rolling_stats      (from pitcher_game_stats + cumulative)

Run after a boxscore backfill so newly-added batting/pitching rows flow into the
rollups the data_loader reads.

Usage (non-login shell):
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/rebuild_mlb_derived.py         # full rebuild all seasons
    ./venv/bin/python app/scripts/rebuild_mlb_derived.py --skip-cumulative
"""
import argparse
import sys
import os
import logging
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("earl.mlb_derived_rebuild")

DB = settings.database_url_sync


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-cumulative", action="store_true",
                    help="Skip rebuilding cumulative_game_stats (e.g. if already done)")
    ap.add_argument("--incremental", action="store_true",
                    help="Rolling rebuilds incremental instead of full")
    args = ap.parse_args()

    t0 = time.time()
    if not args.skip_cumulative:
        logger.info("== Rebuilding cumulative_game_stats ==")
        from app.handicapping.mlb.cumulative_stats import populate_cumulative_stats
        summary = populate_cumulative_stats(db_url=DB, seasons=None, force_rebuild=True)
        logger.info("cumulative_game_stats summary: %s", summary)
    else:
        logger.info("Skipping cumulative_game_stats (--skip-cumulative)")

    logger.info("== Rebuilding team_rolling_stats + pitcher_rolling_stats ==")
    import importlib
    pr = importlib.import_module("app.handicapping.mlb.populate_rolling")
    n_team = pr.populate_team_rolling(engine=None, incremental=args.incremental)
    n_pitch = pr.populate_pitcher_rolling(engine=None, incremental=args.incremental)
    logger.info("team_rolling_stats rows=%s, pitcher_rolling_stats rows=%s", n_team, n_pitch)
    logger.info("DONE in %.1fs", time.time() - t0)


if __name__ == "__main__":
    main()
