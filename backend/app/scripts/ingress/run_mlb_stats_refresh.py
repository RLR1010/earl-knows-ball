#!/usr/bin/env python3
"""
MLB stats refresh — standalone subprocess job.

Runs via the Earl task scheduler as a `subprocess` task (previously an
`api_call` hitting /ingest/mlb/stats/refresh). Moved off the granian event loop
so it can never block a request-serving worker.

Previously this was a fire-and-forget `asyncio.create_task` inside a granian
worker loop; the route returned ~242ms "success" and the scheduler recorded a
fake success before the background work (with real failures) finished. Now the
entire refresh runs in a real OS subprocess, reports nothing until it is
actually done, and updates the real `task_runs` row via report_task_outcome.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ingress/run_mlb_stats_refresh.py

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
logger = logging.getLogger("earl.mlb_stats_refresh")


async def run(started_at=None):
    """Run MLB stats refresh in background.

    Every run syncs rosters, game statuses, probable pitchers, lineups,
    boxscores, cumulative/rolling/bullpen stats. The full batting/pitching
    season stats + games load only when stale (> 6h) or never run — a
    state-based gate that self-heals if a refresh fails, replacing the old
    time-based morning-only check.
    """
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
    logger = logging.getLogger("earl.mlb_stats_refresh")
    logger.info("BACKGROUND TASK: MLB stats refresh starting...")

    from app.database import async_session
    from app.ingestion.mlb_stats import (
        sync_teams, sync_seasons,
        load_batting_season, load_pitching_season,
        load_games_for_season, update_probable_pitchers, update_game_statuses,
        sync_all_team_rosters,
        MLB_TEAMS,
    )
    from app.models.mlb import MLBBattingStats, MLBPitchingStats
    from sqlalchemy import select

    CURRENT_YEAR = 2026

    # State-based full-refresh gate (replaces old time-based `hour < 10` check):
    # Load full batting/pitching season stats + games whenever the last full
    # refresh is stale (> 6h) or has never succeeded. This self-heals — if a
    # refresh fails or the service is down in the morning, the next 30-min run
    # picks it up instead of waiting until tomorrow's clock gate.
    FULL_REFRESH_STALE_SECONDS = 6 * 60 * 60

    if started_at is None:
        from datetime import datetime as _dt, timezone as _tz
        started_at = _dt.now(_tz.utc)
    step_failures: list[str] = []

    async with async_session() as db:
        need_full_refresh = await mlb_full_refresh_due(db, CURRENT_YEAR, FULL_REFRESH_STALE_SECONDS)
        await db.commit()
        logger.info("=" * 60)
        label = "Full Refresh" if need_full_refresh else "Incremental Refresh (lineups, pitchers, stats)"
        logger.info(f"MLB Stats {label}")
        logger.info(f"Targeting year: {CURRENT_YEAR}")
        logger.info("=" * 60)

        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        await db.commit()

        season_id = season_map.get(CURRENT_YEAR)
        if not season_id:
            logger.error(f"Season {CURRENT_YEAR} not found")
            return

        team_abbr_by_api_id = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}

        if need_full_refresh:
            # Batting
            logger.info(f"[Step 1] Loading batting stats for {CURRENT_YEAR}...")
            await load_batting_season(db, CURRENT_YEAR, season_id, team_map, team_abbr_by_api_id)
            r = await db.execute(
                select(MLBBattingStats).where(MLBBattingStats.season_id == season_id)
            )
            logger.info(f"  Batting {CURRENT_YEAR}: {len(r.scalars().all())} entries")

            # Pitching
            logger.info(f"[Step 2] Loading pitching stats for {CURRENT_YEAR}...")
            await load_pitching_season(db, CURRENT_YEAR, season_id, team_map)
            r = await db.execute(
                select(MLBPitchingStats).where(MLBPitchingStats.season_id == season_id)
            )
            logger.info(f"  Pitching {CURRENT_YEAR}: {len(r.scalars().all())} entries")

            # Games
            logger.info(f"[Step 3] Loading games for {CURRENT_YEAR}...")
            games = await load_games_for_season(db, CURRENT_YEAR, season_id, team_map, team_abbr_by_api_id)
            logger.info(f"  Games {CURRENT_YEAR}: {games}")

            # Full refresh succeeded — record timestamp so the staleness gate
            # resets. Only reached if batting+pitching+games all loaded.
            await mlb_mark_full_refresh(db, CURRENT_YEAR)
            await db.commit()

        else:
            logger.info("[Skipping] Full stats refresh — recent refresh still fresh")

        # Step 4: Active roster sync (always run)
        logger.info("[Step 4] Syncing active 40-man rosters from MLB Stats API...")
        try:
            roster_result = await sync_all_team_rosters(db, team_map)
            summary = roster_result.get("_summary", {})
            logger.info(f"  Active: {summary.get('total_active', 0)}, IL: {summary.get('total_injured', 0)}")
        except Exception as e:
            logger.error(f"  Roster sync failed: {e}")
            step_failures.append(f"Roster sync: {e}")

        # Step 5: Game status updates (always run)
        logger.info("[Step 5] Updating game statuses from MLB Stats API...")
        status_result = await update_game_statuses(db)
        logger.info(f"  Status changes: {len(status_result.get('status_changes', {}))}, rescheduled: {status_result.get('rescheduled', 0)}")

        # Step 6: Probable pitchers (always run)
        logger.info("[Step 6] Updating probable pitchers for upcoming games...")
        pitcher_result = await update_probable_pitchers(db)
        pitchers_changed = pitcher_result.get('games_updated', 0)
        logger.info(f"  Probable pitchers updated: {pitchers_changed}")

        # Step 7: Starting lineups (always run)
        logger.info("[Step 7] Fetching starting lineups...")
        from datetime import date
        try:
            from app.ingestion.mlb_lineups import update_lineups_for_date
            today = date.today()
            lineup_result = await update_lineups_for_date(db, today)
            logger.info(f"  Lineups: {lineup_result.get('lineups_saved', 0)} saved, {lineup_result.get('pitchers_updated', 0)} pitchers updated")
        except Exception as e:
            logger.error(f"  Lineups fetch failed: {e}")
            step_failures.append(f"Lineups: {e}")

        # (Step 7b removed 2026-08-04)
        # Pick-card regeneration now handled entirely by the `mlb-lines-and-picks`
        # task (every 15 min), which re-predicts all future games with both spread
        # and OU set. Regenerating here on every pitcher/lineup change duplicated
        # that expensive model load for no unique value.

        # Step 8: Load boxscore stats for FINAL games (batting_game_stats, pitcher_game_stats)
        # Uses asyncpg to match boxscore_ingest's connection type
        logger.info("[Step 8] Loading boxscores for recent FINAL games...")
        try:
            import asyncpg
            from urllib.parse import urlparse
            from app.db_urls import PSYCOPG2_DATABASE_URL
            # PSYCOPG2_DATABASE_URL already reflects .env DATABASE_URL (asyncpg suffix stripped)
            db_url = PSYCOPG2_DATABASE_URL
            parsed = urlparse(db_url)
            pconn = await asyncpg.connect(
                user=parsed.username or "earl",
                password=parsed.password or PSYCOPG2_DATABASE_URL.split("@")[0].split(":")[-1],
                database=parsed.path.lstrip("/") or "earl_knows_football",
                host=parsed.hostname or "localhost",
                port=parsed.port or 5432,
            )
            from app.ingestion.boxscore_ingest import (
                refresh_boxscores_for_recent_games,
            )
            try:
                boxscore_result = await refresh_boxscores_for_recent_games(pconn)
                logger.info(f"  Boxscores: {boxscore_result['games_processed']} games, "
                            f"{boxscore_result['batting_rows']} batting rows, "
                            f"{boxscore_result['pitching_rows']} pitching rows")
            except Exception as e:
                logger.error(f"  Boxscore loading failed: {e}")
                step_failures.append(f"Boxscores: {e}")

            # Step 9 removed 2026-08-04: prediction-result writes were redundant —
            # refresh_boxscores_for_recent_games already writes actuals for FINAL games.
        except Exception as e:
            logger.error(f"  Outer boxscore block failed: {e}")

        # Step 10: Refresh cumulative season-to-date stats
        # Pre-computed in mlb.cumulative_game_stats table for fast GAME_QUERY
        try:
            from app.handicapping.mlb.data_loader import refresh_cumulative_stats
            from app.core.config import settings
            result = refresh_cumulative_stats(db_url=settings.database_url_sync)
            logger.info(
                f"  Step 10: Cumulative stats refreshed — "
                f"{result.get('total_inserted', 0)} new rows"
            )
        except Exception as e:
            logger.error(f"  Step 10 cumulative stats refresh failed: {e}")
            step_failures.append(f"Cumulative stats: {e}")

        # Step 11: Refresh pre-computed rolling team & pitcher stats
        try:
            from app.handicapping.mlb.populate_rolling import (
                populate_team_rolling,
                populate_pitcher_rolling,
            )
            team_rows = populate_team_rolling(incremental=True)
            pitcher_rows = populate_pitcher_rolling(incremental=True)
            logger.info(
                f"  Step 11: Rolling stats refreshed — "
                f"{team_rows} team rows, {pitcher_rows} pitcher rows"
            )
        except Exception as e:
            logger.error(f"  Step 11 rolling stats refresh failed: {e}")
            step_failures.append(f"Rolling stats: {e}")

        # Step 12: Refresh bullpen game stats (from pitcher_game_stats WHERE is_starter=FALSE)
        try:
            from app.handicapping.mlb.populate_bullpen_stats import populate_bullpen_stats
            from app.core.config import settings
            from sqlalchemy import create_engine
            engine = create_engine(settings.database_url_sync)
            bullpen_rows = populate_bullpen_stats(engine)
            logger.info(
                f"  Step 12: Bullpen stats refreshed — {bullpen_rows} rows"
            )
        except Exception as e:
            logger.error(f"  Step 12 bullpen stats refresh failed: {e}")
            step_failures.append(f"Bullpen stats: {e}")

        # Step 13: Refresh mlb.team_ops_vs_arm (cumulative team OPS/WINS vs arm)
        # Cumulatives partition by season, so rebuild the current (in-progress)
        # season fully each refresh — correct AND fast (~1s). Historical seasons
        # are only corrected by an explicit full rebuild.
        try:
            from app.handicapping.mlb.populate_team_ops_vs_arm import \
                populate_team_ops_vs_arm
            from sqlalchemy import text
            cur_season = (await db.execute(
                text("SELECT MAX(season_id) FROM mlb.games WHERE status = 'FINAL'")
            )).scalar()
            logger.info(f"  Step 13: team_ops_vs_arm rebuild for season {cur_season}...")
            populate_team_ops_vs_arm(season=cur_season)
        except Exception as e:
            logger.error(f"  Step 13 team_ops_vs_arm refresh failed: {e}")
            step_failures.append(f"team_ops_vs_arm: {e}")

        # Step 13b: Refresh mlb.team_runs_vs_arm (cumulative runs-scored-vs-arm)
        # Powers the h/a rpg_vs_* loader features. Same season-scoped pattern.
        try:
            from app.handicapping.mlb.populate_team_runs_vs_arm import \
                populate_team_runs_vs_arm
            from sqlalchemy import text
            cur_season = (await db.execute(
                text("SELECT MAX(season_id) FROM mlb.games WHERE status = 'FINAL'")
            )).scalar()
            logger.info(f"  Step 13b: team_runs_vs_arm rebuild for season {cur_season}...")
            populate_team_runs_vs_arm(season=cur_season)
        except Exception as e:
            logger.error(f"  Step 13b team_runs_vs_arm refresh failed: {e}")
            step_failures.append(f"team_runs_vs_arm: {e}")

        # Step 14: Refresh per-hitter rolling stats (player_batting_rolling_stats)
        # Powers OPS/hitter features in GAME_QUERY. Windows are season-scoped, so
        # incremental (auto-target current season) emits only new FINAL games' rows.
        try:
            from app.handicapping.mlb.populate_batting_rolling import populate
            n = populate(incremental=True)
            logger.info(f"  Step 14: per-hitter batting rolling refreshed (+{n} rows)")
        except Exception as e:
            logger.error(f"  Step 14 per-hitter batting rolling refresh failed: {e}")
            step_failures.append(f"player_batting_rolling_stats: {e}")

        await pconn.close()

        # Commit all changes (lineups, pitchers, picks)
        try:
            await db.commit()
        except Exception as e:
            logger.error(f"Final commit failed: {e}")
            step_failures.append(f"Final commit: {e}")

        # Report the REAL outcome (overwrites the scheduler's fake dispatch success).
        if step_failures:
            joined = "; ".join(step_failures)
            logger.error(f"\n❌ MLB stats {label} finished WITH ERRORS:\n  {joined}")
            await report_task_outcome("mlb-stats-refresh", success=False, error=joined, started_at=started_at)
        else:
            logger.info(f"\n✅ MLB stats {label} complete!")
            await report_task_outcome("mlb-stats-refresh", success=True, started_at=started_at)


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
        logger.error("mlb stats refresh CRASHED: " + traceback.format_exc())
        # Only report here if the worker never got a chance to (hard crash).
        try:
            await async_session_commit_crash("mlb", started_at)
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
