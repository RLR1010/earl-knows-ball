"""
nba_games_espn.py — Ingest NBA game data from ESPN
Usage: cd backend && PYTHONPATH=$PWD python3 -m app.ingestion.nba_games_espn
Fetches games year by year from 2016 to most recent season.
"""
import asyncio
import httpx
from datetime import datetime, timedelta
from sqlalchemy import select, text
from app.database import async_session
from app.models.nba import NBASeason, NBATeam, NBAGame
from app.models.nba.game import NBAGameStatus
from app.ingestion.nba_stats import NBA_TEAMS, season_str

Season = NBASeason
Team = NBATeam
Game = NBAGame


NBA_TEAM_ABBR = {}  # populated on first fetch


async def fetch_espn_games(date_str: str, client: httpx.AsyncClient) -> list:
    """Fetch NBA games from ESPN scoreboard for a given date (YYYYMMDD)."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    params = {"dates": date_str, "limit": 1000}
    resp = await client.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    return data.get("events", [])


async def ensure_team(abbr: str, session, client: httpx.AsyncClient) -> int:
    """Get or create a team by abbreviation. Returns team id."""
    abbr = abbr.upper()[:4]  # nba.teams.abbreviation is VARCHAR(4)
    result = await session.execute(
        select(Team).where(Team.abbreviation == abbr)
    )
    team = result.scalar_one_or_none()
    if team:
        return team.id

    # Fetch team info from ESPN
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{abbr}"
    try:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        team_data = data.get("team", {})
        name = team_data.get("displayName", abbr)
        logo = (team_data.get("logos") or [{}])[0].get("href", "")
    except Exception:
        name = abbr
        logo = ""

    # Conference / division are NOT reliably present on the single-team ESPN
    # endpoint; take them from the authoritative NBA_TEAMS list (all 30 teams).
    # Match by name (ESPN abbreviations like NY/GS vs NBA_TEAMS NYK/GSW differ).
    _tup = next((t for t in NBA_TEAMS if t[2].lower() == str(name).lower()), None)
    # Event/all-star/summer-stub teams (e.g. "MEL") aren't in NBA_TEAMS — give
    # non-null placeholders so the NOT NULL constraint is satisfied.
    conf = (_tup[3] if _tup else "")
    div = (_tup[4] if _tup else "")

    new_team = Team(
        abbreviation=abbr, name=name,
        conference=conf, division=div, logo_url=logo,
    )
    session.add(new_team)
    await session.flush()
    return new_team.id


async def ensure_season(year: int, session) -> int:
    """Get or create a season. Returns season id."""
    result = await session.execute(
        select(Season).where(Season.year == year)
    )
    season = result.scalar_one_or_none()
    if season:
        return season.id

    new_season = Season(year=year, name=f"{year}-{year+1} NBA Season", is_regular_season=True)
    session.add(new_season)
    await session.flush()
    return new_season.id


async def ingest_nba_games(seasons: list[int] | None = None):
    """Main entry point: load NBA games for the given seasons.

    Args:
        seasons: list of season start years (e.g. [2025] for 2025-26).
                 Defaults to the full historical backfill (2016-17 .. 2024-25).
    """
    async with httpx.AsyncClient() as client:
        async with async_session() as session:
            loaded = 0
            skipped = 0

            year_list = seasons if seasons else list(range(2024, 2015, -1))

            for season_year in year_list:
                season_id = await ensure_season(season_year, session)

                # Determine date range for this season
                start_date = datetime(season_year - 1, 10, 1)
                end_date = datetime(season_year, 6, 30)
                current = start_date

                print(f"\n{'='*50}")
                print(f"Loading {season_year-1}-{season_year} season...")
                print(f"{'='*50}")

                while current <= end_date:
                    date_str = current.strftime("%Y%m%d")
                    events = await fetch_espn_games(date_str, client)

                    if events:
                        for event in events:
                            game_id = int(event["id"])
                            # Check if already exists
                            existing = await session.execute(
                                select(Game).where(Game.id == game_id)
                            )
                            if existing.scalar_one_or_none():
                                skipped += 1
                                continue

                            comps = event.get("competitions", [])
                            if not comps:
                                skipped += 1
                                continue

                            comp = comps[0]
                            competitors = comp.get("competitors", [])
                            if len(competitors) < 2:
                                skipped += 1
                                continue

                            # Determine home/away
                            away_team = next(
                                (c for c in competitors if c.get("homeAway") == "away"),
                                competitors[0]
                            )
                            home_team = next(
                                (c for c in competitors if c.get("homeAway") == "home"),
                                competitors[1]
                            )

                            away_abbr = (away_team["team"].get("abbreviation") or away_team["team"].get("shortDisplayName") or away_team["team"]["id"])
                            home_abbr = (home_team["team"].get("abbreviation") or home_team["team"].get("shortDisplayName") or home_team["team"]["id"])

                            away_id = await ensure_team(away_abbr, session, client)
                            home_id = await ensure_team(home_abbr, session, client)

                            # Scores
                            away_score = away_team.get("score")
                            home_score = home_team.get("score")
                            try:
                                away_score = int(away_score) if away_score else None
                                home_score = int(home_score) if home_score else None
                            except (ValueError, TypeError):
                                away_score = None
                                home_score = None

                            # Game date/time
                            game_date = comp.get("date", "")
                            try:
                                game_dt = datetime.fromisoformat(game_date.replace("Z", "+00:00"))
                            except (ValueError, TypeError):
                                game_dt = current

                            # Venue
                            venue_info = comp.get("venue", {})
                            venue = venue_info.get("fullName", "") if venue_info else ""
                            neutral = comp.get("neutralSite", False)

                            # Status: ESPN uses freeform strings ("STATUS_SCHEDULED",
                            # "STATUS_IN_PROGRESS", "STATUS_FINAL", ...) → map to enum.
                            _raw_status = (comp.get("status", {})
                                            .get("type", {})
                                            .get("name", "STATUS_SCHEDULED") or "STATUS_SCHEDULED")
                            _st = _raw_status.upper()
                            if "FINAL" in _st:
                                game_status = NBAGameStatus.FINAL
                            elif "POSTPON" in _st:
                                game_status = NBAGameStatus.POSTPONED
                            elif "CANCEL" in _st:
                                game_status = NBAGameStatus.CANCELLED
                            elif "IN_PROGRESS" in _st or "LIVE" in _st:
                                game_status = NBAGameStatus.IN_PROGRESS
                            else:
                                game_status = NBAGameStatus.SCHEDULED

                            game = Game(
                                id=game_id,
                                season_id=season_id,
                                home_team_id=home_id,
                                away_team_id=away_id,
                                home_score=home_score,
                                away_score=away_score,
                                date=game_dt,
                                venue=venue,
                                status=game_status,
                            )
                            session.add(game)
                            loaded += 1

                            # Flush every 25 games
                            if loaded % 25 == 0:
                                await session.flush()
                                print(f"  ... {loaded} loaded ({skipped} skipped)")

                    current += timedelta(days=1)

                # Flush remaining for this season
                await session.flush()
                print(f"Season {season_year-1}-{season_year}: {loaded} total loaded, {skipped} total skipped")

            await session.commit()
            print(f"\n{'='*50}")
            print(f"ALL DONE: {loaded} games loaded, {skipped} skipped")


if __name__ == "__main__":
    asyncio.run(ingest_nba_games())
