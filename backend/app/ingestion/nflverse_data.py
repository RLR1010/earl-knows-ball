"""
Ingest missing data sources from nflverse: draft info, injuries, and transactions.

Data sources:
  - players.csv → GSIS ID → PFR ID mapping, draft info (year/round/pick/team)
  - draft_picks.csv → detailed draft pick history (not used directly, players.csv has it)
  - injuries_{year}.csv → weekly injury reports per player (2009-2025)
  - trades.csv → trade transactions (mapped to players via PFR ID)
"""
import csv
import io
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, Injury, Transaction, Team, Season

logger = logging.getLogger("earl.nflverse_data")

NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"

# Maps GSIS position to our position format
POSITION_MAP = {
    "QB": "QB", "RB": "RB", "WR": "WR", "TE": "TE",
    "K": "K", "P": "K", "LS": "K", "DB": "WR",
    "CB": "WR", "S": "WR", "LB": "RB", "DE": "TE", "DT": "TE",
    "NT": "TE", "OL": "TE", "G": "TE", "T": "TE", "C": "TE",
    "EDGE": "TE",
}


async def _download_csv(url: str) -> list[dict]:
    """Download a CSV and return list of dicts."""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = resp.text
        reader = csv.DictReader(io.StringIO(text))
        return list(reader)


async def _get_season_id(db: AsyncSession, year: int) -> int | None:
    """Get or create a season record and return its ID."""
    r = await db.execute(select(Season).where(Season.year == year))
    season = r.scalar_one_or_none()
    if season:
        return season.id
    s = Season(year=year)
    db.add(s)
    await db.flush()
    return s.id


async def _get_team_id(db: AsyncSession, abbr: str) -> int | None:
    """Look up a team by abbreviation. Returns None if not found."""
    r = await db.execute(select(Team).where(Team.abbreviation == abbr.upper()))
    team = r.scalar_one_or_none()
    return team.id if team else None


async def _build_gsis_to_player(db: AsyncSession) -> dict[str, Player]:
    """Build a mapping of nflverse_id (GSIS ID) to Player objects."""
    r = await db.execute(select(Player))
    mapping = {}
    for p in r.scalars().all():
        if p.nflverse_id:
            mapping[p.nflverse_id] = p
    return mapping


async def ingest_draft_info(db: AsyncSession) -> dict:
    """Load draft info from nflverse players.csv and update our players table."""
    logger.info("Downloading nflverse players data...")
    rows = await _download_csv(f"{NFLVERSE_BASE}/players/players.csv")
    logger.info(f"Downloaded {len(rows)} player records")

    updated = 0
    skipped = 0
    errors = 0

    for row in rows:
        gsis = (row.get("gsis_id") or "").strip()
        if not gsis:
            skipped += 1
            continue

        # Find our player by nflverse_id (prefer active players over phantom duplicates)
        r = await db.execute(
            select(Player)
            .where(Player.nflverse_id == gsis)
            .order_by(Player.team_id.isnot(None).desc(), Player.id)
            .limit(1)
        )
        player = r.scalar_one_or_none()
        if not player:
            skipped += 1
            continue

        # Update with draft info from nflverse
        draft_year = row.get("draft_year")
        draft_round = row.get("draft_round")
        draft_pick = row.get("draft_pick")
        draft_team = row.get("draft_team")

        has_update = False
        if draft_year:
            player.draft_year = int(draft_year)
            has_update = True
        if draft_round and draft_round != "0":
            player.draft_round = int(draft_round)
            has_update = True
        if draft_pick and draft_pick != "0":
            player.draft_pick = int(draft_pick)
            has_update = True
        if draft_team:
            player.draft_team = draft_team.upper()
            has_update = True

        if has_update:
            updated += 1
            if updated % 500 == 0:
                await db.flush()

    await db.commit()
    logger.info(f"Draft info: {updated} updated, {skipped} skipped, {errors} errors")
    return {"updated": updated, "skipped": skipped, "errors": errors}


