"""ESPN NBA API data ingestion for schedules, scores, and team box scores.

CRITICAL: ESPN's historical NBA API:
1. Ignores seasontype & page params — returns ALL game types within date range
2. Multi-day date ranges DROP events for historical seasons (timezone offset issue)
   → Day-by-day queries are the ONLY reliable approach for complete historical data
   → Max ~15 games/day, well under the 100-event limit

The NBA regular season for season year Y runs Oct Y through Jun Y+1.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx
from dateutil import parser
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBATeam, NBASeason, NBAGame, NBAGameStatus

logger = logging.getLogger("earl.espn_nba")

# ESPN shorthand → DB abbreviation mapping
ESPN_TEAM_MAP = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GS": "GSW", "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NJ": "NJ",  # New Jersey Nets (historical)
    "NO": "NOP", "NY": "NYK",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
    "POR": "POR", "SAC": "SAC", "SA": "SAS", "SEA": "SEA",  # Seattle Supersonics (historical)
    "TOR": "TOR",
    "UTAH": "UTA", "WSH": "WAS",
}

# NBA season date ranges: October through June of the following year
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
    2020: ("20201201", "20210731"),  # COVID-delayed season
    2021: ("20211001", "20220630"),
    2022: ("20221001", "20230630"),
    2023: ("20231001", "20240630"),
    2024: ("20241001", "20250630"),
    2025: ("20251001", "20260630"),
    2026: ("20261001", "20270630"),
}

SEASON_TYPE_MAP = {1: "PRE", 2: "REG", 3: "POST"}


def _extract_stat(stats_list: list, name: str) -> int | None:
    for s in stats_list:
        if s.get("name") == name:
            try:
                return int(float(s.get("displayValue", 0)))
            except (ValueError, TypeError):
                return None
    return None


def _map_espn_status(status_name: str) -> NBAGameStatus:
    return {
        "STATUS_SCHEDULED": NBAGameStatus.SCHEDULED,
        "STATUS_IN_PROGRESS": NBAGameStatus.IN_PROGRESS,
        "STATUS_FINAL": NBAGameStatus.FINAL,
        "STATUS_POSTPONED": NBAGameStatus.POSTPONED,
        "STATUS_CANCELLED": NBAGameStatus.CANCELLED,
    }.get(status_name, NBAGameStatus.SCHEDULED)


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _parse_game_type(season_type_raw: int | None) -> str:
    return SEASON_TYPE_MAP.get(season_type_raw, "REG")


def _get_day_list(start_str: str, end_str: str) -> list[str]:
    """Generate YYYYMMDD strings from start to end (inclusive)."""
    start = datetime.strptime(start_str, "%Y%m%d")
    end = datetime.strptime(end_str, "%Y%m%d")
    days = []
    current = start
    while current <= end:
        days.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return days


async def _fetch_day(client: httpx.AsyncClient, date_str: str) -> list:
    """Fetch NBA events for a single day.
    Day-by-day is the ONLY reliable approach for ESPN NBA historical data.
    """
    url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
    params = {"dates": f"{date_str}-{date_str}", "limit": 100}
    try:
        resp = await client.get(url, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json().get("events", [])
    except Exception as e:
        logger.warning(f"  Error fetching {date_str}: {e}")
        return []


async def get_season_game_count(session: AsyncSession, season_year: int) -> int:
    """Count existing regular season games for a given season."""
    result = await session.execute(
        select(func.count(NBAGame.id))
        .join(NBASeason)
        .where(NBASeason.year == season_year, NBAGame.game_type == "REG")
    )
    return result.scalar() or 0


async def ingest_nba_schedule(
    session: AsyncSession,
    season_year: int = 2025,
    batch_commit: bool = True,
) -> dict:
    """Load NBA games from ESPN API for a single season (all game types)."""
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

    # Build day-by-day list
    date_range = NBA_SEASON_RANGES.get(
        season_year, (f"{season_year}1001", f"{season_year+1}0630")
    )
    days = _get_day_list(date_range[0], date_range[1])
    total_days = len(days)

    async with httpx.AsyncClient(timeout=30.0) as client:
        for idx, day_str in enumerate(days):
            events = await _fetch_day(client, day_str)
            if not events:
                continue

            day_loaded = 0
            for event in events:
                try:
                    event_id_str = event.get("id", "")
                    if not event_id_str:
                        continue

                    competitions = event.get("competitions", [])
                    if not competitions:
                        continue
                    comp = competitions[0]

                    # Game type from event data
                    season_type_raw = event.get("season", {}).get("type")
                    game_type = _parse_game_type(season_type_raw)

                    # Skip duplicates
                    existing = await session.execute(
                        select(NBAGame).where(NBAGame.nba_game_id == event_id_str)
                    )
                    if existing.scalar_one_or_none():
                        games_skipped += 1
                        continue

                    # Competitors
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

                    espn_home = home_data.get("team", {}).get("abbreviation")
                    espn_away = away_data.get("team", {}).get("abbreviation")
                    if not espn_home or not espn_away:
                        continue

                    home_abbr = ESPN_TEAM_MAP.get(espn_home, espn_home)
                    away_abbr = ESPN_TEAM_MAP.get(espn_away, espn_away)

                    home_team = team_by_abbr.get(home_abbr)
                    away_team = team_by_abbr.get(away_abbr)
                    if not home_team or not away_team:
                        errors += 1
                        continue

                    date_str_evt = comp.get("date") or event.get("date", "")
                    try:
                        game_date = parser.parse(date_str_evt)
                    except (ValueError, TypeError):
                        game_date = datetime.now(timezone.utc)

                    status_type = comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
                    game_status = _map_espn_status(status_type)

                    venue_info = comp.get("venue") or {}
                    venue_name = venue_info.get("fullName") if venue_info else None

                    home_stats = home_data.get("statistics", [])
                    away_stats = away_data.get("statistics", [])
                    has_stats = any(s.get("name") == "fieldGoalsMade" for s in home_stats)

                    game = NBAGame(
                        nba_game_id=event_id_str,
                        season_id=season.id,
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
                    games_loaded += 1
                    day_loaded += 1
                    games_by_type[game_type] = games_by_type.get(game_type, 0) + 1

                except Exception as e:
                    logger.warning(f"  Error on {event.get('id', '?')}: {e}")
                    errors += 1
                    continue

            # Commit every 10 days
            if batch_commit and day_loaded > 0 and (idx + 1) % 10 == 0:
                await session.commit()

            # Log progress
            if (idx + 1) % 50 == 0 or idx == 0 or idx == total_days - 1:
                logger.info(f"  Day {idx+1}/{total_days}: {games_loaded} loaded, {games_skipped} skipped")

    if not batch_commit:
        await session.commit()

    return {
        "season": season_year,
        "games_loaded": games_loaded,
        "games_skipped": games_skipped,
        "errors": errors,
        "by_type": games_by_type,
    }


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

    logger.info(f"\n{'='*60}")
    logger.info(f"Loading NBA games for {start_year}-{end_year} seasons")
    logger.info(f"{'='*60}")

    for year in range(start_year, end_year + 1):
        # Skip if already complete
        existing_count = await get_season_game_count(session, year)
        if existing_count >= 1200 and not reparse_all:
            logger.info(f"  Season {year}-{year+1}: ✅ already complete ({existing_count} REG), skipping")
            continue

        # Delete existing games for partial seasons to avoid duplicates
        if not reparse_all and 0 < existing_count < 1200:
            # Delete just the incomplete season's games
            season_result = await session.execute(
                select(NBASeason).where(NBASeason.year == year)
            )
            season = season_result.scalar_one_or_none()
            if season:
                await session.execute(
                    select(NBAGame.id).where(NBAGame.season_id == season.id)
                )
                # Use SQL directly for delete
                from sqlalchemy import text
                await session.execute(
                    text(f"DELETE FROM nba.games WHERE season_id = {season.id}")
                )
                await session.commit()
                logger.info(f"  [{year}] Deleted {existing_count} existing games, re-loading...")

        logger.info(f"\n  [{year}-{year+1}] Loading (day-by-day)...")
        result = await ingest_nba_schedule(session, season_year=year)
        total["games_loaded"] += result["games_loaded"]
        total["games_skipped"] += result["games_skipped"]
        total["errors"] += result["errors"]
        for gt, cnt in result.get("by_type", {}).items():
            total["by_type"][gt] = total["by_type"].get(gt, 0) + cnt

        reg = result.get("by_type", {}).get("REG", 0)
        logger.info(f"  [{year}] ✅ {result['games_loaded']} new, REG={reg}")

    logger.info(f"\n{'='*60}")
    logger.info(f"FINAL: {total['games_loaded']} new games loaded")
    logger.info(f"  By type: {total['by_type']}")

    return total


async def quick_test() -> dict:
    """Quick test for a single recent season."""
    from app.database import async_session
    async with async_session() as db:
        return await ingest_nba_all_seasons(db, start_year=2006, end_year=2006)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(quick_test())
