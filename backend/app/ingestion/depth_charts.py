"""
Ourlads NFL depth chart scraper.
Pulls depth charts + free agent transactions for all 32 teams.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, Player, DepthChart, Transaction

logger = logging.getLogger("earl.depth_charts")

OURlADS_BASE = "https://www.ourlads.com/nfldepthcharts"

# Map Ourlads position codes to standard
POSITION_MAP = {
    "LWR": "WR", "RWR": "WR", "SWR": "WR",
    "LT": "OT", "RT": "OT",
    "LG": "OG", "RG": "OG",
    "LDE": "DE", "RDE": "DE",
    "LDT": "DT", "RDT": "DT", "NT": "DT",
    "WLB": "LB", "MLB": "LB", "SLB": "LB",
    "LCB": "CB", "RCB": "CB", "NB": "CB",
    "SS": "S", "FS": "S",
    "PT": "P", "PK": "K", "LS": "LS", "H": "P", "KO": "K",
    
}


def _parse_acquisition(code: str) -> tuple[str, str]:
    """Parse acquisition code like '23/3', 'FA25', 'SF25', 'CF26', 'T/NO'."""
    if not code:
        return ("", "")
    code = code.strip()
    if code.startswith("CF"):  # CFL/UDFA
        return ("udfa", code)
    if code.startswith("SF") or code.startswith("FA"):  # Free agent signing
        return ("fa_acq", code)
    if code.startswith("T/"):  # Trade
        return ("trade", code)
    if code.startswith("CC/"):  # Claimed
        return ("claimed", code)
    if code.startswith("W/"):  # Waivers
        return ("waivers", code)
    if re.match(r"^\d{2}/\d+$", code):  # Draft pick: e.g. 23/3 = 2023 round 3
        return ("drafted", code)
    if code == "UDFA":
        return ("udfa", code)
    return ("", code)


async def _parse_depth_table(table_html: str, team_id: int) -> list[dict]:
    """Parse a single Ourlads depth chart table into structured entries.
    
    Ourlads uses consecutive <tr> elements for each position:
      Row 1: [Pos] | [Jersey 1] | [Player 1] | [Jersey 2] | [Player 2]
      Row 2: [same Pos/empty] | [Jersey 3] | [Player 3] | [Jersey 4] | [Player 4]
    
    Players in row 1 are above those in row 2 (row 1 = starter line).
    Slot numbers are continuous across rows for the same position.
    
    Player link format: <a href='...' class='...'>Last, First AcqCode</a>
    """
    entries = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
    current_pos = None
    current_line = 0

    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        if len(cells) < 3:
            continue

        # First cell is the position label (or empty for continuation rows)
        pos_raw = re.sub(r"<[^>]+>", "", cells[0]).strip()
        pos = pos_raw if pos_raw else current_pos
        if not pos or len(pos) > 5 or pos in ("Pos", "No.", "Player"):
            continue

        if pos_raw:
            # New position header row — start a new line
            current_pos = pos
            current_line = 0
        else:
            # Continuation row (no position label) — next line of same position
            current_line += 1

        std_pos = POSITION_MAP.get(pos, pos)
        
        # Cells after position come in pairs: [jersey, player_link]
        for i in range(1, len(cells) - 1, 2):
            jersey_cell = cells[i]
            player_cell = cells[i + 1] if i + 1 < len(cells) else ""

            # Get the player link content
            link_match = re.search(r"<a[^>]*>(.*?)</a>", player_cell)
            if not link_match:
                continue

            link_text = link_match.group(1).strip()
            if not link_text or link_text == "-" or "&nbsp;" in link_text:
                continue

            # Parse acquisition code from end of name
            acq_code = ""
            name_text = link_text
            parts = name_text.rsplit(None, 1)
            if len(parts) == 2:
                potential_acq = parts[1]
                if re.match(r"^[\d]{2}/[\d]+$", potential_acq) or \
                   re.match(r"^(SF|FA|CF|CC/|T/|W/)[\d/A-Za-z]+", potential_acq) or \
                   potential_acq == "UDFA":
                    acq_code = potential_acq
                    name_text = parts[0]

            # Parse jersey number
            jersey = None
            j_match = re.search(r"^(\d+)$", re.sub(r"<[^>]+>", "", jersey_cell).strip())
            if j_match:
                jersey = int(j_match.group(1))

            # Determine status from CSS class
            status = "active"
            class_match = re.search(r'class="([^"]*)"', player_cell)
            if class_match:
                css = class_match.group(1)
                if "lc_gold" in css:
                    status = "fa_acq"
                elif "lc_purple" in css:
                    status = "rookie"
                elif "lc_aqua" in css:
                    status = "udfa"
                elif "lc_red" in css:
                    status = "injured"

            # Slot = (line_number * 2) + pair_number + 1
            pair_number = (i - 1) // 2
            slot = current_line * 2 + pair_number + 1

            entries.append({
                "team_id": team_id,
                "position": std_pos,
                "slot": slot,
                "player_name": name_text,
                "jersey_number": jersey,
                "acquisition_info": acq_code or None,
                "status": status,
            })

    return entries


async def scrape_team_depth_chart(db: AsyncSession, team_abbr: str) -> dict:
    """Scrape depth chart for a single team from Ourlads."""
    # Resolve team
    r = await db.execute(select(Team).where(Team.abbreviation == team_abbr.upper()))
    team = r.scalar_one_or_none()
    if not team:
        return {"error": f"Team {team_abbr} not found"}

    # Ourlads uses different abbreviations than our DB for some teams
    ourlads_abbr = {
        "ARI": "ARZ",
    }.get(team_abbr.upper(), team_abbr.upper())
    url = f"{OURlADS_BASE}/depthchart/{ourlads_abbr}"
    logger.info(f"Fetching depth chart: {team.name} → {url}")

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.error(f"Failed to fetch {url}: {e}")
        return {"error": str(e)}

    # Find all tables
    tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)
    all_entries = []
    for table in tables:
        entries = await _parse_depth_table(table, team.id)
        all_entries.extend(entries)

    # Delete old depth chart entries for this team
    await db.execute(
        DepthChart.__table__.delete().where(DepthChart.team_id == team.id)
    )

    # Insert new entries
    for entry in all_entries:
        # Try to match player by name
        player = None
        # Ourlads uses "Last, First" format — normalize to "First Last"
        if ", " in entry["player_name"]:
            parts = entry["player_name"].split(", ", 1)
            search_name = f"{parts[1]} {parts[0]}"
        else:
            search_name = entry["player_name"]

        # Look up player in our DB (first match only; names can collide)
        pr = await db.execute(
            select(Player).where(Player.name.ilike(f"%{search_name.split()[0]}%"),
                                  Player.name.ilike(f"%{search_name.split()[-1]}%"))
        )
        player = pr.scalars().first()

        dc = DepthChart(
            team_id=entry["team_id"],
            position=entry["position"],
            slot=entry["slot"],
            player_id=player.id if player else None,
            player_name=entry["player_name"],
            jersey_number=entry["jersey_number"],
            acquisition_info=entry["acquisition_info"],
            status=entry["status"],
        )
        db.add(dc)

    await db.commit()
    return {"team": team.name, "entries": len(all_entries)}


async def scrape_all_teams(db: AsyncSession) -> dict:
    """Scrape depth charts for all 32 teams."""
    r = await db.execute(select(Team).order_by(Team.id))
    teams = r.scalars().all()

    results = {"total": 0, "teams": []}
    for team in teams:
        if not team.abbreviation:
            continue
        result = await scrape_team_depth_chart(db, team.abbreviation)
        results["teams"].append(result)
        results["total"] += result.get("entries", 0)

    return results


async def parse_acquisition_status(status: str) -> str:
    """Convert depth chart status to human-readable."""
    status_map = {
        "fa_acq": "Free agent acquisition",
        "rookie": "2026 rookie draft pick",
        "udfa": "Undrafted free agent",
        "drafted": "Previously drafted",
        "trade": "Acquired via trade",
        "injured": "Injured/inactive",
        "active": "Active",
    }
    return status_map.get(status, status)
