"""
Data ingestion endpoints for EarlKnowsBall.
Trigger these to populate the database from various sources.
"""
import os
import sys
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException, BackgroundTasks
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from app.ingestion.pipeline import run_full_ingestion, ingest_sleeper_players
from app.ingestion.espn import ingest_espn_schedule
from app.ingestion.nflverse import ingest_nflverse_stats
from app.ingestion.match_players import match_nflverse_ids
from app.ingestion.articles import scrape_rss_feeds
from app.ingestion.articles_mlb import scrape_rss_feeds_mlb
from app.ingestion.articles_fangraphs import scrape_fangraphs_all
from app.ingestion.articles_nba import scrape_rss_feeds_nba
from app.ingestion.articles_hoopsrumors import scrape_hoopsrumors_all
from app.ingestion.historical import generate_all_seasons
from app.ingestion.depth_charts import scrape_team_depth_chart, scrape_all_teams
from app.ingestion.historical_games import ingest_historical_games as _ingest_historical_games
from app.ingestion.sbnation_archives import scrape_all_blogs, scrape_blog_archives, SBNATION_BLOGS, NBA_BLOGS, MLB_BLOGS
from app.ingestion.player_profiles import generate_all_profiles, generate_profile_for_player
from app.ingestion.national_archives import scrape_source, scrape_all_sources, NATIONAL_SOURCES
from app.ingestion.pft_archives import scrape_from_sitemaps, scrape_latest_rss as scrape_pft_rss
from app.ingestion.nflverse_data import ingest_draft_info, ingest_injuries, ingest_trades
from app.ingestion.betting_lines import ingest_historical_lines, ingest_current_lines, snapshot_opening_lines
try:
    from app.ingestion.mlb_betting_lines import ingest_historical_mlb_lines, ingest_current_mlb_lines, snapshot_mlb_opening_lines, ingest_historical_sbr_mlb_lines, ingest_historical_odds_api_mlb_lines
except ImportError:
    ingest_historical_mlb_lines = ingest_current_mlb_lines = snapshot_mlb_opening_lines = ingest_historical_sbr_mlb_lines = ingest_historical_odds_api_mlb_lines = None
from app.ingestion.espn_nba import ingest_nba_schedule, ingest_nba_all_seasons, ingest_nba_games
from app.database import async_session
from app.ingestion.dfs_salaries import scrape_draftkings, scrape_fanduel, scrape_all_dfs
from app.ingestion.nba_betting_lines import fetch_current_lines, snapshot_nba_opening_lines
from app.ingestion.nfl_pace import ingest_pace_data

router = APIRouter()


@router.post("/ingest/sleeper-players")
async def ingest_players(db: AsyncSession = Depends(get_db)):
    count = await ingest_sleeper_players(db)
    return {"status": "ok", "source": "sleeper", "players_loaded": count}


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


@router.post("/ingest/nflverse-stats")
async def ingest_stats(
    season: int = Query(2025, description="Season year"),
    db: AsyncSession = Depends(get_db),
):
    result = await ingest_nflverse_stats(db, season_year=season)
    return {"status": "ok", "source": "nflverse", **result}


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
        results.append(result)
        print(f"[Earl] {year}: {result['stats_loaded']} stats")
    return {"status": "ok", "source": "nflverse", "seasons": results}


@router.post("/ingest/full")
async def ingest_all(db: AsyncSession = Depends(get_db)):
    result = await run_full_ingestion(db)
    return {"status": "ok", **result}


# ── PFT Archive Scraping ──────────────────────────────────────────────────