async def ingest_injuries(db: AsyncSession, years: list[int] | None = None) -> dict:
    """Load injury data from nflverse for given years."""
    if years is None:
        years = list(range(2020, 2026))

    player_map = await _build_gsis_to_player(db)
    logger.info(f"Built player map: {len(player_map)} players with GSIS IDs")

    total_loaded = 0
    total_skipped = 0
    errors = 0

    for year in years:
        try:
            rows = await _download_csv(f"{NFLVERSE_BASE}/injuries/injuries_{year}.csv")
            logger.info(f"  {year}: {len(rows)} injury records downloaded")

            season_id = await _get_season_id(db, year)
            loaded = 0
            skipped = 0

            for row in rows:
                gsis = (row.get("gsis_id") or "").strip()
                if not gsis:
                    skipped += 1
                    continue

                player = player_map.get(gsis)
                if not player:
                    skipped += 1
                    continue

                week_str = row.get("week", "")
                try:
                    week = int(week_str)
                except (ValueError, TypeError):
                    continue

                report_injury = row.get("report_primary_injury") or ""
                report_status = row.get("report_status") or ""
                practice_injury = row.get("practice_primary_injury") or ""
                practice_status = (row.get("practice_status") or "")[:50]
                gs = report_status[:50] if report_status else (practice_status[:50] if practice_status else "")

                injury = Injury(
                    player_id=player.id,
                    week=week,
                    season_id=season_id,
                    injury_type=(report_injury or practice_injury or "Unknown")[:100],
                    practice_status=practice_status,
                    game_status=gs,
                )
                db.add(injury)
                loaded += 1

                if loaded % 200 == 0:
                    await db.flush()

            await db.commit()
            total_loaded += loaded
            total_skipped += skipped
            logger.info(f"  {year}: {loaded} loaded, {skipped} skipped")

        except Exception as e:
            await db.rollback()
            logger.error(f"  {year}: error — {e}")
            errors += 1

    return {"loaded": total_loaded, "skipped": total_skipped, "errors": errors}


async def ingest_trades(db: AsyncSession) -> dict:
    """Load trade data from nflverse and map to players via PFR ID."""
    logger.info("Downloading nflverse trades data...")
    rows = await _download_csv(f"{NFLVERSE_BASE}/trades/trades.csv")
    logger.info(f"Downloaded {len(rows)} trade records")

    # Build PFR ID → Player mapping from our players
    r = await db.execute(select(Player))
    players = {p.nflverse_id: p for p in r.scalars().all() if p.nflverse_id}

    # We also need nflverse players.csv for PFR ID ↔ GSIS ID mapping
    pfr_to_gsis = {}
    try:
        p_rows = await _download_csv(f"{NFLVERSE_BASE}/players/players.csv")
        for pr in p_rows:
            gsis = (pr.get("gsis_id") or "").strip()
            pfr = (pr.get("pfr_id") or "").strip()
            if gsis and pfr:
                pfr_to_gsis[pfr] = gsis
        logger.info(f"Built PFR→GSIS map: {len(pfr_to_gsis)} mappings")
    except Exception as e:
        logger.error(f"Failed to download players.csv for PFR mapping: {e}")
        return {"loaded": 0, "errors": 1}

    loaded = 0
    skipped = 0
    errors = 0

    for row in rows:
        try:
            pfr_id = (row.get("pfr_id") or "").strip()
            if not pfr_id:
                skipped += 1
                continue

            gsis = pfr_to_gsis.get(pfr_id)
            if not gsis:
                skipped += 1
                continue

            player = players.get(gsis)
            if not player:
                skipped += 1
                continue

            # Parse trade data
            trade_date_str = row.get("trade_date", "")
            trade_date = None
            if trade_date_str:
                try:
                    trade_date = datetime.strptime(trade_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                except ValueError:
                    trade_date = datetime.now(timezone.utc)

            gave = row.get("gave", "")
            received = row.get("received", "")

            # Determine from/to teams from the gave/received fields
            from_team_name = ""
            to_team_name = ""
            details = f"Gave: {gave}, Received: {received}"

            # Transaction type — default to trade
            transaction_type = "Trade"

            # Map team names in gave/received to abbreviations
            team_names = {
                "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL",
                "Baltimore Ravens": "BAL", "Buffalo Bills": "BUF",
                "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
                "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE",
                "Dallas Cowboys": "DAL", "Denver Broncos": "DEN",
                "Detroit Lions": "DET", "Green Bay Packers": "GB",
                "Houston Texans": "HOU", "Indianapolis Colts": "IND",
                "Jacksonville Jaguars": "JAX", "Kansas City Chiefs": "KC",
                "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
                "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA",
                "Minnesota Vikings": "MIN", "New England Patriots": "NE",
                "New Orleans Saints": "NO", "New York Giants": "NYG",
                "New York Jets": "NYJ", "Philadelphia Eagles": "PHI",
                "Pittsburgh Steelers": "PIT", "San Francisco 49ers": "SF",
                "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
                "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
            }

            # Attempt to determine from/to teams
            tx = Transaction(
                player_id=player.id,
                player_name=player.name,
                position=player.position,
                transaction_type=transaction_type,
                details=details,
                source="nflverse",
                source_url=f"https://github.com/nflverse/nflverse-data",
                transaction_date=trade_date or datetime.now(timezone.utc),
            )
            db.add(tx)
            loaded += 1

            if loaded % 200 == 0:
                await db.flush()

        except Exception as e:
            logger.error(f"Error processing trade row: {e}")
            errors += 1

    await db.commit()
    logger.info(f"Trades: {loaded} loaded, {skipped} skipped, {errors} errors")
    return {"loaded": loaded, "skipped": skipped, "errors": errors}
