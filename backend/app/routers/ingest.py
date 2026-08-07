"""
Data ingestion endpoints for EarlKnowsBall.
Trigger these to populate the database from various sources.
"""
import os
import sys
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.ingestion.espn import ingest_espn_schedule, update_live_nfl_games
from app.ingestion.nflverse import ingest_nflverse_stats
from app.ingestion.match_players import match_nflverse_ids
from app.ingestion.articles import scrape_rss_feeds
from app.ingestion.articles_mlb import scrape_rss_feeds_mlb
from app.ingestion.articles_nba import scrape_rss_feeds_nba
from app.ingestion.historical import generate_all_seasons
from app.ingestion.nflverse_data import ingest_draft_info, ingest_injuries, ingest_trades
from app.ingestion.nfl_pace import ingest_pace_data

router = APIRouter()


@router.post("/ingest/espn-schedule")
async def ingest_schedule(
    season: int = Query(2025, description="Season year (e.g. 2025)"),
    season_type: int = Query(2, description="1=preseason, 2=regular, 3=postseason"),
    db: AsyncSession = Depends(get_db),
):
    result = await ingest_espn_schedule(db, season_year=season, seasontype=season_type)
    return {"status": "ok", "source": "espn", **result}


@router.post("/ingest/nfl/live-refresh")
async def ingest_live_refresh(db: AsyncSession = Depends(get_db)):
    """Sync live NFL game statuses/scores from ESPN for games in progress.

    Self-skipping: if no NFL game is live or about to start, this returns
    immediately without calling ESPN (near-zero cost when there's no football).
    Intended to run every few minutes during the NFL season.
    """
    result = await update_live_nfl_games(db)
    return {"status": "ok", "endpoint": "nfl/live-refresh", **result}


@router.post("/ingest/match-players")
async def match_players(db: AsyncSession = Depends(get_db)):
    result = await match_nflverse_ids(db)
    return {"status": "ok", **result}


@router.post("/ingest/nflverse-historical")
async def ingest_historical_stats(
    start: int = Query(2005, description="Start season"),
    end: int = Query(2025, description="End season (inclusive)"),
    db: AsyncSession = Depends(get_db),
):
    results = []
    for year in range(start, end + 1):
        if year == 2025:
            continue  # already loaded
        result = await ingest_nflverse_stats(db, season_year=year)
        # Also aggregate PBP if available
        from app.ingestion.pbp_game_stats import aggregate_pbp_to_game_stats
        pbp_result = await aggregate_pbp_to_game_stats(db, seasons=[year])
        result["game_stats_updated"] = pbp_result.get(str(year), 0)
        results.append(result)
        print(f"[Earl] {year}: {result['stats_loaded']} stats, {result['game_stats_updated']} game_stats")
    return {"status": "ok", "source": "nflverse", "seasons": results}


