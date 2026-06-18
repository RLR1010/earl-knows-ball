"""
NBA game schedule ingestion from ESPN API.

Fetches all NBA games (preseason, regular season, playoffs/play-in)
for any season from 2006 onward and writes them into nba.games.

Uses weekly (7-day) date-range chunks because the ESPN scoreboard
API silently caps responses at 100 events and pagination is unreliable.
A typical NBA week has ~10-15 games, well under the 100 limit.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from dateutil import parser
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBAGame, NBASeason, NBATeam

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# ESPN abbreviation → DB abbreviation mapping
ESPN_TEAM_MAP = {
    "BKN": "BKN",
    "BK": "BKN",
    "PHO": "PHX",
    "WSH": "WAS",
    "GS": "GSW",
    "NY": "NYK",
    "SA": "SAS",
    "NO": "NOP",
    "UTAH": "UTA",
    "CHA": "CHO",
    "CHI": "CHI",
    "CLE": "CLE",
    "DAL": "DAL",
    "DEN": "DEN",
    "DET": "DET",
    "HOU": "HOU",
    "IND": "IND",
    "LAC": "LAC",
    "LAL": "LAL",
    "MEM": "MEM",
    "MIA": "MIA",
    "MIL": "MIL",
    "MIN": "MIN",
    "NOP": "NOP",
    "OKC": "OKC",
    "ORL": "ORL",
    "PHI": "PHI",
    "PHX": "PHX",
    "POR": "POR",
    "SAC": "SAC",
    "SAS": "SAS",
    "TOR": "TOR",
    "UTA": "UTA",
    "WAS": "WAS",
    # Retired / relocated
    "SEA": "SEA",
    "VAN": "VAN",
    "NJ": "NJN",
    "NJN": "NJN",
    "NOH": "NOP",
    "NOK": "NOP",
    "CHA_2": "CHO",
    "CHB": "CHO",
    "CHH": "CHO",
    "BOB": "CHO",
}

# NBA season date ranges (start, end) – covers preseason + playoffs
NBA_SEASON_RANGES = {
    2006: ("20061001", "20070630"),
    2007: ("20071001", "20080630"),
    2008: ("20081001", "20090630"),
    2009: ("20091001", "20100630"),
    2010: ("20101001", "20110630"),
    2011: ("20111001", "20120630"),
    2012: ("20121001", "20130630"),
    2013: ("20131001", "20140630"),
    2014: ("20141001", "20150630"),
    2015: ("20151001", "20160630"),
    2016: ("20161001", "20170630"),
    2017: ("20171001", "20180630"),
    2018: ("20181001", "20190630"),
    2019: ("20191001", "20200630"),
    2020: ("20201201", "20210730"),  # pandemic-shifted
    2021: ("20211001", "20220630"),
    2022: ("20221001", "20230630"),
    2023: ("20231001", "20240630"),
    2024: ("20241001", "20250630"),
    2025: ("20251001", "20260630"),
    2026: ("20261001", "20270630"),
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_week_chunks(start_str: str, end_str: str) -> list[tuple[str, str]]:
    """Split a date range into 7-day chunks."""
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    chunks = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=6), end)
        chunks.append((cursor.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _parse_game_type(season_type: Optional[int]) -> str:
    """Map ESPN season type to our game_type enum."""
    if season_type == 1:
        return "PRE"
    elif season_type == 2:
        return "REG"
    elif season_type == 3:
        return "POST"
    else:
        return "REG"


def _map_espn_status(status: str) -> str:
    """Map ESPN status to our simplified status."""
    if "POSTPONED" in status:
        return "postponed"
    elif "CANCEL" in status:
        return "cancelled"
    elif "DELAY" in status:
        return "postponed"
    elif "STATUS_IN_PROGRESS" in status or status == "STATUS_HALFTIME":
        return "in_progress"
    elif "STATUS_FINAL" in status or "STATUS_END" in status:
        return "final"
    elif "STATUS_SCHEDULED" in status or "STATUS_PREGAME" in status or "STATUS_WARMUP" in status:
        return "scheduled"
    return "scheduled"


def _safe_int(val) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _extract_stat(stats: list[dict], name: str) -> Optional[float]:
    for s in stats:
        if s.get("name") == name:
            return _safe_int(s.get("displayValue"))
    return None


def _game_type_label(gt: str) -> str:
    labels = {"PRE": "preseason", "REG": "regular", "POST": "postseason"}
    return labels.get(gt, gt)


# ── API Fetching ─────────────────────────────────────────────────────────────

async def _fetch_week(client: httpx.AsyncClient, start_str: str, end_str: str) -> list[dict]:
    """Fetch all events for a 7-day date range."""
    params = {"dates": f"{start_str}-{end_str}", "limit": 100}
    try:
        resp = await client.get(ESPN_BASE, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("events", [])
    except Exception as e:
        logger.warning(f"  Error fetching {start_str}-{end_str}: {e}")
        return []


async def _process_events(
    events: list[dict],
    session: AsyncSession,
    season_id: int,
    team_by_abbr: dict,
) -> dict:
    """Process a batch of ESPN events and insert games into the DB.

    Returns counts for loaded, skipped, errors, and by_type within this batch.
    """
    loaded = 0
    skipped = 0
    errors = 0
    by_type = {"PRE": 0, "REG": 0, "POST": 0}

    for event in events:
        try:
            event_id_str = event.get("id", "")
            if not event_id_str:
                continue

            # Check for duplicate by nba_game_id
            existing = await session.execute(
                select(NBAGame).where(NBAGame.nba_game_id == event_id_str)
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            competitions = event.get("competitions", [])
            if not competitions:
                continue
            comp = competitions[0]

            # Game type
            season_type_raw = event.get("season", {}).get("type")
            game_type = _parse_game_type(season_type_raw)

            # Home and away competitors
            home_data = next(
                (c for c in comp.get("competitors", []) if c.get("homeAway") == "home"),
                None,
            )
            away_data = next(
                (c for c in comp.get("competitors", []) if c.get("homeAway") == "away"),
                None,
            )
            if not home_data or not away_data:
                continue

            espn_home = home_data.get("team", {}).get("abbreviation", "")
            espn_away = away_data.get("team", {}).get("abbreviation", "")
            if not espn_home or not espn_away:
                continue

            home_abbr = ESPN_TEAM_MAP.get(espn_home, espn_home)
            away_abbr = ESPN_TEAM_MAP.get(espn_away, espn_away)

            home_team = team_by_abbr.get(home_abbr)
            away_team = team_by_abbr.get(away_abbr)
            if not home_team or not away_team:
                errors += 1
                continue

            # Date/time
            date_str_evt = comp.get("date") or event.get("date", "")
            try:
                game_date = parser.parse(date_str_evt)
            except (ValueError, TypeError):
                game_date = datetime.now(timezone.utc)

            # Status
            status_type = (
                comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
            )
            game_status = _map_espn_status(status_type)

            # Venue
            venue_info = comp.get("venue") or {}
            venue_name = venue_info.get("fullName") if venue_info else None

            # Stats
            home_stats = home_data.get("statistics", [])
            away_stats = away_data.get("statistics", [])
            has_stats = any(s.get("name") == "fieldGoalsMade" for s in home_stats)

            # Build the game record
            game = NBAGame(
                nba_game_id=event_id_str,
                season_id=season_id,
                game_type=game_type,
                home_team_id=home_team.id,
                away_team_id=away_team.id,
                date=game_date,
                status=game_status,
                home_score=_safe_int(home_data.get("score")),
                away_score=_safe_int(away_data.get("score")),
                venue=venue_name,
                attendance=_safe_int(comp.get("attendance")),
            )

            if has_stats:
                game.home_field_goals_made = _extract_stat(home_stats, "fieldGoalsMade")
                game.home_field_goals_attempted = _extract_stat(home_stats, "fieldGoalsAttempted")
                game.home_three_points_made = _extract_stat(home_stats, "threePointFieldGoalsMade")
                game.home_three_points_attempted = _extract_stat(home_stats, "threePointFieldGoalsAttempted")
                game.home_free_throws_made = _extract_stat(home_stats, "freeThrowsMade")
                game.home_free_throws_attempted = _extract_stat(home_stats, "freeThrowsAttempted")
                game.home_rebounds = _extract_stat(home_stats, "rebounds")
                game.home_assists = _extract_stat(home_stats, "assists")

                game.away_field_goals_made = _extract_stat(away_stats, "fieldGoalsMade")
                game.away_field_goals_attempted = _extract_stat(away_stats, "fieldGoalsAttempted")
                game.away_three_points_made = _extract_stat(away_stats, "threePointFieldGoalsMade")
                game.away_three_points_attempted = _extract_stat(away_stats, "threePointFieldGoalsAttempted")
                game.away_free_throws_made = _extract_stat(away_stats, "freeThrowsMade")
                game.away_free_throws_attempted = _extract_stat(away_stats, "freeThrowsAttempted")
                game.away_rebounds = _extract_stat(away_stats, "rebounds")
                game.away_assists = _extract_stat(away_stats, "assists")

            session.add(game)
            loaded += 1
            by_type[game_type] = by_type.get(game_type, 0) + 1

        except Exception as e:
            logger.warning(f"  Error processing event {event.get('id', '?')}: {e}")
            errors += 1

    return {"loaded": loaded, "skipped": skipped, "errors": errors, "by_type": by_type}


# ── Main Ingestion Logic ─────────────────────────────────────────────────────

async def ingest_nba_schedule(
    session: AsyncSession,
    season_year: int = 2025,
) -> dict:
    """Load NBA games from ESPN API for a single season using concurrent 7-day chunks."""
    games_loaded = 0
    games_skipped = 0
    errors = 0
    games_by_type = {"PRE": 0, "REG": 0, "POST": 0}

    # Look up or create season
    result = await session.execute(select(NBASeason).where(NBASeason.year == season_year))
    season = result.scalar_one_or_none()
    if not season:
        season = NBASeason(year=season_year)
        session.add(season)
        await session.flush()

    # Get team map
    result = await session.execute(select(NBATeam))
    all_teams = result.scalars().all()
    team_by_abbr = {t.abbreviation: t for t in all_teams}
    # Also add alternate abbreviations
    team_by_abbr["GSW"] = team_by_abbr.get("GS") or team_by_abbr.get("GSW")
    team_by_abbr["PHX"] = team_by_abbr.get("PHO") or team_by_abbr.get("PHX")
    team_by_abbr["NYK"] = team_by_abbr.get("NY") or team_by_abbr.get("NYK")
    team_by_abbr["SAS"] = team_by_abbr.get("SA") or team_by_abbr.get("SAS")
    team_by_abbr["NOP"] = team_by_abbr.get("NO") or team_by_abbr.get("NOP")
    team_by_abbr["UTA"] = team_by_abbr.get("UTAH") or team_by_abbr.get("UTA")
    team_by_abbr["CHO"] = team_by_abbr.get("CHA") or team_by_abbr.get("CHO")
    team_by_abbr["BKN"] = team_by_abbr.get("BK") or team_by_abbr.get("BKN")

    # Build week chunks
    date_range = NBA_SEASON_RANGES.get(
        season_year, (f"{season_year}1001", f"{season_year + 1}0630")
    )
    week_chunks = _get_week_chunks(date_range[0], date_range[1])
    total_chunks = len(week_chunks)

    logger.info(f"  {season_year}-{season_year + 1}: {total_chunks} week chunks ({date_range[0]} to {date_range[1]})")

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Fetch all weeks concurrently
        tasks = [asyncio.create_task(_fetch_week(client, s, e)) for s, e in week_chunks]
        weekly_events = await asyncio.gather(*tasks)

        # Process each week's events
        for chunk_idx, events in enumerate(weekly_events):
            if not events:
                continue

            result = await _process_events(events, session, season.id, team_by_abbr)
            games_loaded += result["loaded"]
            games_skipped += result["skipped"]
            errors += result["errors"]
            for gt, cnt in result["by_type"].items():
                games_by_type[gt] = games_by_type.get(gt, 0) + cnt

            # Periodic commit
            if (chunk_idx + 1) % 10 == 0 or chunk_idx == total_chunks - 1:
                await session.commit()

            if (chunk_idx + 1) % 10 == 0 or chunk_idx == 0 or chunk_idx == total_chunks - 1:
                logger.info(f"    Week {chunk_idx + 1}/{total_chunks}: {games_loaded} loaded, {games_skipped} skipped, {errors} errors")

    # Final commit
    await session.commit()

    return {
        "season": season_year,
        "games_loaded": games_loaded,
        "games_skipped": games_skipped,
        "errors": errors,
        "by_type": games_by_type,
    }


async def ingest_nba_games(season_year: int, db_session) -> dict:
    """Load NBA games from ESPN API for a single season.

    Accepts a sessionmaker (not a session) so callers can pass the factory
    directly. Internally opens and manages its own session.
    """
    async with db_session() as session:
        result = await ingest_nba_schedule(session, season_year=season_year)
        await session.commit()
        return result


async def get_season_game_count(session: AsyncSession, season_year: int) -> int:
    """Count existing regular season games for a given season."""
    result = await session.execute(
        select(func.count(NBAGame.id))
        .join(NBASeason)
        .where(NBASeason.year == season_year, NBAGame.game_type == "REG")
    )
    return result.scalar() or 0


async def ingest_nba_all_seasons(
    session: AsyncSession,
    start_year: int = 2006,
    end_year: int = 2026,
    reparse_all: bool = False,
) -> dict:
    """Load NBA games for all seasons from ESPN.

    Args:
        session: DB session
        start_year: First season year
        end_year: Last season year (inclusive)
        reparse_all: If True, re-parse all seasons. If False, only parse seasons
                     with fewer than 1,200 regular season games.
    """
    total = {"games_loaded": 0, "games_skipped": 0, "errors": 0, "by_type": {}}

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Loading NBA games for {start_year}-{end_year} seasons")
    logger.info(f"{'=' * 60}")

    for year in range(start_year, end_year + 1):
        existing_count = await get_season_game_count(session, year)
        if existing_count >= 1200 and not reparse_all:
            logger.info(f"  Season {year}-{year + 1}: ✅ already complete ({existing_count} REG), skipping")
            continue

        # Delete existing games for incomplete seasons to avoid duplicates
        if not reparse_all and 0 < existing_count < 1200:
            season_result = await session.execute(
                select(NBASeason).where(NBASeason.year == year)
            )
            existing_season = season_result.scalar_one_or_none()
            if existing_season:
                await session.execute(
                    delete(NBAGame).where(NBAGame.season_id == existing_season.id)
                )
                await session.commit()
                logger.info(f"    Cleared {existing_count} existing games for {year}-{year + 1}")

        logger.info(f"\n  [{year}-{year + 1}] Loading...")
        result = await ingest_nba_schedule(session, season_year=year)
        total["games_loaded"] += result["games_loaded"]
        total["games_skipped"] += result["games_skipped"]
        total["errors"] += result["errors"]
        for gt, cnt in result.get("by_type", {}).items():
            total["by_type"][gt] = total["by_type"].get(gt, 0) + cnt

        reg = result.get("by_type", {}).get("REG", 0)
        logger.info(f"  [{year}] ✅ {result['games_loaded']} new, REG={reg}")

    logger.info(f"\n{'=' * 60}")
    logger.info(f"FINAL: {total['games_loaded']} new games loaded")
    logger.info(f"  By type: {total['by_type']}")

    return total


async def quick_test() -> dict:
    """Quick test for 2006 season (smallest, mostly already loaded)."""
    from app.database import async_session

    async with async_session() as db:
        return await ingest_nba_all_seasons(db, start_year=2006, end_year=2006, reparse_all=True)


async def run_fast() -> dict:
    """Run full load across all seasons (2006-2026)."""
    from app.database import async_session

    async with async_session() as db:
        return await ingest_nba_all_seasons(db, start_year=2006, end_year=2026)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(run_fast())