@router.post("/ingest/articles/pft/latest")
async def ingest_pft_latest(
    max_articles: int = Query(50, description="Max articles from RSS"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape the most recent ProFootballTalk articles from their RSS feed."""
    result = await scrape_pft_rss(db, max_articles=max_articles)
    return {"status": "ok", **result}


@router.post("/ingest/articles/pft/archives")
async def ingest_pft_archives(
    start_month: str | None = Query(None, description="Earliest sitemap month YYYYMM to process"),
    max_sitemaps: int = Query(3, description="Number of monthly sitemaps to process"),
    max_per_sitemap: int | None = Query(None, description="Max articles per sitemap"),
    delay: float = Query(0.5, description="Seconds between requests"),
    db: AsyncSession = Depends(get_db),
):
    """
    Scrape ProFootballTalk articles from NBC Sports sitemaps, newest first.

    Each monthly sitemap has ~1,750 PFT articles. Processes them in reverse
    chronological order (newest month first).
    """
    result = await scrape_from_sitemaps(
        db=db,
        start_month=start_month,
        max_sitemaps=max_sitemaps,
        max_per_sitemap=max_per_sitemap,
        delay=delay,
    )
    return {"status": "ok", **result}


# ── Article ingestion ────────────────────────────────────────────────────

class ManualArticleRequest(BaseModel):
    title: str
    body: str
    category: str = "general"
    excerpt: str | None = None
    author: str | None = None
    source_name: str | None = None
    source_url: str | None = None
    tier: str = "free"


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


@router.post("/ingest/articles/mlb/fangraphs")
async def ingest_fangraphs_historical(
    start_year: int = Query(2026, description="First year to scrape (newest first)"),
    end_year: int = Query(2010, description="Last year"),
    delay: float = Query(0.75, description="Seconds between requests"),
    max_articles: int | None = Query(None, description="Max total articles"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape historical Fangraphs articles from monthly RSS feeds.
    Crawls newest to oldest, paginating each month until empty.
    """
    result = await scrape_fangraphs_all(
        db=db,
        start_year=start_year,
        end_year=end_year,
        delay=delay,
        max_articles=max_articles,
    )
    return {"status": "ok", **result}


@router.post("/ingest/nba/games")
async def nba_games_route(
    season: int = Query(2025, description="Season year (e.g. 2025 = 2025-26 season)"),
):
    """Load NBA games (all types: preseason, regular, playoffs) from ESPN for a single season.
    NOTE: ESPN's NBA API ignores the seasontype filter. All game types within the season
    date range are loaded and tagged with their actual type from the event data.
    """
    result = await ingest_nba_games(season_year=season, db_session=async_session)
    return {"status": "ok", "source": "espn_nba", **result}


@router.post("/ingest/nba/games/all")
async def ingest_nba_games_all(
    start_year: int = Query(2006, description="Start season"),
    end_year: int = Query(2026, description="End season (inclusive)"),
    db: AsyncSession = Depends(get_db),
):
    """Load all NBA games from ESPN for all seasons (2006-2026).
    Loads all game types (preseason, regular season, playoffs) for each season.
    """
    result = await ingest_nba_all_seasons(db, start_year=start_year, end_year=end_year)
    return {"status": "ok", "source": "espn_nba", **result}

@router.post("/ingest/nba/betting-lines/current")
async def ingest_nba_current_lines(
    api_key: str = Query("", description="The Odds API key (or set ODDS_API_KEY in .env)"),
    db: AsyncSession = Depends(get_db),
):
    """Fetch current NBA lines from The Odds API."""
    from app.ingestion.nba_betting_lines import fetch_current_lines
    result = await fetch_current_lines(db, api_key=api_key)
    return {"status": "ok", "source": "the_odds_api", **result}


@router.post("/ingest/nba/betting-lines/opening")
async def ingest_nba_opening_lines(
    api_key: str = Query("", description="The Odds API key"),
    db: AsyncSession = Depends(get_db),
):

    """Snapshot NBA opening lines from The Odds API (deduplicated by game)."""
    from app.ingestion.nba_betting_lines import snapshot_nba_opening_lines
    result = await snapshot_nba_opening_lines(db, api_key=api_key)
    return {"status": "ok", "source": "the_odds_api_opening", **result}

@router.post("/ingest/nba/dfs")
async def ingest_nba_dfs(
    platform: str = Query("all", description="draftkings, fanduel, or all"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape NBA DFS salaries from DraftKings and/or FanDuel."""
    from app.ingestion.nba_dfs_salaries import scrape_draftkings_nba, scrape_fanduel_nba
    
    if platform == "draftkings":
        result = await scrape_draftkings_nba(db)
    elif platform == "fanduel":
        result = await scrape_fanduel_nba(db)
    else:
        dk = await scrape_draftkings_nba(db)
        fd = await scrape_fanduel_nba(db)
        result = {"draftkings": dk, "fanduel": fd}
    return {"status": "ok", **result}


@router.post("/ingest/articles/nba/rss")

@router.post("/ingest/nba/players")
async def ingest_nba_players(
    db: AsyncSession = Depends(get_db),
):
    """Fetch NBA player bios (height, weight, college, headshot, etc.) from ESPN team rosters."""
    from app.ingestion.nba_players import ingest_rosters
    result = await ingest_rosters(db)
    return {"status": "ok", **result}
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


@router.post("/ingest/articles/nba/hoopsrumors")
async def ingest_nba_hoopsrumors(
    max_pages: int | None = Query(None, description="Max pages to crawl (15 articles/page)"),
    max_articles: int | None = Query(None, description="Max total articles"),
    delay: float = Query(0.75, description="Seconds between requests"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape historical NBA articles from HoopsRumors into nba.articles."""
    result = await scrape_hoopsrumors_all(
        db,
        max_pages=max_pages,
        max_articles=max_articles,
        delay=delay,
    )
    return {"status": "ok", **result}


@router.post("/ingest/articles/manual")
async def ingest_manual_article(
    req: ManualArticleRequest,
    db: AsyncSession = Depends(get_db),
):
    """Add a single article manually to the database."""
    article = await add_article_manually(
        db=db,
        title=req.title,
        body=req.body,
        category=req.category,
        excerpt=req.excerpt,
        author=req.author,
        source_name=req.source_name,
        source_url=req.source_url,
        tier=req.tier,
    )
    return {"status": "ok", "article_id": article.id, "title": article.title}



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

@router.post("/ingest/historical-games")
async def ingest_historical_games_route(db: AsyncSession = Depends(get_db)):
    """Load historical game schedules+results from nflverse (2005-present)."""
    result = await _ingest_historical_games(db)
    return {"status": "ok", **result}


# ── NFLVerse Data Sources (Draft, Injuries, Trades) ─────────────────────

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


@router.post("/ingest/betting-lines/historical")
async def ingest_historical_betting_lines(
    start_year: int = Query(2005, description="First season year"),
    end_year: int | None = Query(None, description="Last season year (default: current)"),
    source: str = Query("nflverse", description="Data source label"),
    db: AsyncSession = Depends(get_db),
):
    """
    Load historical betting lines from nflverse games.csv.

    Matches games by (season, week, home_team, away_team) with historic
    abbreviation mapping (LA→LAR, SD→LAC, OAK→LV, STL→LAR).
    """
    result = await ingest_historical_lines(
        db=db,
        start_year=start_year,
        end_year=end_year,
        source_name=source,
    )
    return {"status": "ok", **result}


@router.post("/ingest/betting-lines/current")
async def ingest_current_betting_lines(
    api_key: str = Query("", description="The Odds API key (free at https://the-odds-api.com). Falls back to ODDS_API_KEY env var."),
    days_from_now: int = Query(14, description="Look ahead this many days for upcoming games"),
    source: str = Query("the_odds_api", description="Data source label"),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch current betting lines from The Odds API for upcoming NFL games.

    Requires a free API key from https://the-odds-api.com
    Matches games to our DB by full team name → abbreviation.
    Falls back to ODDS_API_KEY environment variable if no key provided.
    """
    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")
    result = await ingest_current_lines(
        db=db,
        api_key=api_key,
        source_name=source,
        days_from_now=days_from_now,
    )
    return {"status": "ok", **result}


# ── Opening Lines Snapshot ────────────────────────────────────────────────


@router.post("/ingest/opening-lines/snapshot")
async def ingest_opening_lines_snapshot(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    days_from_now: int = Query(14, description="Look ahead from today"),
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot opening lines from The Odds API for upcoming NFL games.

    Run this early in the week (Tuesday/Wednesday) after lines are first posted.
    Saves with source='the_odds_api_opening' to distinguish from later snapshots.
    Only saves lines for games that don't already have an opening line saved.
    """
    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"status": "error", "detail": "No API key provided. Get one free at https://the-odds-api.com/"}
    result = await snapshot_opening_lines(
        db=db,
        api_key=api_key,
        days_from_now=days_from_now,
    )
    return {"status": "ok", **result}


# ── DFS Salaries ─────────────────────────────────────────────────────────


@router.post("/ingest/dfs/draftkings")
async def ingest_dk_salaries(
    clear_existing: bool = Query(False, description="Clear existing DK salaries before loading"),
    test_mode: bool = Query(False, description="Load sample historical data instead of live scrape"),
    db: AsyncSession = Depends(get_db),
):
    """
    Scrape player salaries from DraftKings.

    During NFL season, finds active contests and pulls real salaries.
    Off-season (May-July): no NFL contests available — use test_mode=True.
    """
    result = await scrape_draftkings(
        db=db,
        clear_existing=clear_existing,
        test_mode=test_mode,
    )
    return {"status": "ok", **result}


@router.post("/ingest/dfs/fanduel")
async def ingest_fd_salaries(
    clear_existing: bool = Query(False, description="Clear existing FD salaries before loading"),
    test_mode: bool = Query(False, description="Load sample historical data instead of live scrape"),
    db: AsyncSession = Depends(get_db),
):
    """
    Scrape player salaries from FanDuel.

    During NFL season, finds active contests and pulls real salaries.
    Off-season (May-July): no NFL contests available — use test_mode=True.
    """
    result = await scrape_fanduel(
        db=db,
        clear_existing=clear_existing,
        test_mode=test_mode,
    )
    return {"status": "ok", **result}


@router.post("/ingest/dfs/all")
async def ingest_all_dfs(
    clear_existing: bool = Query(False, description="Clear existing salaries before loading"),
    test_mode: bool = Query(False, description="Load sample historical data"),
    db: AsyncSession = Depends(get_db),
):
    """
    Scrape player salaries from all DFS platforms (DraftKings + FanDuel).
    """
    result = await scrape_all_dfs(
        db=db,
        clear_existing=clear_existing,
        test_mode=test_mode,
    )
    return {"status": "ok", **result}


# ── Depth Charts ────────────────────────────────────────────────────────

@router.post("/ingest/depth-charts/team")
async def ingest_team_depth_chart(
    team: str = Query(..., description="Team abbreviation, e.g. HOU"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape depth chart for a single team from Ourlads."""
    result = await scrape_team_depth_chart(db, team)
    return {"status": "ok", **result}


@router.post("/ingest/depth-charts/all")
async def ingest_all_depth_charts(db: AsyncSession = Depends(get_db)):
    """Scrape depth charts for all 32 NFL teams from Ourlads."""
    result = await scrape_all_teams(db)
    return {"status": "ok", **result}


# ── National News Scraping ────────────────────────────────────────────────

@router.post("/ingest/articles/national")
async def ingest_national_source(
    source: str = Query(..., description="Source key, e.g. lastwordonsports"),
    max_articles: int | None = Query(None, description="Max articles to scrape"),
    delay: float = Query(1.0, description="Seconds between requests"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape a national NFL news source from newest to oldest."""
    if source not in NATIONAL_SOURCES:
        available = ", ".join(NATIONAL_SOURCES.keys())
        raise HTTPException(status_code=400, detail=f"Unknown source '{source}'. Available: {available}")
    result = await scrape_source(db=db, source_key=source, max_articles=max_articles, delay=delay)
    return {"status": "ok", **result}


@router.post("/ingest/articles/national/all")
async def ingest_national_all(
    max_per_source: int | None = Query(None, description="Max articles per source"),
    delay: float = Query(1.0, description="Seconds between requests"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape all national NFL news sources."""
    result = await scrape_all_sources(db=db, max_per_source=max_per_source, delay=delay)
    return {"status": "ok", **result}


# ── Player Profiles ──────────────────────────────────────────────────────


@router.post("/ingest/player-profiles/all")
async def ingest_all_profiles(
    position: str | None = Query(None, description="Filter by position (QB, RB, WR, TE, K)"),
    limit: int | None = Query(None, description="Max players to process"),
    db: AsyncSession = Depends(get_db),
):
    """Generate profiles for all (or filtered) players."""
    result = await generate_all_profiles(
        db=db,
        position_filter=position,
        limit=limit,
    )
    return {"status": "ok", **result}


@router.post("/ingest/player-profiles/player")
async def ingest_single_profile(
    player_id: int = Query(..., description="Player ID"),
    db: AsyncSession = Depends(get_db),
):
    """Generate a profile for a single player."""
    result = await generate_profile_for_player(
        db=db,
        player_id=player_id,
    )
    return {"status": "ok", **result}


# ── SB Nation Archive Scraping ────────────────────────────────────────────


@router.post("/ingest/articles/sbnation/blog")
async def ingest_sbnation_blog(
    blog: str = Query(..., description="SB Nation blog domain, e.g. acmepackingcompany"),
    start_year: int = Query(2022, description="First year to scrape"),
    end_year: int | None = Query(None, description="Last year (default: current year)"),
    max_articles: int | None = Query(None, description="Max articles for this blog"),
    delay: float = Query(1.5, description="Seconds between requests"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape archives for a single SB Nation team blog."""
    if blog not in SBNATION_BLOGS:
        available = ", ".join(sorted(SBNATION_BLOGS.keys()))
        raise HTTPException(
            status_code=400,
            detail=f"Unknown blog '{blog}'. Available: {available}",
        )
    result = await scrape_blog_archives(
        db=db,
        blog_domain=blog,
        start_year=start_year,
        end_year=end_year,
        delay=delay,
        max_articles=max_articles,
    )
    return {"status": "ok", **result}


@router.post("/ingest/articles/sbnation/all")
async def ingest_sbnation_all(
    start_year: int = Query(2022, description="First year to scrape"),
    end_year: int | None = Query(None, description="Last year (default: current year)"),
    max_per_blog: int | None = Query(None, description="Max articles per blog"),
    delay: float = Query(1.5, description="Seconds between requests"),
    blog_filter: str | None = Query(None, description="Comma-separated blog domains to scrape (omit for all 32)"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape archives for all (or filtered) SB Nation team blogs."""
    filter_list = blog_filter.split(",") if blog_filter else None
    result = await scrape_all_blogs(
        db=db,
        start_year=start_year,
        end_year=end_year,
        delay=delay,
        max_per_blog=max_per_blog,
        blog_filter=filter_list,
    )
    return {"status": "ok", **result}


@router.post("/ingest/articles/sbnation/nba/all")
async def ingest_sbnation_nba_all(
    start_year: int = Query(2026, description="First year to scrape"),
    end_year: int | None = Query(None, description="Last year (default: current year)"),
    max_per_blog: int | None = Query(None, description="Max articles per blog"),
    delay: float = Query(0.75, description="Seconds between requests"),
    blog_filter: str | None = Query(None, description="Comma-separated blog domains (omit for all 30)"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape archives for all (or filtered) SB Nation NBA team blogs."""
    filter_list = blog_filter.split(",") if blog_filter else None
    result = await scrape_all_blogs(
        db=db,
        start_year=start_year,
        end_year=end_year,
        delay=delay,
        max_per_blog=max_per_blog,
        blog_filter=filter_list,
        blogs=NBA_BLOGS,
    )
    return {"status": "ok", **result}


@router.post("/ingest/articles/sbnation/mlb/all")
async def ingest_sbnation_mlb_all(
    start_year: int = Query(2026, description="First year to scrape"),
    end_year: int | None = Query(None, description="Last year (default: current year)"),
    max_per_blog: int | None = Query(None, description="Max articles per blog"),
    delay: float = Query(0.75, description="Seconds between requests"),
    blog_filter: str | None = Query(None, description="Comma-separated blog domains (omit for all 30)"),
    db: AsyncSession = Depends(get_db),
):
    """Scrape archives for all (or filtered) SB Nation MLB team blogs."""
    filter_list = blog_filter.split(",") if blog_filter else None
    result = await scrape_all_blogs(
        db=db,
        start_year=start_year,
        end_year=end_year,
        delay=delay,
        max_per_blog=max_per_blog,
        blog_filter=filter_list,
        blogs=MLB_BLOGS,
    )
    return {"status": "ok", **result}


# ── MLB Betting Lines ──────────────────────────────────────────────────────


@router.post("/ingest/mlb/betting-lines/historical/github")
async def ingest_mlb_historical_github_betting_lines(
    start_date: str | None = Query(None, description="Start date YYYY-MM-DD (dataset: 2021-04-01)"),
    end_date: str | None = Query(None, description="End date YYYY-MM-DD (dataset: 2025-08-16)"),
    source: str = Query("mlb_odds_dataset", description="Data source label"),
    sportsbook: str = Query("fanduel", description="Preferred sportsbook (fanduel, draftkings, betmgm, etc)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Load historical MLB betting lines from the free GitHub dataset (2021-2025).

    Downloads a 76 MB JSON file with opening + closing lines from multiple
    sportsbooks (FanDuel, DraftKings, BetMGM, Bet365, Caesars, BetRivers).
    """
    result = await ingest_historical_mlb_lines(
        db=db,
        start_date=start_date,
        end_date=end_date,
        source_name=source,
        preferred_sportsbook=sportsbook,
    )
    return {"status": "ok", **result}


@router.post("/ingest/mlb/betting-lines/historical/sbr")
async def ingest_mlb_historical_sbr_betting_lines(
    start_year: int = Query(2011, description="First season (min: 2011)"),
    end_year: int = Query(2021, description="Last season (max: 2021)"),
    source: str = Query("sbr_historical", description="Data source label"),
    db: AsyncSession = Depends(get_db),
):
    """
    Load historical MLB betting lines from SportsbookReview 10Y dataset (2011-2021).

    Downloads an 18 MB JSON file with opening/closing moneylines and totals
    from sportsbookreview.com for MLB seasons 2011-2021.
    """
    result = await ingest_historical_sbr_mlb_lines(
        db=db,
        start_year=start_year,
        end_year=end_year,
        source_name=source,
    )
    return {"status": "ok", **result}


@router.post("/ingest/mlb/betting-lines/historical")
async def ingest_mlb_historical_betting_lines(
    start_year: int = Query(2011, description="First year (SBR: 2011-2021, GitHub: 2021-2025)"),
    end_year: int | None = Query(None, description="Last year (default: current)"),
    sportsbook: str = Query("fanduel", description="Preferred sportsbook for GitHub dataset"),
    db: AsyncSession = Depends(get_db),
):
    """
    Load ALL historical MLB betting lines.

    Combines two datasets:
      - SBR 10Y (2011-2021): opening/closing ML + totals
      - GitHub (2021-2025): opening/closing ML + spread + totals (per sportsbook)

    This endpoint runs both datasets. Use the /github and /sbr sub-endpoints
    for more granular control.
    """
    from datetime import datetime as dt_module
    now = dt_module.now(timezone.utc)
    if end_year is None:
        end_year = now.year

    results = {}

    # SBR dataset (2011-2021)
    if start_year <= 2021:
        sbr_end = min(end_year, 2021)
        sbr_start = max(start_year, 2011)
        if sbr_start <= sbr_end:
            logger.info("Running SBR dataset ingestion...")
            sbr_result = await ingest_historical_sbr_mlb_lines(
                db=db, start_year=sbr_start, end_year=sbr_end,
            )
            results["sbr"] = sbr_result

    # GitHub dataset (2021-2025)
    if end_year >= 2021:
        git_start = "2021-04-01"
        git_end = f"{min(end_year, 2025)}-12-31"
        if start_year <= 2025:
            logger.info("Running GitHub dataset ingestion...")
            git_result = await ingest_historical_mlb_lines(
                db=db,
                start_date=git_start,
                end_date=git_end,
                preferred_sportsbook=sportsbook,
            )
            results["github"] = git_result

    return {"status": "ok", "datasets": results}
    """
    Load historical MLB betting lines from the free GitHub dataset.

    Downloads a 76 MB JSON file with opening + closing lines from multiple
    sportsbooks (FanDuel, DraftKings, BetMGM, Bet365, Caesars, BetRivers).

    Date range available: 2021-04-01 to 2025-08-16
    """
    result = await ingest_historical_mlb_lines(
        db=db,
        start_date=start_date,
        end_date=end_date,
        source_name=source,
        preferred_sportsbook=sportsbook,
    )
    return {"status": "ok", **result}


@router.post("/ingest/mlb/betting-lines/current")
async def ingest_mlb_current_betting_lines(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    days_from_now: int = Query(14, description="Look ahead this many days for upcoming games"),
    source: str = Query("the_odds_api", description="Data source label"),
    sportsbook: str | None = Query(None, description="Specific sportsbook filter"),
    db: AsyncSession = Depends(get_db),
):
    """
    Fetch current MLB betting lines from The Odds API.

    Uses the baseball_mlb sport key. Requires a free API key.
    Falls back to ODDS_API_KEY environment variable.
    """
    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")
    result = await ingest_current_mlb_lines(
        db=db,
        api_key=api_key,
        source_name=source,
        days_from_now=days_from_now,
        sportsbook=sportsbook,
    )
    return {"status": "ok", **result}


@router.post("/ingest/mlb/opening-lines/snapshot")
async def ingest_mlb_opening_lines_snapshot(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    days_from_now: int = Query(14, description="Look ahead from today"),
    db: AsyncSession = Depends(get_db),
):
    """
    Snapshot MLB opening lines from The Odds API.

    Saves with is_opening='true' and source='the_odds_api_opening'.
    Only saves for games that don't already have an opening line.
    """
    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"status": "error", "detail": "No API key provided. Get one free at https://the-odds-api.com/"}
    result = await snapshot_mlb_opening_lines(
        db=db,
        api_key=api_key,
        days_from_now=days_from_now,
    )
    return {"status": "ok", **result}


@router.post("/ingest/mlb/betting-lines/historical/odds-api")
async def ingest_mlb_historical_odds_api(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    start_date: str = Query(None, description="Start date YYYY-MM-DD (default 2020-06-30)"),
    end_date: str = Query(None, description="End date YYYY-MM-DD (default today)"),
    source: str = Query("the_odds_api_historical", description="Data source label"),
    markets: str = Query("totals,spreads,h2h", description="Markets to fetch (comma-sep)"),
    db: AsyncSession = Depends(get_db),
):
    """
    Ingest historical MLB betting lines from The Odds API paid-tier.

    Uses the odds-history endpoint. Requires a Professional (paid) API key.
    Available from 2020-06-30 onward. Costs 10 credits per region per market.

    Queries once per game date at ~19:00 UTC (3 PM ET) to capture
    closing-adjacent lines from multiple sportsbooks.
    """
    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")
    if not api_key:
        return {"status": "error", "detail": "No API key provided. Paid key required."}

    start = None
    end = None
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

    result = await ingest_historical_odds_api_mlb_lines(
        db=db,
        api_key=api_key,
        start_date=start,
        end_date=end,
        source_name=source,
        markets=markets,
    )
    return {"status": "ok", **result}


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
        logger.info(f"  Probable pitchers updated: {pitchers_changed}")

        # Step 7: Starting lineups (always run)
        logger.info("[Step 7] Fetching starting lineups...")
        from datetime import date
        try:
            from app.ingestion.mlb_lineups import update_lineups_for_date
            today = date.today()
            lineup_result = await update_lineups_for_date(db, today)
            logger.info(f"  Lineups: {lineup_result.get('lineups_saved', 0)} saved, {lineup_result.get('pitchers_updated', 0)} pitchers updated")
            pitchers_changed += lineup_result.get('pitchers_updated', 0)
        except Exception as e:
            logger.error(f"  Lineups fetch failed: {e}")

        # Step 8: Pitcher changes logged — picks are regenerated by lines-and-picks cycle
        else:
            logger.info("[Step 7] No pitcher changes — picks unchanged")

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


@router.post("/ingest/mlb/rosters/sync")
async def ingest_mlb_rosters_sync():
    """
    Sync active 40-man rosters for all 30 MLB teams from the MLB Stats API.
    Updates player.team_id and player.status (IL, etc.) for the current season.
    """
    import asyncio
    from app.database import async_session
    from app.ingestion.mlb_stats import sync_teams, sync_all_team_rosters

    async with async_session() as db:
        team_map = await sync_teams(db)
        result = await sync_all_team_rosters(db, team_map)
        summary = result.get("_summary", {})
        return {
            "status": "ok",
            "active": summary.get("total_active", 0),
            "injured": summary.get("total_injured", 0),
            "teams": {k: v for k, v in result.items() if k != "_summary"},
        }


@router.post("/ingest/mlb/backfill-scores")
async def ingest_mlb_backfill_scores():
    """
    One-shot backfill: find FINAL games with NULL scores and fetch them
    from the MLB Stats API live feed.
    """
    from app.database import async_session
    from app.ingestion.mlb_stats import update_game_statuses

    async with async_session() as db:
        result = await update_game_statuses(db, days_back=30, days_forward=3)
        try:
            await db.commit()
        except Exception as e:
            await db.rollback()
            return {"status": "error", "message": str(e)}
        return {
            "status": "ok",
            "games_updated": result.get("games_updated", 0),
            "scores_updated": result.get("scores_updated", 0),
            "status_changes": result.get("status_changes", {}),
        }


@router.post("/ingest/mlb/daily-prep")
async def ingest_mlb_daily_prep(
    api_key: str = Query("", description="The Odds API key. Falls back to ODDS_API_KEY env var."),
    db: AsyncSession = Depends(get_db),
):
    """
    MLB daily morning pipeline:
      1. Snapshot opening lines from The Odds API
      2. Generate predictions for today's games via MLBHandicapper

    Runs before first pitch (target ~9:00 AM CT).
    """
    from app.ingestion.mlb_betting_lines import snapshot_mlb_opening_lines
    from app.handicapping.mlb.mlb_engine import MLBHandicapper

    if not api_key:
        api_key = os.getenv("ODDS_API_KEY", "")

    results = {"opening_lines": None, "predictions": None, "errors": [], "consolidated": None}

    # Step 1: Snapshot opening + current lines from The Odds API
    updated_game_ids = []
    if api_key:
        try:
            lines_result = await snapshot_mlb_opening_lines(
                db=db,
                api_key=api_key,
                days_from_now=3,
            )
            results["opening_lines"] = lines_result
            updated_game_ids = lines_result.get("updated_game_ids", [])
        except Exception as e:
            results["errors"].append(f"Opening lines snapshot failed: {e}")
    else:
        results["opening_lines"] = {"detail": "No API key, skipped"}

    # Step 1b: Run consolidation (uses best sportsbook per game with consensus checking)
    if updated_game_ids:
        try:
            import subprocess as _sp
            import os
            script_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "../ingestion/mlb_betting_lines_consolidate.py"
            )
            proc = _sp.run(
                [sys.executable, script_path, "--games"] + [str(gid) for gid in updated_game_ids],
                capture_output=True, text=True, timeout=120,
                cwd=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                env={**os.environ, "PYTHONPATH": os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")},
            )
            for line in proc.stdout.split("\n"):
                if line.strip():
                    logger.info(f"[consolidated] {line}")
            if proc.returncode != 0:
                results["errors"].append(f"Consolidation failed: {proc.stderr[:300]}")
            results["consolidated"] = {
                "games": len(updated_game_ids),
                "stdout": proc.stdout[-300:],
            }
        except Exception as e:
            results["errors"].append(f"Consolidated refresh failed: {e}")
            logger.error(f"Consolidated refresh failed: {e}")

    # Step 2: Use MLBHandicapper.handicap_date() to generate picks
    try:
        import logging
        logger = logging.getLogger("earl.mlb_daily_prep")

        today_str = datetime.now().strftime("%Y-%m-%d")
        logger.info(f"Running MLBHandicapper for {today_str}...")

        handicapper = MLBHandicapper(db)
        picks = await handicapper.handicap_date(today_str, num_games=10)

        results["predictions"] = {
            "games_analyzed": len(picks),
            "picks": [p.to_dict() for p in picks],
        }
        logger.info(f"Generated {len(picks)} picks for {today_str}")
    except Exception as e:
        import traceback
        results["errors"].append(f"Predictions failed: {e}")
        logger.error(f"MLB predictions failed: {e}\n{traceback.format_exc()}")

    return {"status": "ok", "results": results}


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
    from app.handicapping.mlb.data_loader import get_data_loader, build_features
    from app.handicapping.mlb.mlb_engine import (
        _save_api_prediction,
        _extract_feature_vector,
        _load_model_for_year,
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
        import pandas as pd
        import numpy as np

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
            # 3b – Load model pkl files once
            ats_model = _load_model_for_year("ats", CURRENT_YEAR)
            ou_model = _load_model_for_year("ou", CURRENT_YEAR)
            logger.info(f"Models loaded for {CURRENT_YEAR} (ats={'loaded' if ats_model else 'none'}, ou={'loaded' if ou_model else 'none'})")

            # 3c – Load ALL historic finished games + upcoming games, build features ONCE
            dl = get_data_loader()
            all_historic = dl.load_games(status="FINAL", include_upcoming=False)

            # Batch-load all target games with one query
            target_games = dl.load_games(status=None, include_upcoming=True, game_ids=game_ids_needing_picks)

            if target_games.empty:
                results["predictions"] = {"picks_generated": 0, "note": "No games found in DB"}
            else:
                combined = pd.concat([all_historic, target_games], ignore_index=True)
                df = build_features(combined)
                logger.info(f"Feature DF built: {len(df)} rows, {len(df.columns)} cols, "
                           f"{len(game_ids_needing_picks)} target games")

                # 3d – Get consolidated lines
                from app.models.mlb.consolidated import MLBBettingLineConsolidated
                from sqlalchemy import select as sa_select
                rows_result = await db.execute(
                    sa_select(MLBBettingLineConsolidated).where(
                        MLBBettingLineConsolidated.game_id.in_(game_ids_needing_picks)
                    )
                )
                line_rows = {r.game_id: r for r in rows_result.scalars().all()}

                pick_results = []
                for gid in game_ids_needing_picks:
                    try:
                        row = df[df["game_id"].astype(str) == str(gid)]
                        if row.empty:
                            logger.warning(f"Game {gid} not in feature set")
                            pick_results.append({"game_id": gid, "error": "not_in_feature_set"})
                            continue
                        row_s = row.iloc[0]

                        # Get spread / total from consolidated line
                        line = line_rows.get(gid)
                        spread = float(line.closing_spread) if line and line.closing_spread else (
                            float(row_s.get("spread", row_s.get("h_line_runline", 1.5)))
                            if pd.notna(row_s.get("spread")) else None
                        )
                        total = float(line.closing_ou) if line and line.closing_ou else (
                            float(row_s.get("over_under", row_s.get("ou_line", 8.5)))
                            if pd.notna(row_s.get("over_under")) else None
                        )

                        # Predict
                        ats_feats = _extract_feature_vector(row_s, "ats")
                        ou_feats = _extract_feature_vector(row_s, "ou")

                        if ats_feats is not None and ats_model:
                            pred_margin = float(ats_model.predict(ats_feats[np.newaxis, :])[0])
                        else:
                            pred_margin = 0.0

                        if ou_feats is not None and ou_model:
                            pred_total = float(ou_model.predict(ou_feats[np.newaxis, :])[0])
                        else:
                            pred_total = total or 8.5

                        pred_home_covers = pred_margin > -(spread or 0) if spread else True
                        pred_over = pred_total > (total or 8.5) if total else True
                        pred_home_wins = pred_margin > 0

                        # Save to DB
                        await _save_api_prediction(
                            db, row_s, CURRENT_YEAR,
                            spread, total,
                            pred_margin, pred_total,
                            pred_home_covers, pred_over, pred_home_wins,
                        )
                        pick_results.append({
                            "game_id": gid,
                            "home": str(row_s.get("ha", "")),
                            "away": str(row_s.get("aa", "")),
                            "spread": spread,
                            "total": total,
                            "predicted_margin": round(pred_margin, 2),
                            "predicted_total": round(pred_total, 2),
                        })
                    except Exception as exc:
                        logger.warning(f"Prediction failed for game {gid}: {exc}")
                        pick_results.append({"game_id": gid, "error": str(exc)[:200]})

                await db.commit()
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

