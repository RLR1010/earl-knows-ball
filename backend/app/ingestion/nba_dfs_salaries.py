"""NBA DFS salary scraper — DraftKings + FanDuel."""

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBADfsSalary, NBAPlayer, NBATeam, NBAGame, NBASeason

logger = logging.getLogger("earl.nba_dfs")

DK_LOBBY_URL = "https://www.draftkings.com/lobby/getcontests?sport=NBA"
DK_DRAFTABLES_TPL = "https://api.draftkings.com/draftgroups/v1/{}/draftables"
FD_FIXTURES_URL = "https://api.fanduel.com/fixtures?include_extra=true"


def _map_dk_team(team_abbr: str) -> str | None:
    """Map DraftKings abbreviation to our DB abbreviation."""
    mapping = {
        "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
        "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
        "DET": "DET", "GS": "GSW", "HOU": "HOU", "IND": "IND",
        "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
        "MIL": "MIL", "MIN": "MIN", "NO": "NOP", "NY": "NYK",
        "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
        "POR": "POR", "SAC": "SAC", "SA": "SAS", "TOR": "TOR",
        "UTAH": "UTA", "WAS": "WAS",
    }
    return mapping.get(team_abbr.upper())


async def scrape_draftkings_nba(db: AsyncSession) -> dict:
    """Scrape current NBA DraftKings salaries."""
    headers = {"User-Agent": "Mozilla/5.0"}
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.get(DK_LOBBY_URL)
        if resp.status_code != 200:
            return {"error": f"DraftKings lobby: {resp.status_code}"}
        data = resp.json()
        contests = data.get("Contests", [])
        nba_contests = []
    for c in contests:
        gt = c.get("gameType", {})
        if isinstance(gt, dict):
            gid = gt.get("gameTypeId")
        elif isinstance(gt, str):
            gid = None  # No NBA contests available (off-season)
        else:
            gid = None
        if gid == 1:
            nba_contests.append(c)

        if not nba_contests:
            return {"error": "No NBA contests found (off-season?)"}

        draft_group_id = None
        for c in nba_contests:
            dg = c.get("ContestDetail", {}).get("draftGroupId")
            if dg:
                draft_group_id = dg
                break

        if not draft_group_id:
            return {"error": "No draft group ID found"}

        draft_resp = await client.get(DK_DRAFTABLES_TPL.format(draft_group_id))
        if draft_resp.status_code != 200:
            return {"error": f"Draftables: {draft_resp.status_code}"}
        draft_data = draft_resp.json()
        players = draft_data.get("draftables", [])

    loaded = 0
    season_result = await db.execute(select(NBASeason).where(NBASeason.year == 2025))
    season = season_result.scalar_one_or_none()
    if not season:
        season = NBASeason(year=2025)
        db.add(season)
        await db.flush()
    season_id = season.id

    team_map = {}
    result = await db.execute(select(NBATeam))
    for t in result.scalars():
        team_map[t.abbreviation] = t.id

    for p in players:
        try:
            name = p.get("displayName", "").strip() or p.get("firstName", "") + " " + p.get("lastName", "")
            position = p.get("position", "F")[:10]
            salary = p.get("salary")
            if not salary or salary <= 0:
                continue
            team_abbr = _map_dk_team(p.get("teamAbbreviation", ""))
            team_id = team_map.get(team_abbr) if team_abbr else None
            opponent_abbr = p.get("opponentAbbreviation", "")
            opp_abbr = _map_dk_team(opponent_abbr) if opponent_abbr else None
            opp_id = team_map.get(opp_abbr) if opp_abbr else None

            game_id = None
            game_key = p.get("gameId")
            if game_key:
                result = await db.execute(
                    select(NBAGame).where(NBAGame.nba_game_id == str(game_key))
                )
                g = result.scalar_one_or_none()
                if g:
                    game_id = g.id

            salary_entry = NBADfsSalary(
                platform="draftkings",
                player_name=name,
                position=position,
                salary=salary,
                team_id=team_id,
                opponent_id=opp_id,
                game_id=game_id,
                season_id=season_id,
                slate_type="main",
                scraped_at=datetime.now(timezone.utc),
            )
            db.add(salary_entry)
            loaded += 1
        except Exception as e:
            logger.warning(f"DK error on {p.get('displayName')}: {e}")

    await db.commit()
    logger.info(f"DraftKings NBA: {loaded} salaries loaded")
    return {"platform": "draftkings", "loaded": loaded}


async def scrape_fanduel_nba(db: AsyncSession) -> dict:
    """Scrape current NBA FanDuel salaries."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Origin": "https://www.fanduel.com",
        "Referer": "https://www.fanduel.com/",
    }
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.get(FD_FIXTURES_URL)
        if resp.status_code != 200:
            return {"error": f"FanDuel: {resp.status_code}"}
        data = resp.json()

    loaded = 0
    season_result = await db.execute(select(NBASeason).where(NBASeason.year == 2025))
    season = season_result.scalar_one_or_none()
    if not season:
        season = NBASeason(year=2025)
        db.add(season)
        await db.flush()
    season_id = season.id

    for fixture in data if isinstance(data, list) else data.get("fixtures", []):
        for entry in fixture.get("entries", []):
            try:
                name = entry.get("name", "").strip()
                position = entry.get("position", "F")[:10]
                salary = entry.get("salary")
                if not salary or salary <= 0:
                    continue
                team_abbr = _map_dk_team(entry.get("teamAbbreviation", ""))
                team_id = None
                if team_abbr:
                    r = await db.execute(select(NBATeam).where(NBATeam.abbreviation == team_abbr))
                    t = r.scalar_one_or_none()
                    if t:
                        team_id = t.id

                salary_entry = NBADfsSalary(
                    platform="fanduel",
                    player_name=name,
                    position=position,
                    salary=salary,
                    team_id=team_id,
                    season_id=season_id,
                    slate_type="main",
                    scraped_at=datetime.now(timezone.utc),
                )
                db.add(salary_entry)
                loaded += 1
            except Exception as e:
                logger.warning(f"FD error on {entry.get('name')}: {e}")

    await db.commit()
    logger.info(f"FanDuel NBA: {loaded} salaries loaded")
    return {"platform": "fanduel", "loaded": loaded}


async def scrape_all_nba(db: AsyncSession) -> dict:
    """Scrape both DraftKings and FanDuel NBA salaries."""
    dk = await scrape_draftkings_nba(db)
    fd = await scrape_fanduel_nba(db)
    return {"draftkings": dk, "fanduel": fd}


async def quick_test():
    from app.database import async_session
    async with async_session() as db:
        result = await scrape_draftkings_nba(db)
        print(result)
        result2 = await scrape_fanduel_nba(db)
        print(result2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(quick_test())