@router.post("/ingest/articles/rss")
async def ingest_rss_articles(
    max_per_feed: int = Query(20, description="Max articles per feed"),
    skip_older_than_days: int = Query(30, description="Skip articles older than N days"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape NFL articles from RSS feeds into the database. Embeddings handled by pgvector embedder."""
    results = await scrape_rss_feeds(
        db,
        max_per_feed=max_per_feed,
        skip_older_than_days=skip_older_than_days,
    )
    return {"status": "ok", **results}


@router.post("/ingest/articles/mlb/rss")
async def ingest_mlb_rss_articles(
    max_per_feed: int = Query(20, description="Max articles per feed"),
    skip_older_than_days: int = Query(30, description="Skip articles older than N days"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape MLB articles from RSS feeds into mlb.articles."""
    results = await scrape_rss_feeds_mlb(
        db,
        max_per_feed=max_per_feed,
        skip_older_than_days=skip_older_than_days,
    )
    return {"status": "ok", **results}


@router.post("/ingest/articles/nba/rss")
async def ingest_nba_rss_articles(
    max_per_feed: int = Query(20, description="Max articles per feed"),
    skip_older_than_days: int = Query(30, description="Skip articles older than N days"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape NBA articles from RSS feeds into nba.articles."""
    results = await scrape_rss_feeds_nba(
        db,
        max_per_feed=max_per_feed,
        skip_older_than_days=skip_older_than_days,
    )
    return {"status": "ok", **results}


@router.post("/ingest/historical")
async def ingest_historical_seasons(
    start: int = Query(2005, description="First season year"),
    end: int = Query(2025, description="Last season year (inclusive)"),
    db: AsyncSession = Depends(get_db),
):
    """Generate season recaps for historical seasons using nflverse stats + Wikipedia."""
    result = await generate_all_seasons(db, start_year=start, end_year=end)
    return {"status": "ok", **result}


# ── Historical Games ──────────────────────────────────────────────────

@router.post("/ingest/nflverse/draft")
async def ingest_nflverse_draft(db: AsyncSession = Depends(get_db)):
    """Load draft info from nflverse players.csv and update Player records."""
    result = await ingest_draft_info(db)
    return {"status": "ok", **result}


@router.post("/ingest/nflverse/injuries")
async def ingest_nflverse_injuries(
    start_year: int = Query(2020, description="First year"),
    end_year: int = Query(2025, description="Last year (inclusive)"),
    db: AsyncSession = Depends(get_db),
):
    """Load injury data from nflverse for the given year range."""
    result = await ingest_injuries(db, years=list(range(start_year, end_year + 1)))
    return {"status": "ok", **result}


@router.post("/ingest/nflverse/trades")
async def ingest_nflverse_trades(db: AsyncSession = Depends(get_db)):
    """Load trade data from nflverse."""
    result = await ingest_trades(db)
    return {"status": "ok", **result}


# ── Betting Lines ───────────────────────────────────────────────────────


@router.post("/ingest/nfl/pace")
async def ingest_nfl_pace_data(
    years: str = Query(None, description="Comma-separated years e.g. '2022,2023,2024'. Defaults to 2012-current"),
    clear: bool = Query(False, description="Clear existing pace data before inserting"),
    db: AsyncSession = Depends(get_db),
):
    """
    Ingest NFL pace data from nflverse snap_counts.

    Downloads player-level snap counts, aggregates to team-game level,
    and stores in nfl.team_pace_stats table.
    """
    year_list = None
    if years:
        year_list = [int(y.strip()) for y in years.split(",")]
    result = await ingest_pace_data(db, years=year_list, clear_existing=clear)
    return {"status": "ok", "pace_data": result}


# ── MLB Daily Pipeline Endpoints ────────────────────────────────────

# Full-refresh staleness tracker. Replaces the old time-based (`hour < 10`)
# morning gate with a state-based check so a failed/skipped morning run
# self-heals on the next 30-min cycle.
_MLB_REFRESH_TRACKER_SQL = """
CREATE TABLE IF NOT EXISTS mlb.mlb_stats_refresh_tracker (
    year   INTEGER PRIMARY KEY,
    last_full_refresh_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def _mlb_full_refresh_due(db, year, stale_seconds):
    """Return True if the full batting/pitching/games refresh is stale or never ran."""
    from sqlalchemy import text as sa_text
    from datetime import datetime, timezone

    # Ensure tracker table exists (idempotent).
    await db.execute(sa_text(_MLB_REFRESH_TRACKER_SQL))

    row = (await db.execute(
        sa_text("""
            SELECT last_full_refresh_at
            FROM mlb.mlb_stats_refresh_tracker
            WHERE year = :y
        """),
        {"y": year},
    )).first()

    if row is None:
        return True  # never refreshed this season

    last_at = row[0]
    if last_at is None:
        return True
    # Normalize tz-aware comparison.
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - last_at
    return age.total_seconds() > stale_seconds


async def _mlb_mark_full_refresh(db, year):
    """Record a successful full refresh."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text("""
            INSERT INTO mlb.mlb_stats_refresh_tracker (year, last_full_refresh_at)
            VALUES (:y, now())
            ON CONFLICT (year) DO UPDATE SET last_full_refresh_at = now()
        """),
        {"y": year},
    )


async def _report_task_outcome(task_name: str, success: bool, error: str = "", started_at=None):
    """Overwrite the scheduler's dispatch-time `task_runs` row with the REAL
    outcome of a background refresh.

    The scheduler marks api_call tasks `success` the moment the endpoint returns
    (fire-and-forget, ~242ms). That fake status hides background failures. This
    helper updates the latest run for `task_name` with the true result once the
    detached work actually finishes.
    """
    from sqlalchemy import text as sa_text
    from datetime import datetime, timezone
    from app.database import async_session

    finished_at = datetime.now(timezone.utc)
    duration_ms = int((finished_at - started_at).total_seconds() * 1000) if started_at else 0

    async with async_session() as s:
        await s.execute(
            sa_text("""
                UPDATE task_runs
                SET status = :s, finished_at = :f, duration_ms = :d, error_message = :e
                WHERE id = (
                    SELECT id FROM task_runs
                    WHERE task_name = :t
                    ORDER BY started_at DESC, id DESC LIMIT 1
                )
            """),
            {
                "t": task_name,
                "s": "success" if success else "failed",
                "f": finished_at,
                "d": duration_ms,
                "e": (error or "")[:2000] if not success else None,
            },
        )
        await s.commit()


async def _run_mlb_stats_refresh(started_at=None):
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
        need_full_refresh = await _mlb_full_refresh_due(db, CURRENT_YEAR, FULL_REFRESH_STALE_SECONDS)
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
            await _mlb_mark_full_refresh(db, CURRENT_YEAR)
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
                            f"{boxscore_result['pitching_rows']} pitching rows, "
                            f"{boxscore_result.get('weather_updated', 0)} weather updates")
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
            await _report_task_outcome("mlb-stats-refresh", success=False, error=joined, started_at=started_at)
        else:
            logger.info(f"\n✅ MLB stats {label} complete!")
            await _report_task_outcome("mlb-stats-refresh", success=True, started_at=started_at)


@router.post("/ingest/mlb/stats/refresh")
async def ingest_mlb_stats_refresh():
    """
    Refresh MLB player stats from statsapi.mlb.com (fire-and-forget background task).

    Every run: syncs rosters, game statuses, probable pitchers, lineups,
    boxscores, and cumulative/rolling/bullpen stats.
    Full batting/pitching season stats + games load only when stale (> 6h)
    or never run (state-based gate; self-heals if a refresh fails).

    The scheduler records dispatch success immediately (~242ms); the detached
    task overwrites that with the REAL outcome on completion so the Tasks UI
    shows true success/failure.
    """
    import asyncio
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc)

    async def _run_reported():
        try:
            await _run_mlb_stats_refresh(started_at)
        except Exception as e:
            import traceback
            logger.error(f"MLB stats refresh CRASHED: {e}\n{traceback.format_exc()}")
            await _report_task_outcome("mlb-stats-refresh", success=False, error=f"{type(e).__name__}: {e}", started_at=started_at)

    asyncio.create_task(_run_reported())
    return {"status": "started", "message": "MLB stats refresh running in background. Check API logs for progress."}


@router.post("/ingest/nfl/stats/refresh")
async def ingest_nfl_stats_refresh(
    game_type: str = Query("REG", description="REG (default) or PRE for preseason stats"),
):
    """
    Refresh NFL stats from nflverse (fire-and-forget background task).

    Updates the six NFL statistical tables for the current season:
      - nfl.player_weekly_stats    (nflverse player stats; idempotent per week)
      - nfl.play_by_play           (raw nflverse PBP)
      - nfl.game_stats             (base per-game team stats + advanced aggregates)
      - nfl.cumulative_game_stats  (recompute, scoped to game_type) + qb_cumulative_stats
      - nfl.team_rolling_stats     (scoped to game_type; PRE/REG coexist)
      - nfl.qb_rolling_stats       (scoped to game_type)

    Pass game_type=PRE to build preseason stats in isolation. Preseason rows
    never mix into REG inference/training (the loader defaults to REG).
    """
    import asyncio
    from datetime import datetime, timezone

    default_game_type = (game_type or "REG").upper()
    started_at = datetime.now(timezone.utc)

    async def _run_reported():
        try:
            await _run_nfl_stats_refresh(started_at, game_type=default_game_type)
        except Exception:
            import traceback
            logger.error(f"NFL stats refresh CRASHED: {traceback.format_exc()}")
            await _report_task_outcome("nfl-stats-refresh", success=False, error=f"crash", started_at=started_at)

    asyncio.create_task(_run_reported())
    return {"status": "started", "message": f"NFL stats refresh running (game_type={default_game_type}). Check API logs."}


async def _run_nfl_stats_refresh(started_at=None, game_type: str = "REG"):
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
            logger.error(f"  Player weekly stats failed: {e}")
            step_failures.append(f"player_weekly_stats: {e}")

        # Step 2: raw play-by-play (nfl.play_by_play) — idempotent per game
        logger.info("[Step 2] Loading nflverse play-by-play...")
        try:
            from app.ingestion.nflverse_pbp import ingest_nfl_pbp
            pbp_result = await ingest_nfl_pbp(db, [season])
            logger.info(f"  play_by_play: {pbp_result.get('games_loaded', pbp_result)}")
        except Exception as e:
            logger.error(f"  Play-by-play failed: {e}")
            step_failures.append(f"play_by_play: {e}")

        # Step 3: base per-game team stats (nfl.game_stats) from nflverse team data
        # ingest_all_years is a sync script (needs a sync Engine); run on worker thread.
        logger.info("[Step 3] Building nfl.game_stats base rows from nflverse team stats...")
        try:
            from app.database import engine as sync_engine
            from app.ingestion.nflverse_ingest import ingest_all_years
            stored = await _run_in_thread(ingest_all_years, sync_engine, [season])
            logger.info(f"  game_stats base rows: {stored}")
        except Exception as e:
            logger.error(f"  game_stats base build failed: {e}")
            step_failures.append(f"game_stats base: {e}")

        # Step 4: advanced per-game aggregates (UPDATE nfl.game_stats)
        logger.info("[Step 4] Aggregating advanced per-game stats (pbp_game_stats)...")
        try:
            from app.ingestion.pbp_game_stats import aggregate_pbp_to_game_stats
            agg_result = await aggregate_pbp_to_game_stats(db, seasons=[season])
            logger.info(f"  advanced game_stats: {agg_result}")
        except Exception as e:
            logger.error(f"  Advanced game stats failed: {e}")
            step_failures.append(f"game_stats advanced: {e}")

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
            await db.commit()
        except Exception as e:
            logger.error(f"  Commit failed: {e}")
            step_failures.append(f"commit: {e}")

    # Step 6 + 7: rolling stats — sync scripts on worker threads (no worker pinning)
    logger.info("[Step 6] Refreshing nfl.team_rolling_stats...")
    try:
        from app.handicapping.nfl.populate_team_rolling_stats import run as run_team_rolling
        team_res = await _run_in_thread(run_team_rolling, game_type)
        logger.info(f"  team_rolling_stats: {team_res}")
    except Exception as e:
        logger.error(f"  team_rolling_stats failed: {e}")
        step_failures.append(f"team_rolling_stats: {e}")

    logger.info("[Step 7] Refreshing nfl.qb_cumulative_stats + qb_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nfl.populate_qb_rolling_stats import populate_qb_tables
        qb_res = await _run_in_thread(populate_qb_tables, sync_engine, [season], game_type)
        logger.info(f"  qb_cumulative/qb_rolling: {qb_res}")
    except Exception as e:
        logger.error(f"  QB rolling stats failed: {e}")
        step_failures.append(f"qb_rolling_stats: {e}")

    # Report the REAL outcome to task_runs
    if step_failures:
        joined = "; ".join(step_failures)
        logger.error(f"\n❌ NFL stats refresh finished WITH ERRORS:\n  {joined}")
        await _report_task_outcome("nfl-stats-refresh", success=False, error=joined, started_at=started_at)
    else:
        logger.info(f"\n✅ NFL stats refresh complete!")
        await _report_task_outcome("nfl-stats-refresh", success=True, started_at=started_at)


@router.post("/ingest/nba/stats/refresh")
async def ingest_nba_stats_refresh():
    """
    Refresh NBA stats (fire-and-forget background task).

    Updates the four NBA statistical tables for the current season:
      - nba.games                 (ESPN schedule; current season only, idempotent)
      - nba.player_game_stats     (NBA Stats API per-game boxscores)
      - nba.cumulative_game_stats (incremental; sync script on worker thread)
      - nba.team_rolling_stats    (sync script on worker thread)

    Same pattern as MLB/NFL: fire-and-forget dispatch, real success/failure
    reported to task_runs so the Tasks UI reflects the actual background outcome.
    """
    import asyncio
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc)

    async def _run_reported():
        try:
            await _run_nba_stats_refresh(started_at)
        except Exception as e:
            import traceback
            logger.error(f"NBA stats refresh CRASHED: {e}\n{traceback.format_exc()}")
            await _report_task_outcome("nba-stats-refresh", success=False, error=f"{type(e).__name__}: {e}", started_at=started_at)

    asyncio.create_task(_run_reported())
    return {"status": "started", "message": "NBA stats refresh running in background. Check API logs for progress."}


async def _run_nba_stats_refresh(started_at=None):
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

    season = date.today().year

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

    # Step 3: nba.cumulative_game_stats — sync script on worker thread
    logger.info("[Step 3] Refreshing nba.cumulative_game_stats...")
    try:
        from app.db_urls import PSYCOPG2_DATABASE_URL
        from app.handicapping.nba.cumulative_stats import populate_cumulative_stats
        cum_result = await _run_in_thread(populate_cumulative_stats, PSYCOPG2_DATABASE_URL, [season])
        logger.info(f"  cumulative_game_stats: {cum_result}")
    except Exception as e:
        logger.error(f"  cumulative_game_stats failed: {e}")
        step_failures.append(f"cumulative_game_stats: {e}")

    # Step 4: nba.team_rolling_stats — sync script on worker thread
    logger.info("[Step 4] Refreshing nba.team_rolling_stats...")
    try:
        from app.database import engine as sync_engine
        from app.handicapping.nba.populate_team_rolling_stats import populate_team_rolling
        roll_result = await _run_in_thread(populate_team_rolling, sync_engine, True)
        logger.info(f"  team_rolling_stats: {roll_result}")
    except Exception as e:
        logger.error(f"  team_rolling_stats failed: {e}")
        step_failures.append(f"team_rolling_stats: {e}")

    # Report the REAL outcome to task_runs
    if step_failures:
        joined = "; ".join(step_failures)
        logger.error(f"\n❌ NBA stats refresh finished WITH ERRORS:\n  {joined}")
        await _report_task_outcome("nba-stats-refresh", success=False, error=joined, started_at=started_at)
    else:
        logger.info(f"\n✅ NBA stats refresh complete!")
        await _report_task_outcome("nba-stats-refresh", success=True, started_at=started_at)


async def _run_in_thread(func, *args, **kwargs):
    """Run a sync function on the default executor (thread pool) without blocking
    the event loop — the no-subprocess way to run the sync rolling-stats scripts
    and the sync game_stats base loader."""
    import asyncio
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: func(*args, **kwargs))


@router.post("/ingest/mlb/lines-and-picks")
async def ingest_mlb_lines_and_picks(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    db: AsyncSession = Depends(get_db),
):
    """
    Combined lines + picks refresh. Runs every ~15 min during game days.

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features ONCE, predicts ALL upcoming games,
       and saves predictions to mlb.game_predictions
    """
    import logging
    logger = logging.getLogger("earl.mlb_lines_and_picks")

    from app.ingestion.mlb_betting_lines import snapshot_mlb_opening_lines
    from app.handicapping.mlb.mlb_engine import (
        batch_predict_upcoming_games,
        CURRENT_YEAR,
    )

    if not api_key:
        from app.core.config import settings as _mlb_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _mlb_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_mlb_opening_lines(
            db=db,
            api_key=api_key,
            days_from_now=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.mlb_betting_lines_consolidate import run as consolidate_mlb
                await _run_in_thread(consolidate_mlb, set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")
                results["errors"].append(f"consolidation_failed: {exc}")
        else:
            results["consolidated"] = {"status": "ok", "note": "no_lines_to_consolidate"}

        # ── Step 3: Batch predictions ───────────────────────────────
        from sqlalchemy import text as sa_text

        # 3a – Find all future-scheduled games to generate/refresh picks
        result = await db.execute(
            sa_text("""
                SELECT g.id
                FROM mlb.games g
                JOIN mlb.betting_lines_consolidated blc ON blc.game_id = g.id
                WHERE g.status = 'SCHEDULED'
                  AND g.date > NOW()
                  AND blc.closing_spread IS NOT NULL
                  AND blc.closing_ou IS NOT NULL
                ORDER BY g.date
            """)
        )
        game_ids_needing_picks = [row[0] for row in result.fetchall()]

        # Snapshot existing picks BEFORE they are overwritten so we can detect
        # whether OU / ML / ATS changed (batch_predict deletes+reinserts source='api').
        old_picks: dict[int, dict] = {}
        if game_ids_needing_picks:
            old_res = await db.execute(
                sa_text("""
                    SELECT game_id, ou_pick, ml_pick, run_line_pick
                    FROM mlb.game_predictions
                    WHERE source = 'api'
                      AND game_id = ANY(:gids)
                """),
                {"gids": game_ids_needing_picks},
            )
            for gid, ou, ml, rl in old_res.fetchall():
                old_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "run_line_pick": rl}

        if not game_ids_needing_picks:
            results["predictions"] = {"picks_generated": 0, "note": "No future scheduled games with consolidated lines"}
        else:
            pick_results = await batch_predict_upcoming_games(
                db=db,
                game_ids=game_ids_needing_picks,
                _logger=logger,
                year=CURRENT_YEAR,
            )
            results["predictions"] = {
                "picks_generated": len([p for p in pick_results if "error" not in p]),
                "games_attempted": len(game_ids_needing_picks),
                "game_results": pick_results,
            }

            # ── Step 4: Regenerate premium writeups when a pick changed ──
            # Picks are refreshed throughout the day until game time. The morning
            # writeup uses picks as a guide, so if OU / ML / ATS changed on a game
            # that already has a premium writeup, regenerate it to stay in sync.
            regenerated: list[int] = []
            regen_failures: list[dict] = []
            if game_ids_needing_picks:
                try:
                    wu_rows = await db.execute(
                        sa_text("""
                            SELECT game_id
                            FROM mlb.game_writeups
                            WHERE game_id = ANY(:gids)
                              AND premium_content IS NOT NULL
                              AND premium_content != ''
                        """),
                        {"gids": game_ids_needing_picks},
                    )
                    games_with_premium = {r[0] for r in wu_rows.fetchall()}

                    new_res = await db.execute(
                        sa_text("""
                            SELECT game_id, ou_pick, ml_pick, run_line_pick
                            FROM mlb.game_predictions
                            WHERE source = 'api'
                              AND game_id = ANY(:gids)
                        """),
                        {"gids": game_ids_needing_picks},
                    )
                    new_picks: dict[int, dict] = {}
                    for gid, ou, ml, rl in new_res.fetchall():
                        new_picks[gid] = {"ou_pick": ou, "ml_pick": ml, "run_line_pick": rl}

                    from app.writeups.mlb.generator import MLBWriteupGenerator
                    gen = MLBWriteupGenerator()

                    # Only regenerate when a pick FLIPS SIDE — not when a margin/
                    # line just drifts. OU/ML are already side-only (Over/Under, home/away).
                    # ATS run_line_pick is "<team> <+/-val>"; side = team token only, so
                    # spread movement (e.g. +1.5 → +2.5 on the same team) does NOT fire.
                    def _ats_side(val):
                        if not val:
                            return None
                        return str(val).split()[0].strip()

                    def _pick_flipped(old_v, new_v):
                        # normalize empties; flip = different non-empty side
                        a = (old_v or "").strip()
                        b = (new_v or "").strip()
                        if a == b:
                            return False
                        return bool(a) and bool(b)

                    for gid in game_ids_needing_picks:
                        if gid not in games_with_premium:
                            continue
                        old = old_picks.get(gid)
                        new = new_picks.get(gid)
                        if old is None or new is None:
                            continue
                        flipped = (
                            _pick_flipped(old.get("ou_pick"), new.get("ou_pick"))
                            or _pick_flipped(old.get("ml_pick"), new.get("ml_pick"))
                            or _pick_flipped(
                                _ats_side(old.get("run_line_pick")),
                                _ats_side(new.get("run_line_pick")),
                            )
                        )
                        if not flipped:
                            continue
                        try:
                            writeup, _qc = await gen.generate(
                                db, gid, is_historical=False,
                                as_of_date=None, reasoning="minimal",
                            )
                            if "error" in writeup:
                                raise RuntimeError(writeup["error"])
                            regenerated.append(gid)
                            logger.info(f"Pick flipped side for game {gid} — regenerated premium writeup")
                        except Exception as exc:
                            regen_failures.append({"game_id": gid, "error": str(exc)[:200]})
                            logger.warning(f"Writeup regen failed for game {gid}: {exc}")
                except Exception as exc:
                    logger.warning(f"Writeup regeneration pass failed: {exc}")
                    regen_failures.append({"game_id": None, "error": f"pass_failed: {exc}"})

                results["writeup_regen"] = {
                    "regenerated_count": len(regenerated),
                    "regenerated_game_ids": regenerated,
                    "failures": regen_failures,
                }
            logger.info(
                f"Lines+picks: {lines_result.get('loaded', 0)} new lines, "
                f"{len(game_ids_needing_picks)} games, "
                f"{len([p for p in pick_results if 'error' not in p])} picks"
            )

    except Exception as e:
        import traceback
        results["errors"].append(str(e))
        logger.error(f"Lines+picks refresh failed: {e}\n{traceback.format_exc()}")

    return {"status": "ok", "results": results}


@router.post("/ingest/nfl/lines-and-picks")
async def ingest_nfl_lines_and_picks(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    db: AsyncSession = Depends(get_db),
):
    """
    Combined NFL lines + picks refresh. Mirrors ingest_mlb_lines_and_picks.

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features, predicts future games with both spread+OU,
       and saves predictions to nfl.game_predictions
    """
    import logging
    logger = logging.getLogger("earl.nfl_lines_and_picks")

    from app.ingestion.nfl_betting_lines import snapshot_nfl_opening_lines
    from app.handicapping.nfl.engine import (batch_predict_upcoming_games, CURRENT_NFL_YEAR)

    if not api_key:
        from app.core.config import settings as _nfl_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _nfl_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_nfl_opening_lines(
            db=db,
            api_key=api_key,
            days=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.nfl_betting_lines_consolidate import run as consolidate_nfl
                await _run_in_thread(consolidate_nfl, set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")

        # ── Step 3: Predict future games with both spread + OU set ──
        from datetime import datetime, timezone
        from sqlalchemy import text
        predict_rows = (
            await db.execute(
                text("""
                    SELECT DISTINCT blc.game_id
                    FROM nfl.betting_lines_consolidated blc
                    JOIN nfl.games g ON g.id = blc.game_id
                    WHERE g.date > NOW()
                      AND g.status = 'SCHEDULED'
                      AND blc.closing_spread IS NOT NULL
                      AND blc.closing_ou IS NOT NULL
                """)
            )
        ).fetchall()
        game_ids = [r[0] for r in predict_rows]
        logger.info(f"NFL: {len(game_ids)} games have consolidated lines")

        if game_ids:
            # Use the engine's resolved live model year (max trained year), NOT the
            # calendar year — no NFL model exists for the upcoming season until it's
            # trained. Using calendar year silently left models unloaded.
            year = CURRENT_NFL_YEAR
            pick_results = await batch_predict_upcoming_games(
                game_ids=game_ids,
                year=year,
                db=db,
            )
            results["predictions"] = {"games": len(pick_results)}
        else:
            results["predictions"] = {"games": 0, "skipped": "no games with lines"}

    except Exception:
        import traceback
        logger.error(f"NFL lines+picks refresh failed: {traceback.format_exc()}")

    return {"status": "ok", "results": results}


@router.post("/ingest/nba/lines-and-picks")
async def ingest_nba_lines_and_picks(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    db: AsyncSession = Depends(get_db),
):
    """
    Combined NBA lines + picks refresh. Mirrors ingest_mlb_lines_and_picks.

    1. Fetches current odds from The Odds API
    2. Runs incremental consolidation
    3. Batch-loads model & features, predicts future games with both spread+OU,
       and saves predictions to nba.game_predictions
    """
    import logging
    logger = logging.getLogger("earl.nba_lines_and_picks")

    from app.ingestion.nba_betting_lines import snapshot_nba_opening_lines
    from app.handicapping.nba.nba_engine import (
        batch_predict_upcoming_games,
    )

    if not api_key:
        from app.core.config import settings as _nba_settings
        api_key = os.environ.get("ODDS_API_KEY", "") or _nba_settings.odds_api_key

    results = {"lines": None, "consolidated": None, "predictions": None, "errors": []}

    if not api_key:
        return {"status": "error", "message": "No API key"}

    try:
        # ── Step 1: Fetch lines ──────────────────────────────────────
        lines_result = await snapshot_nba_opening_lines(
            db=db,
            api_key=api_key,
            days=3,
        )
        results["lines"] = lines_result
        updated_game_ids = lines_result.get("updated_game_ids", [])

        # ── Step 2: Consolidate ──────────────────────────────────────
        if updated_game_ids:
            try:
                from app.ingestion.nba_odds_consolidated import run as consolidate_nba
                await _run_in_thread(consolidate_nba, set(updated_game_ids))
                results["consolidated"] = {"status": "ok", "games": len(updated_game_ids)}
            except Exception as exc:
                logger.error(f"Consolidation failed: {exc}")

        # ── Step 3: Predict future games with both spread + OU set ──
        from datetime import datetime, timezone
        from sqlalchemy import text
        predict_rows = (
            await db.execute(
                text("""
                    SELECT DISTINCT blc.game_id
                    FROM nba.betting_lines_consolidated blc
                    JOIN nba.games g ON g.id = blc.game_id
                    WHERE g.date > NOW()
                      AND g.status = 'SCHEDULED'
                      AND blc.closing_spread IS NOT NULL
                      AND blc.closing_ou IS NOT NULL
                """)
            )
        ).fetchall()
        game_ids = [r[0] for r in predict_rows]
        logger.info(f"NBA: {len(game_ids)} games have consolidated lines")

        if game_ids:
            year = datetime.now(timezone.utc).year
            pick_results = await batch_predict_upcoming_games(
                game_ids=game_ids,
                year=year,
                db=db,
            )
            results["predictions"] = {"games": len(pick_results)}
        else:
            results["predictions"] = {"games": 0, "skipped": "no games with lines"}

    except Exception:
        import traceback
        logger.error(f"NBA lines+picks refresh failed: {traceback.format_exc()}")

    return {"status": "ok", "results": results}


@router.post("/ingest/weather-update")
async def ingest_weather_update(
    db: AsyncSession = Depends(get_db),
):
    """
    Combined weather update for all sports.

    Fetches NWS weather forecasts for upcoming SCHEDULED games across MLB and NFL.
    Always overwrites existing weather data with latest forecast.
    Only processes SCHEDULED games — never touches started or completed games.

    Intended for daily cron at 6:03 AM CT.
    """
    import logging
    logger = logging.getLogger("earl.weather_update_route")
    import traceback

    results = {}

    for sport in ["mlb", "nfl"]:
        try:
            if sport == "mlb":
                from app.ingestion.mlb_weather_forecast import main as run
            else:
                from app.ingestion.nfl_weather_forecast import main as run

            await run(force_refresh=True)
            results[sport] = "ok"
            logger.info(f"Weather update OK for {sport}")
        except Exception as e:
            results[sport] = f"error: {e}"
            logger.error(f"Weather update failed for {sport}: {e}\n{traceback.format_exc()}")

    all_ok = all(v == "ok" for v in results.values())
    return {"status": "ok" if all_ok else "partial", "results": results}


@router.post("/ingest/fd-scraper")
async def run_fd_scraper():
    """
    Run the FanDuel daily sportsbook scraper.

    Scrapes team props (championship odds, win totals), player season props
    (awards), and player daily props from FanDuel. Saves to mlb/nfl/nba
    team_props, player_season_props, player_daily_props tables.

    Intended for daily cron at 6:00 AM CT.
    """
    import logging
    logger = logging.getLogger("earl.fd_scraper_route")

    try:
        from app.scrapers.daily_run import run_daily_scrape
        stats = await run_daily_scrape()
        logger.info(f"FD scraper complete: {stats}")
        return {"status": "ok", "stats": stats}
    except Exception as e:
        logger.error(f"FD scraper failed: {e}")
        return {"status": "error", "error": str(e)}


@router.post("/ingest/betmgm-scraper")
async def run_betmgm_scraper():
    """
    Run the BetMGM season-prop / futures scraper.

    Fetches BetMGM's CDS futures fixtures for mlb/nfl/nba and saves:
      - player season props (awards: MVP, Cy Young, ROY, DPOY, etc.)
        to {sport}.player_season_props
      - team props (championship odds, make/miss playoffs, win totals)
        to {sport}.team_props
    Bookmaker is 'betmgm' (additive to the FanDuel / the-odds-api rows).
    Rendered as a subprocess by the task scheduler (like the FD scraper) so it
    does not block a granian worker. Intended for daily cron.
    """
    import logging
    logger = logging.getLogger("earl.betmgm_scraper_route")

    try:
        from app.scrapers.betmgm_run import run_betmgm_scrape
        stats = await run_betmgm_scrape()
        logger.info(f"BetMGM scraper complete: {stats}")
        return {"status": "ok", "stats": stats}
    except Exception as e:
        logger.error(f"BetMGM scraper failed: {e}")
        return {"status": "error", "error": str(e)}


