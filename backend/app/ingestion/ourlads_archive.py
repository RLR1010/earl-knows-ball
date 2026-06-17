"""
Scrape historical depth charts from Ourlads archives (2007-2025).

Ourlads has archived depth chart snapshots at ~3 month intervals.
Archive URL: https://www.ourlads.com/nfldepthcharts/archive/{id}/{abbr}

Usage:
    python -m app.ingestion.ourlads_archive --all
    python -m app.ingestion.ourlads_archive --snapshot 200 210 220
"""
import asyncio, logging, re
from datetime import datetime, timezone, date

import httpx
from sqlalchemy import select, text

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('earl.ourlads_archive')

BASE = "https://www.ourlads.com/nfldepthcharts/archive"

# Ourlads uses different abbreviations for some teams
OURLADS_ABBR = {"ARI": "ARZ"}

POSITION_MAP = {
    "LWR": "WR", "RWR": "WR", "SWR": "WR",
    "LT": "OT", "RT": "OT", "LG": "OG", "RG": "OG",
    "LDE": "DE", "RDE": "DE", "LDT": "DT", "RDT": "DT", "NT": "DT",
    "WLB": "LB", "MLB": "LB", "SLB": "LB",
    "LCB": "CB", "RCB": "CB", "NB": "CB",
    "SS": "S", "FS": "S",
    "PT": "P", "PK": "K", "H": "P", "KO": "K",
}

# Snapshot IDs with known dates (from earlier discovery)
SNAPSHOT_IDS = {
    15: (2007, 7, 1), 20: (2007, 12, 1),
    25: (2008, 5, 1), 30: (2008, 11, 1),
    35: (2009, 4, 1), 40: (2009, 9, 6),
    105: (2010, 3, 1), 110: (2010, 8, 1),
    115: (2011, 1, 1), 120: (2011, 6, 1), 125: (2011, 11, 1),
    130: (2012, 4, 1), 135: (2012, 9, 1),
    140: (2013, 2, 1), 145: (2013, 6, 5), 150: (2013, 9, 5),
    155: (2014, 2, 1), 160: (2014, 7, 1), 165: (2014, 11, 1),
    170: (2015, 4, 1), 175: (2015, 9, 1),
    180: (2016, 2, 1), 185: (2016, 7, 1), 190: (2016, 12, 1),
    195: (2017, 5, 1), 200: (2017, 10, 1),
    205: (2018, 2, 1), 210: (2018, 7, 1), 215: (2018, 12, 1),
    220: (2019, 5, 1), 225: (2019, 10, 1),
    230: (2020, 3, 1), 235: (2020, 8, 2),
    245: (2021, 5, 2), 250: (2021, 10, 1),
    255: (2022, 3, 1), 260: (2022, 7, 2), 265: (2022, 12, 1),
    270: (2023, 4, 27), 275: (2023, 9, 1),
    280: (2024, 2, 1), 285: (2024, 6, 1), 290: (2024, 11, 1),
    295: (2025, 4, 1), 300: (2025, 8, 1),
}


def _parse_table(table_html: str, snapshot_id: int, snap_date: date) -> list[dict]:
    """Parse a single depth chart table from the archive page.

    Archive pages have white/grey alternating rows per position.
    Player names are plain text in <td> cells (no <a> links like current season).
    Format: "Last, First AcqCode" e.g. "Thielen, Adam CF13"
    """
    entries = []
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL)
    current_pos = None
    current_line = 0

    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        if len(cells) < 3:
            continue

        # Strip HTML from first cell to get position
        first = re.sub(r"<[^>]+>", "", cells[0]).strip()

        # Skip header rows
        if first in ("Pos", "No.", "Player", ""):
            continue
        # Skip section headers like "Offense", "Defense", "Special Teams", "ST"
        if len(first) > 5 and not any(c.isdigit() for c in first):
            continue
        # Skip mobile-only rows (have colspan=11)
        if "colspan" in cells[0].lower() and not first:
            continue

        std_pos = current_pos
        if first and len(first) <= 4:
            # New position header
            current_pos = POSITION_MAP.get(first, first)
            current_line = 0
            std_pos = current_pos
        elif current_pos:
            # Continuation row for same position
            current_line += 1
        else:
            continue

        if len(std_pos) > 5:
            continue

        # Player cells come in pairs: [jersey, player_name]
        for i in range(1, len(cells) - 1, 2):
            # Get jersey number
            jersey_raw = re.sub(r"<[^>]+>", "", cells[i]).strip()
            try:
                jersey = int(jersey_raw) if jersey_raw else None
            except ValueError:
                jersey = None

            # Get player name cell
            player_raw = re.sub(r"<[^>]+>", "", cells[i + 1]).strip() if i + 1 < len(cells) else ""
            if not player_raw or player_raw in ("-", "&nbsp;", ""):
                continue

            # Parse "Last, First AcqCode" format
            name = player_raw
            acq = None

            # Check for acquisition code at end
            parts = name.rsplit(None, 1)
            if len(parts) == 2:
                code = parts[1]
                if re.match(r"^\d{2}/\d+$", code) or \
                   re.match(r"^(SF|FA|CF|CC/|T/|W/|P/)[\d/A-Za-z]+", code):
                    acq = code
                    name = parts[0]

            # Normalize name: "Last, First" -> "First Last"
            if ", " in name:
                parts = name.split(", ", 1)
                name = f"{parts[1]} {parts[0]}"

            pair = (i - 1) // 2
            slot = current_line * 2 + pair + 1

            entries.append({
                "snapshot_id": snapshot_id,
                "snapshot_date": snap_date,
                "position": std_pos,
                "slot": slot,
                "player_name": name,
                "jersey_number": jersey,
                "acquisition_info": acq,
            })

    return entries


