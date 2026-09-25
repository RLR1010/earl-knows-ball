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


async def _qc_historical_tags(db) -> None:
    """QC guard: historical game tags must be immutable.

    The rolling/cumulative builders partition by (season, season_type) and are
    rebuilt from source on every refresh. If a historical game's season/week/
    season_type silently changes, the affected seasons' features are rewritten and
    every model trained on them changes (this is the 2021-drift bug). We freeze a
    snapshot of (season, week, team_abbr, season_type) for every completed season
    and refuse to continue if a frozen season changes.
    """
    from sqlalchemy import text

    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS nfl.historical_tag_snapshot (
            season      integer     NOT NULL,
            week        integer     NOT NULL,
            team_abbr   text        NOT NULL,
            season_type text        NOT NULL,
            captured_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (season, week, team_abbr, season_type)
        )"""))
    await db.commit()

    max_season = (await db.execute(
        text("SELECT COALESCE(MAX(season), 0) FROM nfl.game_stats")
    )).scalar() or 0
    if not max_season:
        return

    frozen = sorted({int(s) for s in (await db.execute(
        text("SELECT DISTINCT season FROM nfl.historical_tag_snapshot")
    )).scalars().all()})

    problems = []
    for s in frozen:
        if s >= max_season:
            continue  # still-live seasons are not frozen
        cur = {(r[0], r[1], r[2]) for r in (await db.execute(text(
            "SELECT week, team_abbr, season_type FROM nfl.game_stats WHERE season = :s"
        ), {"s": s})).all()}
        old = {(r[0], r[1], r[2]) for r in (await db.execute(text(
            "SELECT week, team_abbr, season_type FROM nfl.historical_tag_snapshot WHERE season = :s"
        ), {"s": s})).all()}
        added, removed = cur - old, old - cur
        if added or removed:
            problems.append((s, sorted(added)[:6], sorted(removed)[:6], len(added), len(removed)))

    # Freeze any newly-completed season not yet snapshotted (season < live season).
    seasons_now = {int(x) for x in (await db.execute(text(
        "SELECT DISTINCT season FROM nfl.game_stats WHERE season < :m"
    ), {"m": max_season})).scalars().all()}
    to_freeze = sorted(seasons_now - set(frozen))
    for s in to_freeze:
        await db.execute(text("""
            INSERT INTO nfl.historical_tag_snapshot (season, week, team_abbr, season_type)
            SELECT DISTINCT season, week, team_abbr, season_type
            FROM nfl.game_stats WHERE season = :s
            ON CONFLICT DO NOTHING"""), {"s": s})
    if to_freeze:
        await db.commit()
        logger.info(f"  QC: froze historical tag snapshot for seasons {to_freeze}")

    if problems:
        for s, added, removed, na, nr in problems:
            logger.error(
                f"  QC FAIL: season {s} tags changed (+{na}/-{nr}) "
                f"added={added} removed={removed}"
            )
        raise RuntimeError(
            "QC guard: historical game tags changed (seasons "
            f"{sorted(p[0] for p in problems)}). Refusing to continue: historical "
            "game data must be immutable (a change silently rewrites historical "
            "features and drifts every model trained on them)."
        )
    logger.info(f"  QC: historical tags unchanged across {len(frozen)} frozen season(s)")


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
        # QC guard (pre): historical game tags (season/week/team/season_type) must be
        # immutable. A change here silently rewrites historical rolling/cumulative
        # features and therefore every model trained on them. Compare against the
        # frozen snapshot BEFORE we touch anything so we surface drift from a prior
        # run loudly instead of silently retraining on mutated history.
        await _qc_historical_tags(db)

        # Step 0: ingest the POSTSEASON schedule so playoff games exist in
        # nfl.games with game_type='POST' BEFORE any stats/rolling rebuild.
        # Keeps future postseasons separated from REG (the 2016-2025 data was
        # fixed once via migrate_nfl_postseason_game_type.py; this prevents
        # re-contamination going forward). Idempotent and non-destructive:
        # only ADDs POST games that aren't already present (the delete that used
        # to wipe the season is commented out — see espn.py ingest_espn_schedule).
        logger.info("[Step 0] Syncing NFL postseason schedule (seasontype=3)...")
        try:
            from app.ingestion.espn import ingest_espn_schedule
            await ingest_espn_schedule(db, season_year=season, seasontype=3)
            logger.info(f"  postseason schedule synced for {season}")
        except Exception as e:
            _e = str(e)
            if "404" in _e or "Not Found" in _e or season > date.today().year:
                logger.info(f"  postseason schedule: none yet for {season} (benign)")
            elif "postseason has not begun" in _e.lower():
                logger.info(f"  postseason schedule: not started yet ({season}) (benign)")
            else:
                logger.error(f"  Postseason schedule sync failed: {e}")
                step_failures.append(f"postseason_schedule: {e}")
                try:
                    await db.rollback()
                except Exception:
                    pass

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
        team_res = await run_in_thread(run_team_rolling, game_type, seasons=[season])
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
        tb_res = await run_in_thread(run_team_bad, seasons=[season])
        logger.info(f"  team_badweather_stats: {tb_res}")
    except Exception as e:
        logger.error(f"  team_badweather_stats failed: {e}")
        step_failures.append(f"team_badweather_stats: {e}")

    # Step 9: QB bad-weather passer rating (leak-free, prior starts)
    logger.info("[Step 9] Refreshing nfl.qb_badweather_stats...")
    try:
        from app.handicapping.nfl.populate_qb_badweather_stats import run as run_qb_bad
        qb_bad_res = await run_in_thread(run_qb_bad, seasons=[season])
        logger.info(f"  qb_badweather_stats: {qb_bad_res}")
    except Exception as e:
        logger.error(f"  qb_badweather_stats failed: {e}")
        step_failures.append(f"qb_badweather_stats: {e}")

    # Step 10: complete player-stat detail (ESPN gap-fill) + rebuild ALL
    # player rolling tables. nflverse (Step 1) only fills a ~5-col defensive
    # subset; this ESPN core-API pass hydrates the full 39-col defensive/ST
    # set for NEW games, then rebuilds defensive/skill/qb/kicker rolling so
    # they stay fresh (they were previously NOT maintained by the refresh).
    # gap_fill=True => additive ON CONFLICT DO UPDATE, never deletes.
    logger.info("[Step 10] ESPN player detail gap-fill + defensive/skill/kicker rolling rebuild...")
    try:
        from app.ingestion.nfl_player_game_stats import _run as _nfl_player_run
        # async, self-contained (own engine + psycopg2 conn); gap-fill is
        # additive ON CONFLICT DO UPDATE and never deletes, so it safely
        # complements the nflverse Step 1 for NEW games.
        await _nfl_player_run([season], game_type=game_type, gap_fill=True)
        logger.info("  ESPN player detail gap-fill done")
    except Exception as e:
        logger.error(f"  nfl player detail gap-fill failed: {e}")
        step_failures.append(f"nfl_player_detail: {e}")

    logger.info("[Step 10b] Rebuilding nfl.defensive_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nfl.populate_defensive_rolling_stats import populate_defensive_rolling_stats
        dr_res = await run_in_thread(populate_defensive_rolling_stats, sync_engine, [season], game_type)
        logger.info(f"  defensive_rolling_stats: {dr_res}")
    except Exception as e:
        logger.error(f"  defensive_rolling_stats failed: {e}")
        step_failures.append(f"defensive_rolling_stats: {e}")

    logger.info("[Step 10c] Rebuilding nfl.skill_rolling_stats + kicker_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nfl.populate_skill_rolling_stats import populate_skill_rolling_tables
        sk_res = await run_in_thread(populate_skill_rolling_tables, sync_engine, game_type=game_type, seasons=[season])
        logger.info(f"  skill_rolling_stats: {sk_res}")
    except Exception as e:
        logger.error(f"  skill_rolling_stats failed: {e}")
        step_failures.append(f"skill_rolling_stats: {e}")

    # Step 11: settle FINAL games' prediction results (ats/ou/ml_result) so
    # schedule-card picks show Win (green) / Loss (red) / Push (grey).
    # Idempotent - skips already-settled predictions (ats_result IS NULL guard).
    logger.info("[Step 11] Settling NFL prediction results for final games...")
    try:
        from app.handicapping.nfl.settle_predictions import settle_nfl_predictions
        import asyncpg as _asyncpg
        from app.db_urls import PSYCOPG2_DATABASE_URL as _URL
        _conn = await _asyncpg.connect(_URL)
        try:
            _n = await settle_nfl_predictions(_conn)
            logger.info(f"  settled {_n} prediction result(s)")
        finally:
            await _conn.close()
    except Exception as e:
        logger.error(f"  settle prediction results failed: {e}")
        step_failures.append(f"settle_prediction_results: {e}")

    # Step 12: backfill time of possession for newly-final games (from ESPN core
    # team stats). Idempotent - only processes games where nfl.games possession
    # columns are still NULL (i.e. never fetched), updating both nfl.games (home/
    # away) and nfl.game_stats (per-team) at once.
    logger.info("[Step 12] Backfilling time of possession for final games...")
    try:
        from app.ingestion.nfl_team_stats_espn import backfill_time_of_possession
        _pt = await backfill_time_of_possession()
        logger.info(f"  TOP backfill: {_pt}")
    except Exception as e:
        logger.error(f"  TOP backfill failed: {e}")
        step_failures.append(f"top_backfill: {e}")

    # Step 13: purge stale derived rows whose game_type disagrees with nfl.games.
    # Legacy builds wrote postseason games as 'REG' in the derived stat tables, and
    # the upserts (PK includes game_type) never deleted the bogus twins -> two rows
    # per playoff game, which contaminates game_type-filtered history lookups.
    # The current builders derive game_type from nfl.games, so this normally removes 0.
    logger.info("[Step 13] Purging stale game_type rows in derived stat tables...")
    try:
        from app.handicapping.nfl.stale_game_type import purge_stale_game_type_rows
        import asyncpg as _asyncpg_stale
        from app.db_urls import PSYCOPG2_DATABASE_URL as _STALE_URL
        _stale_conn = await _asyncpg_stale.connect(_STALE_URL)
        try:
            _stale = await purge_stale_game_type_rows(_stale_conn, apply=True)
        finally:
            await _stale_conn.close()
        _stale_removed = {k: v for k, v in _stale.items() if v}
        logger.info(f"  stale game_type rows removed: {_stale_removed or 'none'}")
    except Exception as e:
        logger.error(f"  stale game_type purge failed: {e}")
        step_failures.append(f"stale_game_type_purge: {e}")

    # QC guard (post): re-check that this refresh did not mutate any historical tag.
    from app.database import async_session as _qc_session
    async with _qc_session() as _qc_db:
        await _qc_historical_tags(_qc_db)

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
