import logging
from fastapi import APIRouter, HTTPException
from sqlalchemy import text
from app.database import async_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/teams/{abbr}/players")
async def mlb_team_players(abbr: str):
    """Get players for an MLB team (from stats API)."""
    try:
        async with async_session() as db:
            r = await db.execute(text("SELECT api_team_id FROM mlb.teams WHERE LOWER(abbreviation) = :abbr"), {"abbr": abbr.lower()})
            row = r.fetchone()
            if not row:
                return {"players": [], "injuries": []}
            api_id = row.api_team_id

        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            roster_resp = await client.get(f"https://statsapi.mlb.com/api/v1/teams/{api_id}/roster?rosterType=fullSeason")
            if roster_resp.status_code != 200:
                return {"players": [], "injuries": []}

            roster_data = roster_resp.json()
            players = []
            injuries = []

            for entry in roster_data.get("roster", []):
                person = entry.get("person", {}) or {}
                status = entry.get("status", {}) or {}
                position = entry.get("position", {}) or {}

                players.append({
                    "id": person.get("id"),
                    "fullName": person.get("fullName", ""),
                    "position": position.get("abbreviation", ""),
                    "jerseyNumber": person.get("primaryNumber", person.get("jerseyNumber", "")),
                    "status": status.get("code", "A"),
                    "statusDescription": status.get("description", "Active"),
                })

                if status.get("code") in ("IL10", "IL60", "D7", "D15", "D60"):
                    injuries.append({
                        "player_id": person.get("id"),
                        "player_name": person.get("fullName", ""),
                        "injury_type": status.get("description", ""),
                        "status": status.get("code", ""),
                    })

            return {"players": players, "injuries": injuries}

    except Exception as e:
        logger.warning(f"Failed to fetch MLB players: {e}")
        return {"players": [], "injuries": []}


@router.get("/leaders")
async def mlb_leaders():
    """Get MLB league leaders (from stats API)."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Fetch top hitters from stats API
            resp = await client.get("https://statsapi.mlb.com/api/v1/stats/leaders?leaderCategories=homeRuns&limit=10&season=2026")
            if resp.status_code == 200:
                data = resp.json()
                return data

        return {"leaderCategories": [], "leaders": []}
    except Exception:
        return {"leaderCategories": [], "leaders": []}