async def scrape_snapshot(db, snapshot_id: int, team_abbrs: list[str],
                          team_id_map: dict[str, int]) -> int:
    """Scrape all teams for a single archive snapshot. Returns entries loaded."""
    snap_date = date(*SNAPSHOT_IDS[snapshot_id])
    total = 0

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        for abbr in team_abbrs:
            oa = OURLADS_ABBR.get(abbr, abbr)
            url = f"{BASE}/{snapshot_id}/{oa}"
            try:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                html = resp.text
            except Exception as e:
                logger.warning(f"  {abbr} (ID {snapshot_id}): {e}")
                continue

            team_id = team_id_map[abbr]
            tables = re.findall(r"<table[^>]*>(.*?)</table>", html, re.DOTALL)

            entries = []
            for table in tables:
                entries.extend(_parse_table(table, snapshot_id, snap_date))

            for e in entries:
                e["team_id"] = team_id

            if entries:
                await db.execute(
                    text("""
                        INSERT INTO nfl.depth_charts_archive
                        (snapshot_id, snapshot_date, team_id, position, slot,
                         player_name, jersey_number, acquisition_info)
                        VALUES (:snapshot_id, :snapshot_date, :team_id, :position, :slot,
                                :player_name, :jersey_number, :acquisition_info)
                    """),
                    entries,
                )
                await db.flush()
                total += len(entries)

    return total


async def run():
    import argparse, os, sys
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Scrape all snapshots")
    group.add_argument("--snapshot", type=int, nargs="+", help="Specific snapshot IDs")
    group.add_argument("--year", type=int, help="All snapshots for a specific year")
    args = parser.parse_args()

    from app.database import async_session

    # Get team list
    async with async_session() as db:
        r = await db.execute(text("SELECT abbreviation, id FROM nfl.teams ORDER BY id"))
        teams = {row.abbreviation: row.id for row in r.fetchall()}
    team_abbrs = list(teams.keys())
    logger.info(f"Teams: {len(team_abbrs)}")

    # Select snapshots
    if args.snapshot:
        snapshots = sorted(set(args.snapshot) & set(SNAPSHOT_IDS.keys()))
    elif args.year:
        snapshots = sorted(sid for sid, (y, m, d) in SNAPSHOT_IDS.items() if y == args.year)
    else:
        snapshots = sorted(SNAPSHOT_IDS.keys())

    logger.info(f"Snapshots to scrape: {len(snapshots)}")
    logger.info(f"Teams per snapshot: {len(team_abbrs)}")

    # Clear existing archive data for these snapshots
    async with async_session() as db:
        for sid in snapshots:
            await db.execute(
                text("DELETE FROM nfl.depth_charts_archive WHERE snapshot_id = :sid"),
                {"sid": sid},
            )
        await db.commit()
    logger.info("Cleared existing archive data for target snapshots")

    total_entries = 0
    for sid in sorted(snapshots):
        y, m, d = SNAPSHOT_IDS[sid]
        logger.info(f"Snapshot {sid}: {y}-{m:02d}-{d:02d}")
        async with async_session() as db:
            n = await scrape_snapshot(db, sid, team_abbrs, teams)
            await db.commit()
        total_entries += n
        logger.info(f"  Total for this snapshot: {n} entries")

    logger.info(f"\nDone! {total_entries} total entries loaded across {len(snapshots)} snapshots")


if __name__ == "__main__":
    asyncio.run(run())
