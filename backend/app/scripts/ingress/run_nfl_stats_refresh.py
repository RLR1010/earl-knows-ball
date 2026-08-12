#!/usr/bin/env python3
"""
NFL stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/nfl/stats/refresh). Moved off the granian event loop
so it can never block a request-serving worker.

Previously this was a fire-and-forget `asyncio.create_task` inside a granian
worker loop; the route returned ~242ms "success" and the scheduler recorded a
fake success before the background work (with real failures) finished. Now the
entire refresh runs in a real OS subprocess, reports nothing until it is
actually done, and updates the real `task_runs` row via report_task_outcome.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nfl_stats_refresh.py

Exit code 0 on success, non-zero on failure.
"""

import asyncio
import logging
import os
import sys

# sys.path: make the repo importable when run as <repo>/backend/app/scripts/...py
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402
from app.scripts.ingress._ingest_common import (  # noqa: E402
    run_in_thread,
    report_task_outcome,
    mlb_full_refresh_due,
    mlb_mark_full_refresh,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("earl.nfl_stats_refresh")


async def run(started_at=None, game_type: str = "REG"):
    """Run NFL stats refresh in background.

    game_type scopes every derived-stat rebuild (cumulative, team_rolling,
    qb_*) so preseason (PRE) can be built in isolation from regular season.
    """
    import logging

    logger = logging.getLogger("earl.nfl_stats_refresh")

    # nflverse files are downloaded to local cache; silence requests info spam.
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("NFL Stats Refresh")

    from datetime import date
    from app.database import async_session

    if started_at is None:
        from datetime import datetime as _dt, timezone as _tz
        started_at = _dt.now(_tz.utc)
    step_failures: list[str] = []

    season = date.today().year

    async with async_session() as db:
        # Step 1: nflverse player week stats (nfl.player_weekly_stats) — idempotent
        logger.info("[Step 1] Loading nflverse player weekly stats...")
        try:
            from app.ingestion.nflverse import ingest_nflverse_stats
            player_result = await ingest_nflverse_stats(db, season,
                                                         include_preseason=(game_type.upper() == "PRE"))
            logger.info(f"  player_weekly_stats: {player_result}")
        except Exception as e:
            _e = str(e)
            if "404" in _e or "Not Found" in _e:
                # nflverse-data not yet published for this season (during preseason
                # 2026) — benign, not a task failure.
                logger.info(f"  player weekly stats: no data yet ({season}) — skipping (benign)")
            else:
                logger.error(f"  Player weekly stats failed: {e}")
                step_failures.append(f"player_weekly_stats: {e}")
            # Recover from the failed transaction so later steps can commit.
            try:
                await db.rollback()
            except Exception:
                pass

        # Step 2: raw play-by-play (nfl.play_by_play) — idempotent per game
        logger.info("[Step 2] Loading nflverse play-by-play...")
        try:
            from app.ingestion.nflverse_pbp import ingest_nfl_pbp
            pbp_result = await ingest_nfl_pbp(db, [season])
            logger.info(f"  play_by_play: {pbp_result.get('games_loaded', pbp_result)}")
        except Exception as e:
            _e = str(e)
            if "404" in _e or "Not Found" in _e:
                logger.info(f"  play-by-play: no data yet ({season}) — skipping (benign)")
            else:
                logger.error(f"  Play-by-play failed: {e}")
                step_failures.append(f"play_by_play: {e}")
            try:
                await db.rollback()
            except Exception:
                pass

        # Step 3: base per-game team stats (nfl.game_stats) from nflverse team data
        # ingest_all_years is a sync script (needs a sync Engine); run on worker thread.
        logger.info("[Step 3] Building nfl.game_stats base rows from nflverse team stats...")
        try:
            from app.database import engine as sync_engine
            from app.ingestion.nflverse_ingest import ingest_all_years
            stored = await run_in_thread(ingest_all_years, sync_engine, [season])
            logger.info(f"  game_stats base rows: {stored}")
        except Exception as e:
            _e = str(e)
            if "404" in _e or "Not Found" in _e:
                logger.info(f"  game_stats base: no data yet ({season}) — skipping (benign)")
            else:
                logger.error(f"  game_stats base build failed: {e}")
                step_failures.append(f"game_stats base: {e}")
            try:
                await db.rollback()
            except Exception:
                pass

        # Step 4: advanced per-game aggregates (UPDATE nfl.game_stats)
        logger.info("[Step 4] Aggregating advanced per-game stats (pbp_game_stats)...")
        try:
            from app.ingestion.pbp_game_stats import aggregate_pbp_to_game_stats
            agg_result = await aggregate_pbp_to_game_stats(db, seasons=[season])
            logger.info(f"  advanced game_stats: {agg_result}")
        except Exception as e:
            logger.error(f"  Advanced game stats failed: {e}")
            step_failures.append(f"game_stats advanced: {e}")
            try:
                await db.rollback()
            except Exception:
                pass

        # Step 5: recompute cumulative game stats + QB rankings (qb_cumulative_stats)
        logger.info("[Step 5] Recomputing cumulative + QB rankings...")
        try:
            from app.handicapping.nfl.cumulative_stats import recompute
            cum_result = await recompute(db, seasons=[season], game_type=game_type)
            logger.info(f"  cumulative/qb_cumulative: {cum_result}")
        except Exception as e:
            logger.error(f"  Cumulative recompute failed: {e}")
            step_failures.append(f"cumulative_stats: {e}")
            try:
                await db.rollback()
            except Exception:
                pass

        try:
            await db.commit()
        except Exception as e:
            logger.warning(f"  Commit skipped (no active transaction): {e}")
            try:
                await db.rollback()
            except Exception:
                pass

    # Step 6 + 7: rolling stats — sync scripts on worker threads (no worker pinning)
    logger.info("[Step 6] Refreshing nfl.team_rolling_stats...")
    try:
        from app.handicapping.nfl.populate_team_rolling_stats import run as run_team_rolling
        team_res = await run_in_thread(run_team_rolling, game_type)
        logger.info(f"  team_rolling_stats: {team_res}")
    except Exception as e:
        logger.error(f"  team_rolling_stats failed: {e}")
        step_failures.append(f"team_rolling_stats: {e}")

    logger.info("[Step 7] Refreshing nfl.qb_cumulative_stats + qb_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nfl.populate_qb_rolling_stats import populate_qb_tables
        qb_res = await run_in_thread(populate_qb_tables, sync_engine, [season], game_type)
        logger.info(f"  qb_cumulative/qb_rolling: {qb_res}")
    except Exception as e:
        logger.error(f"  QB rolling stats failed: {e}")
        step_failures.append(f"qb_rolling_stats: {e}")

    # Step 8: team bad-weather situational stats (leak-free, prior games)
    logger.info("[Step 8] Refreshing nfl.team_badweather_stats...")
    try:
        from app.handicapping.nfl.populate_team_badweather_stats import run as run_team_bad
        tb_res = await run_in_thread(run_team_bad)
        logger.info(f"  team_badweather_stats: {tb_res}")
    except Exception as e:
        logger.error(f"  team_badweather_stats failed: {e}")
        step_failures.append(f"team_badweather_stats: {e}")

    # Step 9: QB bad-weather passer rating (leak-free, prior starts)
    logger.info("[Step 9] Refreshing nfl.qb_badweather_stats...")
    try:
        from app.handicapping.nfl.populate_qb_badweather_stats import run as run_qb_bad
        qb_bad_res = await run_in_thread(run_qb_bad)
        logger.info(f"  qb_badweather_stats: {qb_bad_res}")
    except Exception as e:
        logger.error(f"  qb_badweather_stats failed: {e}")
        step_failures.append(f"qb_badweather_stats: {e}")

    # Report the REAL outcome to task_runs
    if step_failures:
        joined = "; ".join(step_failures)
        logger.error(f"\n❌ NFL stats refresh finished WITH ERRORS:\n  {joined}")
        await report_task_outcome("nfl-stats-refresh", success=False, error=joined, started_at=started_at)
    else:
        logger.info(f"\n✅ NFL stats refresh complete!")
        await report_task_outcome("nfl-stats-refresh", success=True, started_at=started_at)


async def _run_standalone() -> int:
    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc)
    try:
        # The worker body reports its own outcome internally (report_task_outcome)
        # on success OR failure, mirroring the old fire-and-forget flow.
        await run(started_at)
        return 0
    except Exception:
        import traceback
        logger.error("nfl stats refresh CRASHED: " + traceback.format_exc())
        # Only report here if the worker never got a chance to (hard crash).
        try:
            await async_session_commit_crash("nfl", started_at)
        except Exception:
            pass
        return 1


async def async_session_commit_crash(sport: str, started_at) -> None:
    from datetime import datetime, timezone
    from app.scripts.ingress._ingest_common import report_task_outcome
    await report_task_outcome(
        sport + "-stats-refresh", success=False, error="crash", started_at=started_at,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(_run_standalone()))
