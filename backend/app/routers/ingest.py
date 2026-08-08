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


