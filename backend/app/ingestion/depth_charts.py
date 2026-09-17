"""
Ourlads NFL depth chart scraper.
Pulls depth charts + free agent transactions for all 32 teams.
"""
import logging
import re
import unicodedata
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
    "LOLB": "LB", "ROLB": "LB", "LILB": "LB", "RILB": "LB", "RUSH": "LB",
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


_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v", "vi"}


def _norm_name(value: str) -> str:
    """Lowercase, strip accents + punctuation so names match across sources."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _split_last_first(raw: str) -> tuple[str, str]:
    """Ourlads names are 'Last, First' (e.g. 'Tracy Jr., Tyrone')."""
    raw = (raw or "").strip()
    if "," in raw:
        last, first = raw.split(",", 1)
        return first.strip(), last.strip()
    parts = raw.split()
    if len(parts) < 2:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _name_key(raw: str) -> tuple[str, str]:
    """Normalized (first-token, surname-token); suffixes like Jr./II dropped."""
    first, last = _split_last_first(raw)
    last_tokens = [t for t in _norm_name(last).split() if t and t not in _SUFFIX_TOKENS]
    first_tokens = [t for t in _norm_name(first).split() if t]
    return (first_tokens[0] if first_tokens else ""), (last_tokens[-1] if last_tokens else "")


async def _player_name_index(db: AsyncSession):
    """Build normalized lookup index of stored players (once per team-scrape)."""
    rows = (await db.execute(select(Player.id, Player.name))).all()
    by_full: dict[tuple[str, str], int] = {}
    by_initial: dict[tuple[str, str], int] = {}
    by_surname: dict[str, list[int]] = {}
    for pid, pname in rows:
        toks = _norm_name(pname).split()
        if len(toks) < 2:
            continue
        first_tok, last_tok = toks[0], toks[-1]
        by_full.setdefault((first_tok, last_tok), pid)
        by_initial.setdefault((first_tok[0], last_tok), pid)
        by_surname.setdefault(last_tok, []).append(pid)
    return by_full, by_initial, by_surname


def _match_player_id(by_full, by_initial, by_surname, raw_name: str):
    first_tok, last_tok = _name_key(raw_name)
    if not last_tok:
        return None
    if (first_tok, last_tok) in by_full:
        return by_full[(first_tok, last_tok)]
    if first_tok and (first_tok[0], last_tok) in by_initial:
        return by_initial[(first_tok[0], last_tok)]
    cands = by_surname.get(last_tok)
    if cands and len(cands) == 1:
        return cands[0]
    return None


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


INCLUDE_DEPTH_SECTIONS = ("offense", "defense", "special teams")
EXCLUDE_DEPTH_SECTIONS = ("practice", "reserve", "suffix", "coaching", "inactive")


def _section_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", title)).strip().lower()


def _depth_chart_tables(html: str) -> list[str]:
    """Return only the REAL depth-chart tables (Offense/Defense/Special Teams).

    The Ourlads team page also contains "Practice Squad", "Reserves" and "Suffix
    Key" tables. Those are NOT starters; parsing the Practice Squad table was
    writing a practice-squad QB at slot 1 (shown as the starting QB).
    """
    tables: list[str] = []
    section = ""
    for m in re.finditer(
        r"<h[1-5][^>]*>(.*?)</h[1-5]>|<table[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE
    ):
        if m.group(1) is not None:
            section = _section_key(m.group(1))
            continue
        if any(k in section for k in EXCLUDE_DEPTH_SECTIONS):
            continue
        if not any(k in section for k in INCLUDE_DEPTH_SECTIONS):
            continue
        tables.append(m.group(2))
    # Safety net: if heading detection ever fails, the real sections are the first
    # three tables in document order (Offense, Defense, Special Teams).
    if not tables:
        tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)[:3]
    return tables


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

    # Only the real depth-chart sections. NOTE: several Ourlads columns collapse to the
    # same canonical position (LWR/RWR/SWR -> WR, LT/RT -> OT, LG/RG -> OG, LDE/RDE -> DE,
    # LCB/RCB/NB -> CB, SS/FS -> S), and each column restarts its own slot numbering — so
    # MULTIPLE rows per (position, slot) are expected (e.g. three starting WRs). Only drop
    # exact duplicates: same position + slot + player (e.g. the punter also listed as the
    # holder, or the kicker also listed as the kickoff specialist).
    all_entries = []
    seen = set()  # (position, slot, player_name)
    for table in _depth_chart_tables(html):
        for entry in await _parse_depth_table(table, team.id):
            key = (entry["position"], entry["slot"], entry["player_name"])
            if key in seen:
                continue
            seen.add(key)
            all_entries.append(entry)

    # Delete old depth chart entries for this team
    await db.execute(
        DepthChart.__table__.delete().where(DepthChart.team_id == team.id)
    )

    # Insert new entries
    index_full, index_initial, index_surname = await _player_name_index(db)
    for entry in all_entries:
        player_id = _match_player_id(
            index_full, index_initial, index_surname, entry["player_name"]
        )

        dc = DepthChart(
            team_id=entry["team_id"],
            position=entry["position"],
            slot=entry["slot"],
            player_id=player_id,
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
