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
from app.ingestion.espn import ingest_espn_schedule
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

async def _run_mlb_stats_refresh():
    """Run MLB stats refresh in background.

    7:30 AM run: full batting/pitching stats + games + pitchers + lineups
    Subsequent 30-min runs: only pitchers + lineups + check pitcher changes
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
    current_hour = datetime.now().hour
    is_morning_run = current_hour < 10  # 7-9 AM = full stats

    async with async_session() as db:
        logger.info("=" * 60)
        label = "Full Refresh" if is_morning_run else "Quick Refresh (lineups + pitchers)"
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

        if is_morning_run:
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
        else:
            logger.info("[Skipping] Full stats refresh — morning-only")

        # Step 4: Active roster sync (always run)
        logger.info("[Step 4] Syncing active 40-man rosters from MLB Stats API...")
        try:
            roster_result = await sync_all_team_rosters(db, team_map)
            summary = roster_result.get("_summary", {})
            logger.info(f"  Active: {summary.get('total_active', 0)}, IL: {summary.get('total_injured', 0)}")
        except Exception as e:
            logger.error(f"  Roster sync failed: {e}")

        # Step 5: Game status updates (always run)
        logger.info("[Step 5] Updating game statuses from MLB Stats API...")
        status_result = await update_game_statuses(db)
        logger.info(f"  Status changes: {len(status_result.get('status_changes', {}))}, rescheduled: {status_result.get('rescheduled', 0)}")

        # Step 6: Probable pitchers (always run)
        logger.info("[Step 6] Updating probable pitchers for upcoming games...")
        pitcher_result = await update_probable_pitchers(db)
        pitchers_changed = pitcher_result.get('games_updated', 0)
        pitcher_changed_ids = pitcher_result.get('updated_game_ids', [])
        logger.info(f"  Probable pitchers updated: {pitchers_changed}")

        # Step 7: Starting lineups (always run)
        logger.info("[Step 7] Fetching starting lineups...")
        all_changed_ids = list(pitcher_changed_ids)
        from datetime import date
        try:
            from app.ingestion.mlb_lineups import update_lineups_for_date
            today = date.today()
            lineup_result = await update_lineups_for_date(db, today)
            logger.info(f"  Lineups: {lineup_result.get('lineups_saved', 0)} saved, {lineup_result.get('pitchers_updated', 0)} pitchers updated")
            pitchers_changed += lineup_result.get('pitchers_updated', 0)
            for gid in lineup_result.get('updated_game_ids', []):
                if gid not in all_changed_ids:
                    all_changed_ids.append(gid)
        except Exception as e:
            logger.error(f"  Lineups fetch failed: {e}")

        # Step 7b: Regenerate pick cards for games where pitcher changed
        if all_changed_ids:
            logger.info(f"[Step 7b] {len(all_changed_ids)} games had pitcher changes — regenerating pick cards...")
            try:
                from app.handicapping.mlb.mlb_engine import batch_predict_upcoming_games
                year = date.today().year
                pick_results = await batch_predict_upcoming_games(
                    db=db,
                    game_ids=all_changed_ids,
                    _logger=logger,
                    year=year,
                )
                regenerated = len([p for p in pick_results if 'error' not in p])
                logger.info(f"  Pick cards regenerated: {regenerated}/{len(all_changed_ids)}")
            except Exception as e:
                import traceback
                logger.error(f"  Pick card regeneration failed: {e}\n{traceback.format_exc()}")
        else:
            logger.info("[Step 7b] No pitcher changes — picks unchanged")

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
                update_prediction_results,
            )
            try:
                boxscore_result = await refresh_boxscores_for_recent_games(pconn)
                logger.info(f"  Boxscores: {boxscore_result['games_processed']} games, "
                            f"{boxscore_result['batting_rows']} batting rows, "
                            f"{boxscore_result['pitching_rows']} pitching rows, "
                            f"{boxscore_result.get('weather_updated', 0)} weather updates")
            except Exception as e:
                logger.error(f"  Boxscore loading failed: {e}")

            # Step 9: Update prediction results for completed games
            # (runs independently of boxscore loading)
            try:
                pred_updated = await update_prediction_results(pconn)
                logger.info(f"  Step 9: Updated {pred_updated} predictions with actual results")
            except Exception as e:
                logger.error(f"  Step 9 prediction result update failed: {e}")
        except Exception as e:
            logger.error(f"  Outer boxscore/prediction block failed: {e}")

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

        await pconn.close()

        # Commit all changes (lineups, pitchers, picks)
        try:
            await db.commit()
        except Exception as e:
            logger.error(f"Final commit failed: {e}")

        logger.info(f"\n✅ MLB stats {label} complete!")


@router.post("/ingest/mlb/stats/refresh")
async def ingest_mlb_stats_refresh():
    """
    Refresh MLB player stats from statsapi.mlb.com.

    Morning (7-9AM): full batting/pitching stats refresh + pitchers + lineups
    Daytime (12-10PM every 30min): pitchers + lineups only (quick)
    When pitchers change, regenerates pick cards.
    """
    import asyncio
    asyncio.create_task(_run_mlb_stats_refresh())
    return {"status": "started", "message": "MLB stats refresh running in background. Check API logs for progress."}


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
                consolidate_mlb(game_ids_filter=set(updated_game_ids))
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


