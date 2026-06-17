"""
Ingestion pipeline for EarlKnowsBall.
Populates the database from free NFL data sources.
"""
import httpx
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, Player, Season


TEAM_INFO = {
    "ARI": {"name": "Arizona Cardinals", "conf": "NFC", "div": "West"},
    "ATL": {"name": "Atlanta Falcons", "conf": "NFC", "div": "South"},
    "BAL": {"name": "Baltimore Ravens", "conf": "AFC", "div": "North"},
    "BUF": {"name": "Buffalo Bills", "conf": "AFC", "div": "East"},
    "CAR": {"name": "Carolina Panthers", "conf": "NFC", "div": "South"},
    "CHI": {"name": "Chicago Bears", "conf": "NFC", "div": "North"},
    "CIN": {"name": "Cincinnati Bengals", "conf": "AFC", "div": "North"},
    "CLE": {"name": "Cleveland Browns", "conf": "AFC", "div": "North"},
    "DAL": {"name": "Dallas Cowboys", "conf": "NFC", "div": "East"},
    "DEN": {"name": "Denver Broncos", "conf": "AFC", "div": "West"},
    "DET": {"name": "Detroit Lions", "conf": "NFC", "div": "North"},
    "GB":  {"name": "Green Bay Packers", "conf": "NFC", "div": "North"},
    "HOU": {"name": "Houston Texans", "conf": "AFC", "div": "South"},
    "IND": {"name": "Indianapolis Colts", "conf": "AFC", "div": "South"},
    "JAX": {"name": "Jacksonville Jaguars", "conf": "AFC", "div": "South"},
    "KC":  {"name": "Kansas City Chiefs", "conf": "AFC", "div": "West"},
    "LAC": {"name": "Los Angeles Chargers", "conf": "AFC", "div": "West"},
    "LAR": {"name": "Los Angeles Rams", "conf": "NFC", "div": "West"},
    "LV":  {"name": "Las Vegas Raiders", "conf": "AFC", "div": "West"},
    "MIA": {"name": "Miami Dolphins", "conf": "AFC", "div": "East"},
    "MIN": {"name": "Minnesota Vikings", "conf": "NFC", "div": "North"},
    "NE":  {"name": "New England Patriots", "conf": "AFC", "div": "East"},
    "NO":  {"name": "New Orleans Saints", "conf": "NFC", "div": "South"},
    "NYG": {"name": "New York Giants", "conf": "NFC", "div": "East"},
    "NYJ": {"name": "New York Jets", "conf": "AFC", "div": "East"},
    "PHI": {"name": "Philadelphia Eagles", "conf": "NFC", "div": "East"},
    "PIT": {"name": "Pittsburgh Steelers", "conf": "AFC", "div": "North"},
    "SEA": {"name": "Seattle Seahawks", "conf": "NFC", "div": "West"},
    "SF":  {"name": "San Francisco 49ers", "conf": "NFC", "div": "West"},
    "TB":  {"name": "Tampa Bay Buccaneers", "conf": "NFC", "div": "South"},
    "TEN": {"name": "Tennessee Titans", "conf": "AFC", "div": "South"},
    "WAS": {"name": "Washington Commanders", "conf": "NFC", "div": "East"},
}


async def _get_or_create_team(session: AsyncSession, abbr: str) -> Team | None:
    info = TEAM_INFO.get(abbr.upper())
    if not info:
        return None
    result = await session.execute(select(Team).where(Team.abbreviation == abbr.upper()))
    team = result.scalar_one_or_none()
    if team:
        return team
    team = Team(abbreviation=abbr.upper(), name=info["name"], conference=info["conf"], division=info["div"])
    session.add(team)
    await session.flush()
    return team


async def ingest_sleeper_players(session: AsyncSession) -> int:
    """Load all NFL players from the free Sleeper API. Idempotent — updates existing."""
    players_loaded = 0
    updated = 0
    team_cache = set()

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get("https://api.sleeper.app/v1/players/nfl")
        resp.raise_for_status()
        raw = resp.json()

    for player_id, data in raw.items():
        pos = data.get("position")
        if pos not in ("QB", "RB", "WR", "TE", "K", "DST"):
            continue

        team_abbr = data.get("team")
        team = None
        if team_abbr and team_abbr not in team_cache:
            team = await _get_or_create_team(session, team_abbr)
            if team:
                team_cache.add(team_abbr)
        elif team_abbr and team_abbr in team_cache:
            r = await session.execute(select(Team).where(Team.abbreviation == team_abbr.upper()))
            team = r.scalar_one_or_none()

        gsis = data.get("gsis_id")
        gsis_id = str(gsis).strip() if gsis and str(gsis).strip() else None

        # Check existing
        result = await session.execute(select(Player).where(Player.sleeper_id == player_id))
        existing = result.scalar_one_or_none()
        if existing:
            existing.team_id = team.id if team else None
            existing.status = data.get("status", "Active")
            if not existing.nflverse_id and gsis_id:
                existing.nflverse_id = gsis_id
            updated += 1
            continue

        weight_raw = data.get("weight")
        years_raw = data.get("years_exp", 0)
        jersey_raw = data.get("number")
        birth_raw = data.get("birth_date")
        birth_date = datetime.strptime(birth_raw, "%Y-%m-%d").date() if birth_raw else None

        player = Player(
            sleeper_id=player_id,
            nflverse_id=gsis_id,
            name=data.get("full_name") or data.get("first_name", ""),
            position=pos,
            team_id=team.id if team else None,
            status=data.get("status", "Active"),
            jersey_number=int(jersey_raw) if jersey_raw else None,
            height=parse_height(data.get("height")),
            weight=int(weight_raw) if weight_raw else None,
            birth_date=birth_date,
            college=data.get("college"),
            years_exp=int(years_raw) if years_raw else 0,
        )
        session.add(player)
        players_loaded += 1

        if players_loaded % 500 == 0:
            await session.flush()

    await session.commit()
    print(f"  Sleeper: {players_loaded} new, {updated} updated")
    return players_loaded


def parse_height(height_str: str | None) -> int | None:
    if not height_str:
        return None
    try:
        parts = height_str.replace("'", " ").replace('"', "").split()
        if len(parts) == 2:
            return int(parts[0]) * 12 + int(parts[1])
    except (ValueError, IndexError):
        return None
    return None


async def run_full_ingestion(session: AsyncSession) -> dict:
    print("[Earl] Starting full ingestion...")
    player_count = await ingest_sleeper_players(session)
    result = await session.execute(select(Season).where(Season.year == 2025))
    if not result.scalar_one_or_none():
        session.add(Season(year=2025))
        await session.commit()
    return {"players_loaded": player_count, "teams_seeded": len(TEAM_INFO)}
