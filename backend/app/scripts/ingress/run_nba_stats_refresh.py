#!/usr/bin/env python3
"""
NBA stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/nba/stats/refresh). Moved off the granian event loop
so it can never block a request-serving worker.

Previously this was a fire-and-forget `asyncio.create_task` inside a granian
worker loop; the route returned ~242ms "success" and the scheduler recorded a
fake success before the background work (with real failures) finished. Now the
entire refresh runs in a real OS subprocess, reports nothing until it is
actually done, and updates the real `task_runs` row via report_task_outcome.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_nba_stats_refresh.py

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
logger = logging.getLogger("earl.nba_stats_refresh")


async def run(started_at=None):
    """Refresh the four NBA statistical tables for the current season.

    NBA games + player game stats come from ESPN/NBA Stats (async). Cumulative
    and team rolling stats are sync scripts, run on worker threads so they
    don't block a granian worker.
    """
    import logging

    logger = logging.getLogger("earl.nba_stats_refresh")

    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    logger.info("=" * 60)
    logger.info("NBA Stats Refresh")

    from datetime import date
    from app.database import async_session

    if started_at is None:
        from datetime import datetime as _dt, timezone as _tz
        started_at = _dt.now(_tz.utc)
    step_failures: list[str] = []

    # Resolve the active/upcoming NBA season from the calendar month. nba.seasons.year
    # stores the season START year (fall). So: Jul-Dec of year Y => the season that starts
    # this fall = Y (e.g. Aug 2026 => 2026-27 = year 2026); Jan-Jun of year Y => the season
    # still running = Y-1 (e.g. Feb 2026 => 2025-26 = year 2025). Previously this was simply
    # date.today().year, which was off-by-one in winter/spring (and, with the old games-walk,
    # would have re-ingested the ended season instead of the released-but-uningested one).
    _m = date.today().month
    season = date.today().year if _m >= 7 else date.today().year - 1
    logger.info(f"  active NBA season (month={_m}) -> year {season} ({season}-{season+1})")

    # Step 1: nba.games — current season schedule (ESPN), idempotent
    logger.info("[Step 1] Syncing current-season nba.games from ESPN...")
    try:
        from app.ingestion.nba_games_espn import ingest_nba_games
        games_result = await ingest_nba_games([season])
        logger.info(f"  nba.games: {games_result}")
    except Exception as e:
        logger.error(f"  nba.games sync failed: {e}")
        step_failures.append(f"nba.games: {e}")

    # Step 2: nba.player_game_stats — per-game boxscores (async, idempotent)
    logger.info("[Step 2] Ingesting nba.player_game_stats...")
    try:
        from app.ingestion.nba_player_game_stats import ingest_season
        pgs_result = await ingest_season(season)
        logger.info(f"  player_game_stats rows: {pgs_result}")
    except Exception as e:
        logger.error(f"  player_game_stats failed: {e}")
        step_failures.append(f"player_game_stats: {e}")

    # Step 2.5: nba.games team box-score stats (ESPN team-statistics endpoint)
    # Fill the new team-level columns (real ORB/DRB, estimatedPossessions,
    # pointsInPaint, fastBreakPoints, turnoverPoints, team/total turnovers,
    # lead/flow, fouling detail, advanced ratios/ratings, VORP...). Authoritative
    # from ESPN's team-statistics endpoint — NOT derivable by summing players.
    logger.info("[Step 2.5] Ingesting nba.games team box-score stats from ESPN...")
    try:
        from sqlalchemy import create_engine as _create_engine, text as _text
        from app.db_urls import PSYCOPG2_DATABASE_URL as _PDU
        from app.ingestion.nba_team_stats_espn import run_for_games as _run_team_stats
        _eng = _create_engine(_PDU.replace("+asyncpg", "+psycopg2"))
        with _eng.connect() as _conn:
            _need = _conn.execute(_text(
                "SELECT g.nba_game_id, g.id FROM nba.games g "
                "WHERE g.nba_game_id IS NOT NULL AND g.home_estimated_possessions IS NULL "
                "AND g.season_id = :s",
            ), {"s": season}).fetchall()
        _games = {int(r[0]): int(r[1]) for r in _need}
        if _games:
            logger.info(f"  fetching team stats for {len(_games)} {season} games...")
            _upd, _err = await _run_team_stats(_games, throttle=0.12)
            logger.info(f"  team-stats: {_upd} sides updated, {_err} errors")
        else:
            logger.info("  no games need team-stats backfill (all covered)")
    except Exception as e:
        logger.error(f"  team-stats failed: {e}")
        step_failures.append(f"team_stats: {e}")

    # Step 3: nba.cumulative_game_stats — sync script on worker thread
    logger.info("[Step 3] Refreshing nba.cumulative_game_stats...")
    try:
        from app.db_urls import PSYCOPG2_DATABASE_URL
        from app.handicapping.nba.cumulative_stats import populate_cumulative_stats
        cum_result = await run_in_thread(populate_cumulative_stats, PSYCOPG2_DATABASE_URL, [season])
        logger.info(f"  cumulative_game_stats: {cum_result}")
    except Exception as e:
        logger.error(f"  cumulative_game_stats failed: {e}")
        step_failures.append(f"cumulative_game_stats: {e}")

    # Step 4: nba.team_rolling_stats — sync script on worker thread
    logger.info("[Step 4] Refreshing nba.team_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nba.populate_team_rolling_stats import populate_team_rolling
        roll_result = await run_in_thread(populate_team_rolling, sync_engine, True)
        logger.info(f"  team_rolling_stats: {roll_result}")
    except Exception as e:
        logger.error(f"  team_rolling_stats failed: {e}")
        step_failures.append(f"team_rolling_stats: {e}")

    # Report the REAL outcome to task_runs
    if step_failures:
        joined = "; ".join(step_failures)
        logger.error(f"\n❌ NBA stats refresh finished WITH ERRORS:\n  {joined}")
        await report_task_outcome("nba-stats-refresh", success=False, error=joined, started_at=started_at)
    else:
        logger.info(f"\n✅ NBA stats refresh complete!")
        await report_task_outcome("nba-stats-refresh", success=True, started_at=started_at)


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
        logger.error("nba stats refresh CRASHED: " + traceback.format_exc())
        # Only report here if the worker never got a chance to (hard crash).
        try:
            await async_session_commit_crash("nba", started_at)
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
