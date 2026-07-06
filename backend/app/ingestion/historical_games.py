"""Load historical game schedules/results from nflverse games.csv."""

import httpx
import io
import csv
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, Game, Season
from app.models.nfl.game import GameStatus


async def ingest_historical_games(session: AsyncSession) -> dict:
    """Load all historical game data from nflverse games.csv (1999–present)."""
    # Pre-load team cache
    result = await session.execute(select(Team))
    teams = {t.abbreviation: t for t in result.scalars().all()}
    print(f"  Cached {len(teams)} teams")

    # Download
    url = "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.text

    reader = csv.DictReader(io.StringIO(content))
    loaded = 0
    skipped = 0
    missing_team = 0
    missing_season = 0
    espn_missing = 0
    bypassed = 0

    for row in reader:
        season_year = int(row.get("season", 0))
        if season_year < 2005:
            continue  # Skip before our range

        espn_id = row.get("espn", "").strip()
        if not espn_id:
            espn_missing += 1
            continue  # Skip games without ESPN ID

        # Check if already exists
        existing = await session.execute(select(Game).where(Game.id == int(espn_id)))
        if existing.scalar_one_or_none():
            skipped += 1
            continue

        home_abbr = str(row.get("home_team", "")).strip().upper()
        away_abbr = str(row.get("away_team", "")).strip().upper()

        # Map historical team abbreviations to current
        TEAM_ABBR_MAP = {
            "LA": "LAR", "SL": "LAR", "STL": "LAR",  # Rams (STL→LA→LAR)
            "SD": "LAC",  # Chargers (SD→LAC)
            "OAK": "LV",  # Raiders (OAK→LV)
            "WSH": "WAS",  # Washington
        }
        home_abbr = TEAM_ABBR_MAP.get(home_abbr, home_abbr)
        away_abbr = TEAM_ABBR_MAP.get(away_abbr, away_abbr)

        home_team = teams.get(home_abbr)
        away_team = teams.get(away_abbr)
        if not home_team or not away_team:
            missing_team += 1
            continue

        # nflverse uses LAR, LAC, WAS, etc. which match our DB

        # Get or create season
        season_result = await session.execute(select(Season).where(Season.year == season_year))
        season = season_result.scalar_one_or_none()
        if not season:
            # Create season (they already exist in DB from earlier setup)
            missing_season += 1
            continue

        game_type = str(row.get("game_type", "REG")).upper()
        gt = "REG"
        if game_type in ("POST", "WC", "DIV", "CON", "SB"):
            gt = "POST"
        elif game_type in ("PRE", "HOF"):
            gt = "PRE"

        week = int(row.get("week", 0))

        # Date
        gameday = row.get("gameday", "")
        gametime = row.get("gametime", "")
        try:
            if gameday:
                if gametime:
                    game_date = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
                else:
                    game_date = datetime.strptime(gameday, "%Y-%m-%d")
            else:
                game_date = datetime.now()
        except (ValueError, TypeError):
            game_date = datetime.now()

        # Scores
        home_s = row.get("home_score")
        away_s = row.get("away_score")
        if home_s and away_s and home_s.strip() and away_s.strip():
            status = GameStatus.FINAL
            home_score = int(home_s)
            away_score = int(away_s)
        else:
            status = GameStatus.SCHEDULED
            home_score = None
            away_score = None

        # Venue info
        stadium = row.get("stadium", "").strip() or None
        roof = row.get("roof", "").strip() or None
        surface = row.get("surface", "").strip() or None
        # Weather
        temp_raw = row.get("temp", "").strip()
        temp = int(float(temp_raw)) if temp_raw and temp_raw != "nan" and temp_raw != "" else None
        wind_raw = row.get("wind", "").strip()
        wind = int(float(wind_raw)) if wind_raw and wind_raw != "nan" and wind_raw != "" else None

        game = Game(
            id=int(espn_id),
            season_id=season.id,
            week=week,
            game_type=gt,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            date=game_date,
            status=status,
            home_score=home_score,
            away_score=away_score,
            venue=stadium,
            roof_type=roof if roof and roof != "nan" else None,
            surface=surface if surface and surface != "nan" else None,
            temperature=temp,
            wind_speed=wind,
        )
        session.add(game)
        loaded += 1

        if loaded % 500 == 0:
            await session.flush()
            print(f"  {loaded} games loaded...")

    await session.commit()
    return {
        "games_loaded": loaded,
        "skipped_duplicate": skipped,
        "missing_espn_id": espn_missing,
        "missing_team": missing_team,
        "missing_season": missing_season,
    }
