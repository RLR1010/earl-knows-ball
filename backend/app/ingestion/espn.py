"""ESPN NFL API data ingestion for schedules, scores, and live data."""

import httpx
from datetime import datetime
from dateutil import parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, Game, Season
from app.models.nfl.game import GameStatus


ESPN_TEAM_MAP = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BUF": "BUF",
    "CAR": "CAR", "CHI": "CHI", "CIN": "CIN", "CLE": "CLE",
    "DAL": "DAL", "DEN": "DEN", "DET": "DET", "GB": "GB",
    "HOU": "HOU", "IND": "IND", "JAX": "JAX", "KC": "KC",
    "LAC": "LAC", "LAR": "LAR", "LV": "LV",
    "MIA": "MIA", "MIN": "MIN", "NE": "NE", "NO": "NO",
    "NYG": "NYG", "NYJ": "NYJ", "PHI": "PHI", "PIT": "PIT",
    "SEA": "SEA", "SF": "SF", "TB": "TB", "TEN": "TEN",
    "WSH": "WAS",
}


SEASON_DATE_RANGES = {
    # NFL season approximate date ranges: (start, end) as mmdd
    2025: ("20250901", "20260215"),
    2024: ("20240901", "20250215"),
    2023: ("20230901", "20240215"),
    2022: ("20220901", "20230215"),
    2021: ("20210901", "20220215"),
    2020: ("20200901", "20210215"),
    2019: ("20190901", "20200215"),
    2018: ("20180901", "20190215"),
    2017: ("20170901", "20180215"),
    2016: ("20160901", "20170215"),
    2015: ("20150901", "20160215"),
    2014: ("20140901", "20150215"),
    2013: ("20130901", "20140215"),
    2012: ("20120901", "20130215"),
    2011: ("20110901", "20120215"),
    2010: ("20100901", "20110215"),
    2009: ("20090901", "20100215"),
    2008: ("20080901", "20090215"),
    2007: ("20070901", "20080215"),
    2006: ("20060901", "20070215"),
    2005: ("20050901", "20060215"),
}


async def fetch_espn_scoreboard(season: int, seasontype: int, week: int | None = None, force_dates: str | None = None) -> list:
    """Fetch all games for a season/week with pagination.
    
    Uses the dates parameter for historical seasons (pre-2025) since the
    year parameter only works for the current/upcoming season.
    """
    all_events = []
    page = 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
            
            date_range = SEASON_DATE_RANGES.get(season)
            if force_dates:
                # Override: use a specific date range (e.g. one month), no pagination needed
                params = {"dates": force_dates, "seasontype": seasontype}
                # fetch all events without pagination; month-level data fits in one page
                # (but we still need to break from the while loop after first fetch)
            elif date_range:
                # Use dates param for complete season data (handles future seasons correctly)
                params = {"dates": f"{date_range[0]}-{date_range[1]}", "seasontype": seasontype, "page": page, "limit": 100}
            else:
                # Use year param for current/recent seasons without a date range
                params = {"year": season, "seasontype": seasontype, "page": page}
            
            if week is not None and not date_range and not force_dates:
                # Only add week param when using year-based query
                params["week"] = week

            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            events = data.get("events", [])
            if not events:
                break
            all_events.extend(events)

            # If force_dates was used, we only need the first page (month-level fits in one)
            if force_dates:
                break

            # Pagination
            total_pages = data.get("pageCount")
            if total_pages is not None:
                # year+week mode: API tells us how many pages
                if page >= total_pages:
                    break
            elif date_range:
                # dates mode: API returns partial pages until empty
                if len(events) < 100:
                    break
            else:
                # year-only mode (future seasons): each page has ~16 events, no pageCount
                # Stop only when a page has NO events
                if len(events) == 0:
                    break

            page += 1

            if page > 60:
                break

    return all_events


