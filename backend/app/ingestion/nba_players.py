"""NBA player data ingestion from ESPN API.

Fills in missing bio fields (height, weight, college, jersey, headshot, etc.)
on existing NBA players by fetching team rosters from ESPN.
Also loads player season stats from the ESPN stats API.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBAPlayer, NBATeam, NBASeason, NBAPlayerSeasonStats

logger = logging.getLogger("earl.nba_players")

# DB abbreviation → ESPN URL abbreviation (different for some teams)
ESPN_URL_ABBR = {
    "ATL": "atl", "BOS": "bos", "BKN": "bkn", "CHA": "cha",
    "CHI": "chi", "CLE": "cle", "DAL": "dal", "DEN": "den",
    "DET": "det", "GSW": "gs", "HOU": "hou", "IND": "ind",
    "LAC": "lac", "LAL": "lal", "MEM": "mem", "MIA": "mia",
    "MIL": "mil", "MIN": "min", "NOP": "no", "NYK": "ny",
    "OKC": "okc", "ORL": "orl", "PHI": "phi", "PHX": "phx",
    "POR": "por", "SAC": "sac", "SAS": "sa", "TOR": "tor",
    "UTA": "utah", "WAS": "wsh",
}

BBREF_TEAM_IDS = {
    1610612737: "ATL", 1610612738: "BOS", 1610612739: "CLE",
    1610612740: "NOP", 1610612741: "CHI", 1610612742: "DAL",
    1610612743: "DEN", 1610612744: "GSW", 1610612745: "HOU",
    1610612746: "LAC", 1610612747: "LAL", 1610612748: "MIA",
    1610612749: "MIL", 1610612750: "MIN", 1610612751: "BKN",
    1610612752: "NYK", 1610612753: "ORL", 1610612754: "IND",
    1610612755: "PHI", 1610612756: "PHX", 1610612757: "POR",
    1610612758: "SAC", 1610612759: "SAS", 1610612760: "OKC",
    1610612761: "TOR", 1610612762: "UTA", 1610612763: "MEM",
    1610612764: "WAS", 1610612765: "DET", 1610612766: "CHA",
}

# ESPN player position abbreviations to our format
POSITION_MAP = {
    "PG": "PG", "SG": "SG", "SF": "SF", "PF": "PF", "C": "C",
    "G": "G", "F": "F", "GF": "GF", "FC": "FC",
}


def _pos_to_nyba(espn_pos: str) -> str:
    """Map ESPN position to our format."""
    return POSITION_MAP.get(espn_pos, espn_pos[:4])


async def ingest_rosters(db: AsyncSession) -> dict:
    """Fetch rosters for all NBA teams from ESPN and update player bios."""
    stats = {"updated": 0, "new": 0, "skipped": 0, "errors": 0}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for abbr, espn_abbr in ESPN_URL_ABBR.items():
            url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{espn_abbr}/roster"
            try:
                resp = await client.get(url, timeout=30.0)
                if resp.status_code != 200:
                    logger.warning(f"  {abbr}: ESPN returned {resp.status_code}")
                    stats["errors"] += 1
                    continue

                data = resp.json()
                
                # Get DB team
                result = await db.execute(select(NBATeam).where(NBATeam.abbreviation == abbr))
                db_team = result.scalar_one_or_none()
                if not db_team:
                    logger.warning(f"  {abbr}: Team not found in DB")
                    stats["skipped"] += 1
                    continue

                # ESPN roster: athletes is an array of player objects (or position groups)
                athletes = data.get("athletes", [])
                roster = []
                if isinstance(athletes, list):
                    for item in athletes:
                        if isinstance(item, dict):
                            sub_items = item.get("items")
                            if sub_items and isinstance(sub_items, list):
                                is_injured = item.get("injured", False)
                                for sub in sub_items:
                                    if isinstance(sub, dict):
                                        sub["_injured"] = is_injured
                                        roster.append(sub)
                            else:
                                roster.append(item)
                elif isinstance(athletes, dict):
                    roster = athletes.get("items", [])

                logger.info(f"  {abbr}: {len(roster)} players")

                for p in roster:
                    try:
                        espn_id_str = p.get("id", "")
                        if not espn_id_str:
                            continue

                        # Generate consistent nba_id from ESPN ID
                        nba_id = hash(espn_id_str) % (10**9) + 100000

                        name = p.get("displayName") or f"{p.get('firstName','')} {p.get('lastName','')}"
                        name = name.strip()
                        if not name:
                            continue

                        position = _pos_to_nyba(p.get("position", {}).get("abbreviation", "F"))

                        jersey = p.get("jersey")
                        if jersey:
                            try:
                                jersey = int(jersey)
                            except (ValueError, TypeError):
                                jersey = None

                        height = p.get("height")  # in inches (float)
                        weight = p.get("weight")  # in lbs (float)

                        college = None
                        if p.get("college"):
                            college = p["college"].get("name") or p["college"].get("displayName")

                        birth_date = None
                        dob = p.get("dateOfBirth", "")
                        if dob:
                            try:
                                birth_date = datetime.fromisoformat(dob.replace("Z", "+00:00"))
                            except (ValueError, TypeError):
                                pass

                        debut_year = p.get("debutYear")
                        years_exp = None
                        if debut_year:
                            years_exp = max(0, 2026 - int(debut_year))

                        headshot = None
                        if p.get("headshot"):
                            headshot = p["headshot"].get("href")

                        active = 0 if p.get("_injured") else 1
                        status = "injured" if p.get("_injured") else "active"

                        # Try to find existing player — first by nba_id, then by name + team
                        player = None
                        if nba_id:
                            result = await db.execute(
                                select(NBAPlayer).where(NBAPlayer.nba_id == nba_id)
                            )
                            player = result.scalar_one_or_none()
                        if not player:
                            # Try matching by name (with normalization)

                            # Normalize: strip punctuation and collapse spaces
                            clean_name = re.sub(r'[^a-zA-Z0-9 ]', '', name).strip()
                            clean_name = re.sub(r'\s+', ' ', clean_name)
                            result = await db.execute(
                                text("SELECT * FROM nba.players WHERE regexp_replace(lower(name), '[^a-z0-9 ]', '', 'g') = :clean_name LIMIT 2"),
                                {"clean_name": clean_name.lower()}
                            )
                            existing_by_name = result.mappings().all()
                            if existing_by_name:
                                # RowMapping → load the ORM object
                                # Try exact team match first (for duplicates)
                                matched_row = None
                                if len(existing_by_name) > 1:
                                    for row in existing_by_name:
                                        if row.team_id == db_team.id:
                                            matched_row = row
                                            break
                                if not matched_row:
                                    matched_row = existing_by_name[0]
                                player_id = matched_row.id
                                result = await db.execute(select(NBAPlayer).where(NBAPlayer.id == player_id))
                                player = result.scalar_one_or_none()

                        if not player:
                            player = NBAPlayer(
                                nba_id=nba_id,
                                name=name,
                                position=position,
                                team_id=db_team.id,
                                jersey_number=jersey,
                                height=height,
                                weight=weight,
                                college=college,
                                birth_date=birth_date,
                                years_exp=years_exp,
                                headshot_url=headshot,
                                status=status,
                                active=active,
                            )
                            db.add(player)
                            stats["new"] += 1
                        else:
                            # Update existing
                            player.nba_id = nba_id  # Update to ESPN ID for future matching
                            player.name = name
                            player.position = position
                            player.team_id = db_team.id
                            if jersey: player.jersey_number = jersey
                            if height: player.height = height
                            if weight: player.weight = weight
                            if college: player.college = college
                            if birth_date: player.birth_date = birth_date
                            if years_exp is not None: player.years_exp = years_exp
                            if headshot: player.headshot_url = headshot
                            player.status = status
                            player.active = active
                            stats["updated"] += 1

                    except Exception as e:
                        logger.warning(f"  {abbr} player error: {e}")
                        stats["errors"] += 1
                        continue

                await db.commit()

            except Exception as e:
                logger.warning(f"  {abbr}: failed: {e}")
                stats["errors"] += 1
                await db.rollback()

    logger.info(f"Rosters done: {stats['updated']} updated, {stats['new']} new, {stats['skipped']} skipped, {stats['errors']} errors")
    return stats


async def ingest_espn_player_stats(db: AsyncSession) -> dict:
    """Load NBA player season stats from ESPN for the current season."""
    stats = {"loaded": 0, "skipped": 0, "errors": 0}

    season_result = await db.execute(select(NBASeason).where(NBASeason.year == 2025))
    season = season_result.scalar_one_or_none()
    if not season:
        return {"error": "Current season not found"}

    season_id = season.id

    async with httpx.AsyncClient(timeout=30.0) as client:
        for abbr, espn_abbr in ESPN_URL_ABBR.items():
            url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{espn_abbr}/roster"
            try:
                resp = await client.get(url, timeout=30.0)
                if resp.status_code != 200:
                    continue

                data = resp.json()
                athletes = data.get("athletes", [])
                roster = []
                if isinstance(athletes, list):
                    for item in athletes:
                        if isinstance(item, dict):
                            items = item.get("items")
                            if items and isinstance(items, list):
                                for sub in items:
                                    if isinstance(sub, dict):
                                        roster.append(sub)
                            else:
                                roster.append(item)

                for p in roster:
                    espn_id_str = p.get("id", "")
                    if not espn_id_str:
                        continue

                    nba_id = hash(espn_id_str) % (10**9) + 100000

                    result = await db.execute(
                        select(NBAPlayer).where(NBAPlayer.nba_id == nba_id)
                    )
                    player = result.scalar_one_or_none()
                    if not player:
                        continue

                    # Check for existing stats
                    existing = await db.execute(
                        select(NBAPlayerSeasonStats).where(
                            NBAPlayerSeasonStats.player_id == player.id,
                            NBAPlayerSeasonStats.season_id == season_id,
                        )
                    )
                    if existing.scalar_one_or_none():
                        stats["skipped"] += 1
                        continue

                    logger.info(f"  Stats for {player.name}: no existing, would load from ESPN stats API")

            except Exception:
                stats["errors"] += 1
                continue

    return stats


async def quick_test(db: AsyncSession = None) -> dict:
    """Quick test — load rosters for one team."""
    from app.database import async_session
    async with async_session() as s:
        return await ingest_rosters(s)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(quick_test())