async def ingest_espn_schedule(
    session: AsyncSession,
    season_year: int = 2025,
    seasontype: int = 2,
) -> dict:
    """Load schedule from ESPN API."""
    # Delete existing games for this season to get a clean import
    result = await session.execute(select(Season).where(Season.year == season_year))
    season = result.scalar_one_or_none()
    if not season:
        season = Season(year=season_year)
        session.add(season)
        await session.flush()

    # Don't delete existing games — just skip duplicates (stats depend on them)
    # We'll only add new games that don't exist yet

    games_loaded = 0

    # Determine how to fetch the season:
    # For current/past seasons: use year+week params (returns correct data)
    # For future seasons: year+week returns stale data. Use month-by-month dates instead.
    all_events = []
    weeks = list(range(1, 23)) if seasontype in (2, 3) else [None]

    # Peek at just page 1 of week 1 to see if the API returns the correct season
    async with httpx.AsyncClient(timeout=30.0) as client:
        peek_url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        peek_resp = await client.get(peek_url, params={"year": season_year, "seasontype": seasontype, "page": 1, "week": 1})
        peek_data = peek_resp.json()
        peek_events = peek_data.get("events", [])

    if not peek_events:
        return {"games_loaded": 0, "total_games": 0}

    event_season = peek_events[0].get("season", {}).get("year")

    if event_season == season_year:
        # Week-by-week with force_dates month chunks to avoid pagination issues with season-length date ranges
        # (The year+week param works, but SEASON_DATE_RANGES causes fetch_espn_scoreboard to paginate
        #  the entire season for each week when a date range exists. Month-by-month avoids this.)
        for month in range(9, 13):  # Sep-Dec
            month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
            events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
            if events:
                all_events.extend(events)
        for month in range(1, 3):  # Jan-Feb (next year)
            month_range = f"{season_year+1}{month:02d}01-{season_year+1}{month:02d}31"
            events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
            if events:
                all_events.extend(events)
    else:
        # Week param returned stale data — fall back to month-by-month dates
        print(f"  [Earl] Week param returned {event_season} data, falling back to month-by-month")
        for month in range(9, 13):  # Sep-Dec
            month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
            events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
            if events:
                all_events.extend(events)
        for month in range(1, 3):  # Jan-Feb (next year)
            month_range = f"{season_year+1}{month:02d}01-{season_year+1}{month:02d}31"
            events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
            if events:
                all_events.extend(events)

    for event in all_events:
        competitions = event.get("competitions", [])
        if not competitions:
            continue

        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_raw or not away_raw:
            continue

        home_abbr = ESPN_TEAM_MAP.get(home_raw["team"]["abbreviation"], home_raw["team"]["abbreviation"])
        away_abbr = ESPN_TEAM_MAP.get(away_raw["team"]["abbreviation"], away_raw["team"]["abbreviation"])

        r = await session.execute(select(Team).where(Team.abbreviation == home_abbr))
        home_team = r.scalar_one_or_none()
        r = await session.execute(select(Team).where(Team.abbreviation == away_abbr))
        away_team = r.scalar_one_or_none()

        if not home_team or not away_team:
            continue

        date_str = comp.get("date") or event.get("date", "")
        try:
            game_date = parser.parse(date_str)
        except (ValueError, TypeError):
            game_date = datetime.now()

        status_type = comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
        game_status = _map_espn_status(status_type)
        venue = comp.get("venue", {})

        game_id = int(event["id"])

        # Skip if already exists (use no_autoflush to avoid query-triggered flush of pending games)
        with session.no_autoflush:
            existing = await session.execute(select(Game).where(Game.id == game_id))
            if existing.scalar_one_or_none():
                continue

        # Get week from the event data
        event_week = event.get("week", {}).get("number", 0)

        game = Game(
            id=game_id,
            season_id=season.id,
            week=event_week if event_week else 1,
            game_type="REG" if seasontype == 2 else ("PRE" if seasontype == 1 else "POST"),
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            date=game_date,
            status=game_status,
            home_score=_safe_int(home_raw.get("score")),
            away_score=_safe_int(away_raw.get("score")),
            venue=venue.get("fullName") if venue else None,
            roof_type=_get_roof_type(venue),
            surface=venue.get("surface") if venue else None,
        )
        session.add(game)
        games_loaded += 1

        if games_loaded % 50 == 0:
            await session.flush()

    await session.commit()
    return {"games_loaded": games_loaded, "total_games": games_loaded}


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _get_roof_type(venue: dict | None) -> str | None:
    if not venue:
        return None
    raw = venue.get("indoor", None)
    if raw is True:
        return "dome"
    if raw is False:
        return "outdoor"
    return "outdoor"


def _map_espn_status(status: str) -> GameStatus:
    mapping = {
        "STATUS_SCHEDULED": GameStatus.SCHEDULED,
        "STATUS_IN_PROGRESS": GameStatus.IN_PROGRESS,
        "STATUS_FINAL": GameStatus.FINAL,
        "STATUS_POSTPONED": GameStatus.POSTPONED,
        "STATUS_CANCELLED": GameStatus.CANCELLED,
    }
    return mapping.get(status, GameStatus.SCHEDULED)
