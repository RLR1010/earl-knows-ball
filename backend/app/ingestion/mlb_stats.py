"""
MLB data ingestion pipeline.

Loads comprehensive MLB data from the public MLB Stats API (statsapi.mlb.com).

Data loaded:
  1. Player rosters and profiles (all 30 teams, 2006-2026)
  2. Season batting statistics per player
  3. Season pitching statistics per player
  4. Game schedules and scores
"""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session, Base, engine
from app.models.mlb import (
    MLBTeam,
    MLBSeason,
    MLBGames,
    GameStatus,
    MLBPlayer,
    MLBInjury,
    MLBBattingStats,
    MLBPitchingStats,
)

logger = logging.getLogger("earl.mlb_stats")

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

# All 30 MLB teams with their API IDs
MLB_TEAMS = [
    (108, "LAA", "Los Angeles Angels", "AL", "West"),
    (109, "ARI", "Arizona Diamondbacks", "NL", "West"),
    (110, "BAL", "Baltimore Orioles", "AL", "East"),
    (111, "BOS", "Boston Red Sox", "AL", "East"),
    (112, "CHC", "Chicago Cubs", "NL", "Central"),
    (113, "CIN", "Cincinnati Reds", "NL", "Central"),
    (114, "CLE", "Cleveland Guardians", "AL", "Central"),
    (115, "COL", "Colorado Rockies", "NL", "West"),
    (116, "DET", "Detroit Tigers", "AL", "Central"),
    (117, "HOU", "Houston Astros", "AL", "West"),
    (118, "KC", "Kansas City Royals", "AL", "Central"),
    (119, "LAD", "Los Angeles Dodgers", "NL", "West"),
    (120, "WSH", "Washington Nationals", "NL", "East"),
    (121, "NYM", "New York Mets", "NL", "East"),
    (133, "OAK", "Oakland Athletics", "AL", "West"),
    (134, "PIT", "Pittsburgh Pirates", "NL", "Central"),
    (135, "SD", "San Diego Padres", "NL", "West"),
    (136, "SEA", "Seattle Mariners", "AL", "West"),
    (137, "SF", "San Francisco Giants", "NL", "West"),
    (138, "STL", "St. Louis Cardinals", "NL", "Central"),
    (139, "TB", "Tampa Bay Rays", "AL", "East"),
    (140, "TEX", "Texas Rangers", "AL", "West"),
    (141, "TOR", "Toronto Blue Jays", "AL", "East"),
    (142, "MIN", "Minnesota Twins", "AL", "Central"),
    (143, "PHI", "Philadelphia Phillies", "NL", "East"),
    (144, "ATL", "Atlanta Braves", "NL", "East"),
    (145, "CWS", "Chicago White Sox", "AL", "Central"),
    (146, "MIA", "Miami Marlins", "NL", "East"),
    (147, "NYY", "New York Yankees", "AL", "East"),
    (158, "MIL", "Milwaukee Brewers", "NL", "Central"),
]

# Position mapping from MLB API to our shorthand
POSITION_MAP = {
    "P": "P", "C": "C", "1B": "1B", "2B": "2B", "3B": "3B",
    "SS": "SS", "LF": "LF", "CF": "CF", "RF": "RF",
    "OF": "OF", "DH": "DH", "IF": "IF", "UT": "UT",
    "SP": "P", "RP": "P", "CL": "P",
}

YEARS = list(range(2006, 2027))  # 2006-2026


# ── Helpers ────────────────────────────────────────────────────────────

async def _api_get(url: str, params: dict = None) -> dict | None:
    """Make async HTTP GET to MLB Stats API."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"API error: {url} - {e}")
            return None


def _parse_abbreviation(name: str) -> str:
    """Standardize a team abbreviation."""
    for _, abbr, _, _, _ in MLB_TEAMS:
        if name.upper().startswith(abbr):
            return abbr
    return name.upper()[:4]


def _parse_height(height_str: str | None) -> int | None:
    """Parse height string like '6\' 2"' to inches."""
    if not height_str:
        return None
    m = re.match(r"(\d+)\s*['\"]?\s*(\d+)?", height_str)
    if m:
        ft = int(m.group(1))
        inc = int(m.group(2) or 0)
        return ft * 12 + inc
    return None


def _parse_weight(raw) -> int | None:
    """Parse weight (can be int, str, or None)."""
    if raw is None:
        return None
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely parse a float value that might be a string or None."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    """Safely parse an integer value."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ── Step 1: Sync Teams & Seasons ──────────────────────────────────────

async def sync_teams(db: AsyncSession) -> dict[int, int]:
    """Ensure all 30 MLB teams exist in the database. Returns {mlb_api_id: db_id}."""
    result = {}
    for api_id, abbr, name, league, division in MLB_TEAMS:
        r = await db.execute(select(MLBTeam).where(MLBTeam.abbreviation == abbr))
        team = r.scalar_one_or_none()
        if not team:
            team = MLBTeam(
                abbreviation=abbr,
                name=name,
                league=league,
                division=division,
            )
            db.add(team)
            await db.flush()
        result[api_id] = team.id
    return result


async def sync_seasons(db: AsyncSession) -> dict[int, int]:
    """Ensure seasons 2006-2026 exist. Returns {year: db_id}."""
    result = {}
    for year in YEARS:
        r = await db.execute(select(MLBSeason).where(MLBSeason.year == year))
        season = r.scalar_one_or_none()
        if not season:
            season = MLBSeason(year=year)
            db.add(season)
            await db.flush()
        result[year] = season.id
    return result


# ── Step 2: Roster Sync ───────────────────────────────────────────────

async def sync_roster_for_team_year(
    db: AsyncSession,
    api_team_id: int,
    team_db_id: int,
    year: int,
    team_abbr: str,
) -> list[int]:
    """Load roster for one team/year. Returns list of MLB player IDs created/updated."""
    url = f"{MLB_API_BASE}/teams/{api_team_id}/roster"
    data = await _api_get(url, {"season": year})
    if not data:
        return []

    player_ids = []
    for entry in data.get("roster", []):
        person = entry.get("person", {})
        person_id = person.get("id")
        if not person_id:
            continue

        full_name = person.get("fullName", "")
        pos_abbr = entry.get("position", {}).get("abbreviation", "")
        pos = POSITION_MAP.get(pos_abbr, pos_abbr)

        # Check if player exists by mlb_id
        r = await db.execute(select(MLBPlayer).where(MLBPlayer.mlb_id == person_id))
        player = r.scalar_one_or_none()

        if player:
            # Update team assignment for this year
            player.team_id = team_db_id
            player.position = pos
            if not player.active:
                player.active = 1
        else:
            # Get fuller profile
            profile = await _api_get(
                f"{MLB_API_BASE}/people/{person_id}"
            )
            person_detail = None
            if profile and profile.get("people"):
                person_detail = profile["people"][0]

            bats = None
            throws = None
            height = None
            weight = None
            birth_date = None
            birth_city = None
            birth_state = None
            birth_country = None
            college = None
            jersey = None

            if person_detail:
                bats = person_detail.get("batSide", {}).get("code", "").upper() or None
                throws = person_detail.get("pitchHand", {}).get("code", "").upper() or None
                height = _parse_height(person_detail.get("height"))
                weight = _parse_weight(person_detail.get("weight"))
                if person_detail.get("birthDate"):
                    try:
                        birth_date = datetime.strptime(
                            person_detail["birthDate"], "%Y-%m-%d"
                        ).date()
                    except ValueError:
                        pass
                birth_city = person_detail.get("birthCity")
                birth_state = person_detail.get("birthStateProvince")
                birth_country = person_detail.get("birthCountry")
                if person_detail.get("colleges"):
                    college = person_detail["colleges"][0].get("name")
                jersey = _safe_int(entry.get("jerseyNumber"))

            player = MLBPlayer(
                mlb_id=person_id,
                name=full_name,
                position=pos,
                team_id=team_db_id,
                jersey_number=jersey,
                bats=bats,
                throws=throws,
                height=height,
                weight=weight,
                birth_date=birth_date,
                birth_city=birth_city,
                birth_state_province=birth_state,
                birth_country=birth_country,
                college=college,
                active=1,
            )
            db.add(player)
            await db.flush()

        player_ids.append(player.id)

    return player_ids


async def sync_all_rosters(db: AsyncSession, team_map: dict[int, int]) -> dict[int, dict[int, list[int]]]:
    """Sync rosters for all teams for all years.
    Returns {mlb_team_api_id: {year: [player_db_ids]}}"""
    results = {}
    total_players = 0

    for api_team_id, abbr, name, league, division in MLB_TEAMS:
        team_db_id = team_map[api_team_id]
        results[api_team_id] = {}
        for year in YEARS:
            player_ids = await sync_roster_for_team_year(
                db, api_team_id, team_db_id, year, abbr
            )
            results[api_team_id][year] = player_ids
            total_players += len(player_ids)
            if player_ids:
                logger.info(f"  {abbr} {year}: {len(player_ids)} players")
            await db.commit()

    logger.info(f"Total player roster entries across all teams/years: {total_players}")
    return results


# ── Step 3: Season Stats (Batting) ────────────────────────────────────

async def load_batting_season(
    db: AsyncSession,
    year: int,
    season_id: int,
    team_map: dict[int, int],
    team_abbr_map: dict[int, str],
):
    """Load batting stats for ALL players from all teams for one season.
    
    Strategy: iterate each team's roster, batch player IDs, get stats.
    """
    url = f"{MLB_API_BASE}/stats"
    count = 0

    for api_team_id, abbr, name, league, division in MLB_TEAMS:
        team_db_id = team_map[api_team_id]

        # Get roster for this team/year
        roster_url = f"{MLB_API_BASE}/teams/{api_team_id}/roster"
        roster_data = await _api_get(roster_url, {"season": year})
        if not roster_data:
            continue

        player_ids = []
        for entry in roster_data.get("roster", []):
            pid = entry.get("person", {}).get("id")
            pos_abbr = entry.get("position", {}).get("abbreviation", "")
            # Only include non-pitchers for batting stats
            if pid and pos_abbr != "P":
                player_ids.append((pid, pos_abbr))

        if not player_ids:
            continue

        # Batch players in groups of 10 to avoid API issues with mixed positions
        batch_size = 1
        for i in range(0, len(player_ids), batch_size):
            batch = player_ids[i:i + batch_size]
            ids_str = ",".join(str(pid) for pid, _ in batch)

            # Get hitting stats for this batch
            stats_url = f"{MLB_API_BASE}/people/{ids_str}/stats"
            stats_data = await _api_get(stats_url, {
                "stats": "season",
                "season": str(year),
                "group": "hitting",
                "gameType": "R",
            })

            if not stats_data:
                # Batch failed (mixed positions). Fetch individually.
                for pid, pos in batch:
                    sd = await _api_get(
                        f"{MLB_API_BASE}/people/{pid}/stats",
                        {"stats": "season", "season": str(year), "group": "hitting", "gameType": "R"},
                    )
                    if sd:
                        for se in sd.get("stats", []):
                            if se.get("group", {}).get("displayName", "").lower() != "hitting":
                                continue
                            for sp in se.get("splits", []):
                                await _upsert_batting_row(db, sp, year, season_id, team_db_id)
                continue

            for stat_entry in stats_data.get("stats", []):
                if stat_entry.get("group", {}).get("displayName", "").lower() != "hitting":
                    continue
                for split in stat_entry.get("splits", []):
                    stat = split.get("stat", {})
                    if not stat.get("gamesPlayed"):
                        continue

                    person = split.get("player", {})
                    mlb_player_id = person.get("id")
                    if not mlb_player_id:
                        continue

                    # Find our player
                    r = await db.execute(
                        select(MLBPlayer).where(MLBPlayer.mlb_id == mlb_player_id)
                    )
                    player = r.scalar_one_or_none()
                    if not player:
                        continue

                    # Upsert batting stats
                    r2 = await db.execute(
                        select(MLBBattingStats).where(
                            MLBBattingStats.player_id == player.id,
                            MLBBattingStats.season_id == season_id,
                        )
                    )
                    existing = r2.scalar_one_or_none()

                    bstat = existing or MLBBattingStats(
                        player_id=player.id,
                        season_id=season_id,
                    )
                    bstat.team_id = team_db_id
                    bstat.games_played = _safe_int(stat.get("gamesPlayed"))
                    bstat.plate_appearances = _safe_int(stat.get("plateAppearances"))
                    bstat.at_bats = _safe_int(stat.get("atBats"))
                    bstat.runs = _safe_int(stat.get("runs"))
                    bstat.hits = _safe_int(stat.get("hits"))
                    bstat.doubles = _safe_int(stat.get("doubles"))
                    bstat.triples = _safe_int(stat.get("triples"))
                    bstat.home_runs = _safe_int(stat.get("homeRuns"))
                    bstat.runs_batted_in = _safe_int(stat.get("rbi"))
                    bstat.stolen_bases = _safe_int(stat.get("stolenBases"))
                    bstat.caught_stealing = _safe_int(stat.get("caughtStealing"))
                    bstat.base_on_balls = _safe_int(stat.get("baseOnBalls"))
                    bstat.intentional_walks = _safe_int(stat.get("intentionalWalks"))
                    bstat.strikeouts = _safe_int(stat.get("strikeOuts"))
                    bstat.hit_by_pitch = _safe_int(stat.get("hitByPitch"))
                    bstat.sacrifice_flies = _safe_int(stat.get("sacFlies"))
                    bstat.sacrifice_bunts = _safe_int(stat.get("sacBunts"))
                    bstat.ground_outs = _safe_int(stat.get("groundOuts"))
                    bstat.air_outs = _safe_int(stat.get("airOuts"))
                    bstat.ground_into_double_play = _safe_int(stat.get("groundIntoDoublePlay"))
                    bstat.avg = _safe_float(stat.get("avg"))
                    bstat.obp = _safe_float(stat.get("obp"))
                    bstat.slg = _safe_float(stat.get("slg"))
                    bstat.ops = _safe_float(stat.get("ops"))
                    bstat.babip = _safe_float(stat.get("babip"))
                    bstat.total_bases = _safe_int(stat.get("totalBases"))
                    bstat.at_bats_per_home_run = _safe_float(stat.get("atBatsPerHomeRun"))
                    bstat.stolen_base_percentage = _safe_float(stat.get("stolenBasePercentage"))

                    if not existing:
                        db.add(bstat)
                    count += 1

        await db.commit()
        logger.info(f"  Batting {abbr} {year}: loaded")

    logger.info(f"Total batting stat entries for {year}: {count}")


# ── Step 4: Season Stats (Pitching) ───────────────────────────────────

async def load_pitching_season(
    db: AsyncSession,
    year: int,
    season_id: int,
    team_map: dict[int, int],
):
    """Load pitching stats for ALL players for one season."""
    count = 0

    for api_team_id, abbr, name, league, division in MLB_TEAMS:
        team_db_id = team_map[api_team_id]

        roster_url = f"{MLB_API_BASE}/teams/{api_team_id}/roster"
        roster_data = await _api_get(roster_url, {"season": year})
        if not roster_data:
            continue

        player_ids = []
        for entry in roster_data.get("roster", []):
            pid = entry.get("person", {}).get("id")
            pos_abbr = entry.get("position", {}).get("abbreviation", "")
            # Only include pitchers for pitching stats
            if pid and pos_abbr == "P":
                player_ids.append((pid, pos_abbr))

        if not player_ids:
            continue

        batch_size = 1
        for i in range(0, len(player_ids), batch_size):
            batch = player_ids[i:i + batch_size]
            ids_str = ",".join(str(pid) for pid, _ in batch)

            stats_url = f"{MLB_API_BASE}/people/{ids_str}/stats"
            stats_data = await _api_get(stats_url, {
                "stats": "season",
                "season": str(year),
                "group": "pitching",
                "gameType": "R",
            })

            if not stats_data:
                # Batch failed. Fetch individually.
                for pid, pos in batch:
                    sd = await _api_get(
                        f"{MLB_API_BASE}/people/{pid}/stats",
                        {"stats": "season", "season": str(year), "group": "pitching", "gameType": "R"},
                    )
                    if sd:
                        for se in sd.get("stats", []):
                            if se.get("group", {}).get("displayName", "").lower() != "pitching":
                                continue
                            for sp in se.get("splits", []):
                                await _upsert_pitching_row(db, sp, year, season_id, team_db_id)
                continue

            for stat_entry in stats_data.get("stats", []):
                if stat_entry.get("group", {}).get("displayName", "").lower() != "pitching":
                    continue
                for split in stat_entry.get("splits", []):
                    stat = split.get("stat", {})
                    if not stat.get("gamesPlayed"):
                        continue

                    person = split.get("player", {})
                    mlb_player_id = person.get("id")
                    if not mlb_player_id:
                        continue

                    r = await db.execute(
                        select(MLBPlayer).where(MLBPlayer.mlb_id == mlb_player_id)
                    )
                    player = r.scalar_one_or_none()
                    if not player:
                        continue

                    r2 = await db.execute(
                        select(MLBPitchingStats).where(
                            MLBPitchingStats.player_id == player.id,
                            MLBPitchingStats.season_id == season_id,
                        )
                    )
                    existing = r2.scalar_one_or_none()

                    pstat = existing or MLBPitchingStats(
                        player_id=player.id,
                        season_id=season_id,
                    )
                    pstat.team_id = team_db_id
                    pstat.games_played = _safe_int(stat.get("gamesPlayed"))
                    pstat.games_started = _safe_int(stat.get("gamesStarted"))
                    pstat.games_finished = _safe_int(stat.get("gamesFinished"))
                    pstat.complete_games = _safe_int(stat.get("completeGames"))
                    pstat.shutouts = _safe_int(stat.get("shutouts"))
                    pstat.innings_pitched = _safe_float(stat.get("inningsPitched"))
                    pstat.outs = _safe_int(stat.get("outs"))
                    pstat.wins = _safe_int(stat.get("wins"))
                    pstat.losses = _safe_int(stat.get("losses"))
                    pstat.saves = _safe_int(stat.get("saves"))
                    pstat.blown_saves = _safe_int(stat.get("blownSaves"))
                    pstat.save_opportunities = _safe_int(stat.get("saveOpportunities"))
                    pstat.holds = _safe_int(stat.get("holds"))
                    pstat.win_percentage = _safe_float(stat.get("winPercentage"))
                    pstat.hits = _safe_int(stat.get("hits"))
                    pstat.runs = _safe_int(stat.get("runs"))
                    pstat.earned_runs = _safe_int(stat.get("earnedRuns"))
                    pstat.home_runs = _safe_int(stat.get("homeRuns"))
                    pstat.doubles = _safe_int(stat.get("doubles"))
                    pstat.triples = _safe_int(stat.get("triples"))
                    pstat.at_bats = _safe_int(stat.get("atBats"))
                    pstat.batters_faced = _safe_int(stat.get("battersFaced"))
                    pstat.base_on_balls = _safe_int(stat.get("baseOnBalls"))
                    pstat.intentional_walks = _safe_int(stat.get("intentionalWalks"))
                    pstat.strikeouts = _safe_int(stat.get("strikeOuts"))
                    pstat.hit_by_pitch = _safe_int(stat.get("hitByPitch"))
                    pstat.era = _safe_float(stat.get("era"))
                    pstat.whip = _safe_float(stat.get("whip"))
                    pstat.avg = _safe_float(stat.get("avg"))
                    pstat.obp = _safe_float(stat.get("obp"))
                    pstat.slg = _safe_float(stat.get("slg"))
                    pstat.ops = _safe_float(stat.get("ops"))
                    pstat.hits_per_9 = _safe_float(stat.get("hitsPer9Inn"))
                    pstat.home_runs_per_9 = _safe_float(stat.get("homeRunsPer9"))
                    pstat.strikeouts_per_9 = _safe_float(stat.get("strikeoutsPer9Inn"))
                    pstat.walks_per_9 = _safe_float(stat.get("walksPer9Inn"))
                    pstat.strikeout_walk_ratio = _safe_float(stat.get("strikeoutWalkRatio"))
                    pstat.ground_outs = _safe_int(stat.get("groundOuts"))
                    pstat.air_outs = _safe_int(stat.get("airOuts"))
                    pstat.ground_into_double_play = _safe_int(stat.get("groundIntoDoublePlay"))
                    pstat.wild_pitches = _safe_int(stat.get("wildPitches"))
                    pstat.balks = _safe_int(stat.get("balks"))
                    pstat.pickoffs = _safe_int(stat.get("pickoffs"))
                    pstat.pitches_thrown = _safe_int(stat.get("numberOfPitches"))
                    pstat.strikes = _safe_int(stat.get("strikes"))
                    pstat.strike_percentage = _safe_float(stat.get("strikePercentage"))
                    pstat.pitches_per_inning = _safe_float(stat.get("pitchesPerInning"))
                    pstat.stolen_bases = _safe_int(stat.get("stolenBases"))
                    pstat.caught_stealing = _safe_int(stat.get("caughtStealing"))
                    pstat.caught_stealing_percentage = _safe_float(stat.get("caughtStealingPercentage"))

                    if not existing:
                        db.add(pstat)
                    count += 1

        await db.commit()
        logger.info(f"  Pitching {abbr} {year}: loaded")

    logger.info(f"Total pitching stat entries for {year}: {count}")


# ── Step 5: Game Schedules ────────────────────────────────────────────

async def load_games_for_season(
    db: AsyncSession,
    year: int,
    season_id: int,
    team_map: dict[int, int],
    team_abbr_by_api_id: dict[int, str],
):
    """Load all regular season games for a given year."""
    url = f"{MLB_API_BASE}/schedule"
    data = await _api_get(url, {
        "sportId": 1,
        "season": year,
        "gameTypes": "R",
        "hydrate": "venue,weather",
    })
    if not data:
        logger.warning(f"No schedule data for {year}")
        return 0

    count = 0
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            game_pk = game.get("gamePk")
            if not game_pk:
                continue

            # Check if already loaded
            r = await db.execute(
                select(MLBGames).where(MLBGames.mlb_game_id == game_pk)
            )
            if r.scalar_one_or_none():
                continue

            teams_data = game.get("teams", {})
            away = teams_data.get("away", {})
            home = teams_data.get("home", {})

            away_team_api_id = away.get("team", {}).get("id")
            home_team_api_id = home.get("team", {}).get("id")

            if away_team_api_id not in team_map or home_team_api_id not in team_map:
                continue

            game_date_str = game.get("gameDate")
            game_date = None
            if game_date_str:
                try:
                    game_date = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
                except ValueError:
                    pass

            status_data = game.get("status", {})
            status_code = status_data.get("codedGameState", "S")
            if status_code in ("F", "O", "FT"):
                status = GameStatus.FINAL
            elif status_code in ("I", "M", "MA"):
                status = GameStatus.IN_PROGRESS
            elif status_code == "PD":
                status = GameStatus.POSTPONED
            elif status_code in ("C", "CA"):
                status = GameStatus.CANCELLED
            else:
                status = GameStatus.SCHEDULED

            away_score = away.get("score")
            home_score = home.get("score")
            venue_data = game.get("venue", {})
            venue_name = venue_data.get("name")
            venue_id = venue_data.get("id")

            away_record = away.get("leagueRecord", {})
            home_record = home.get("leagueRecord", {})

            # Extract weather data from API response
            weather_data = game.get("weather", {}) or {}
            temp_str = weather_data.get("temp", "")
            wind_str = weather_data.get("wind", "")
            try:
                wind_speed_val = int("".join(c for c in wind_str.split(",")[0] if c.isdigit() or c in ".-"))
            except (ValueError, IndexError):
                wind_speed_val = None

            # Parse wind direction
            wind_dir_val = None
            if wind_str and "," in wind_str:
                wpart = wind_str.split(",", 1)[1].strip().lower()
                if "out" in wpart:
                    wind_dir_val = "out"
                elif "in" in wpart:
                    wind_dir_val = "in"
                if not wind_dir_val:
                    if wpart.startswith("l") and "r" in wpart:
                        wind_dir_val = "l_to_r"
                    elif wpart.startswith("r") and "l" in wpart:
                        wind_dir_val = "r_to_l"

            db_game = MLBGames(
                mlb_game_id=game_pk,
                season_id=season_id,
                game_type=game.get("gameType", "R"),
                game_number=game.get("gameNumber", 0),
                home_team_id=team_map[home_team_api_id],
                away_team_id=team_map[away_team_api_id],
                date=game_date,
                status=status,
                home_score=_safe_int(home_score),
                away_score=_safe_int(away_score),
                venue=venue_name,
                venue_id=venue_id,
                scheduled_innings=game.get("scheduledInnings", 9),
                day_night=game.get("dayNight"),
                home_wins=_safe_int(home_record.get("wins")),
                home_losses=_safe_int(home_record.get("losses")),
                away_wins=_safe_int(away_record.get("wins")),
                away_losses=_safe_int(away_record.get("losses")),
                temperature=_safe_int(temp_str),
                wind_speed=wind_speed_val,
                weather_condition=weather_data.get("condition"),
                wind_direction=wind_dir_val,
            )
            db.add(db_game)
            count += 1

    await db.commit()
    return count


# ── Upsert helpers ───────────────────────────────────────────────────


async def _upsert_batting_row(db: AsyncSession, split: dict, year: int, season_id: int, team_db_id: int):
    """Upsert a single batting stats row from an API split."""
    stat = split.get("stat", {})
    if not stat.get("gamesPlayed"):
        return
    person = split.get("player", {})
    mlb_player_id = person.get("id")
    if not mlb_player_id:
        return
    r = await db.execute(select(MLBPlayer).where(MLBPlayer.mlb_id == mlb_player_id))
    player = r.scalar_one_or_none()
    if not player:
        return
    r2 = await db.execute(
        select(MLBBattingStats).where(
            MLBBattingStats.player_id == player.id,
            MLBBattingStats.season_id == season_id,
        )
    )
    existing = r2.scalar_one_or_none()
    bstat = existing or MLBBattingStats(player_id=player.id, season_id=season_id)
    bstat.team_id = team_db_id
    bstat.games_played = _safe_int(stat.get("gamesPlayed"))
    bstat.plate_appearances = _safe_int(stat.get("plateAppearances"))
    bstat.at_bats = _safe_int(stat.get("atBats"))
    bstat.runs = _safe_int(stat.get("runs"))
    bstat.hits = _safe_int(stat.get("hits"))
    bstat.doubles = _safe_int(stat.get("doubles"))
    bstat.triples = _safe_int(stat.get("triples"))
    bstat.home_runs = _safe_int(stat.get("homeRuns"))
    bstat.runs_batted_in = _safe_int(stat.get("rbi"))
    bstat.stolen_bases = _safe_int(stat.get("stolenBases"))
    bstat.caught_stealing = _safe_int(stat.get("caughtStealing"))
    bstat.base_on_balls = _safe_int(stat.get("baseOnBalls"))
    bstat.intentional_walks = _safe_int(stat.get("intentionalWalks"))
    bstat.strikeouts = _safe_int(stat.get("strikeOuts"))
    bstat.hit_by_pitch = _safe_int(stat.get("hitByPitch"))
    bstat.sacrifice_flies = _safe_int(stat.get("sacFlies"))
    bstat.sacrifice_bunts = _safe_int(stat.get("sacBunts"))
    bstat.ground_outs = _safe_int(stat.get("groundOuts"))
    bstat.air_outs = _safe_int(stat.get("airOuts"))
    bstat.ground_into_double_play = _safe_int(stat.get("groundIntoDoublePlay"))
    bstat.avg = _safe_float(stat.get("avg"))
    bstat.obp = _safe_float(stat.get("obp"))
    bstat.slg = _safe_float(stat.get("slg"))
    bstat.ops = _safe_float(stat.get("ops"))
    bstat.babip = _safe_float(stat.get("babip"))
    bstat.total_bases = _safe_int(stat.get("totalBases"))
    bstat.at_bats_per_home_run = _safe_float(stat.get("atBatsPerHomeRun"))
    bstat.stolen_base_percentage = _safe_float(stat.get("stolenBasePercentage"))
    if not existing:
        db.add(bstat)


async def _upsert_pitching_row(db: AsyncSession, split: dict, year: int, season_id: int, team_db_id: int):
    """Upsert a single pitching stats row from an API split."""
    stat = split.get("stat", {})
    if not stat.get("gamesPlayed"):
        return
    person = split.get("player", {})
    mlb_player_id = person.get("id")
    if not mlb_player_id:
        return
    r = await db.execute(select(MLBPlayer).where(MLBPlayer.mlb_id == mlb_player_id))
    player = r.scalar_one_or_none()
    if not player:
        return
    r2 = await db.execute(
        select(MLBPitchingStats).where(
            MLBPitchingStats.player_id == player.id,
            MLBPitchingStats.season_id == season_id,
        )
    )
    existing = r2.scalar_one_or_none()
    pstat = existing or MLBPitchingStats(player_id=player.id, season_id=season_id)
    pstat.team_id = team_db_id
    pstat.games_played = _safe_int(stat.get("gamesPlayed"))
    pstat.games_started = _safe_int(stat.get("gamesStarted"))
    pstat.games_finished = _safe_int(stat.get("gamesFinished"))
    pstat.complete_games = _safe_int(stat.get("completeGames"))
    pstat.shutouts = _safe_int(stat.get("shutouts"))
    pstat.innings_pitched = _safe_float(stat.get("inningsPitched"))
    pstat.outs = _safe_int(stat.get("outs"))
    pstat.wins = _safe_int(stat.get("wins"))
    pstat.losses = _safe_int(stat.get("losses"))
    pstat.saves = _safe_int(stat.get("saves"))
    pstat.blown_saves = _safe_int(stat.get("blownSaves"))
    pstat.save_opportunities = _safe_int(stat.get("saveOpportunities"))
    pstat.holds = _safe_int(stat.get("holds"))
    pstat.win_percentage = _safe_float(stat.get("winPercentage"))
    pstat.hits = _safe_int(stat.get("hits"))
    pstat.runs = _safe_int(stat.get("runs"))
    pstat.earned_runs = _safe_int(stat.get("earnedRuns"))
    pstat.home_runs = _safe_int(stat.get("homeRuns"))
    pstat.doubles = _safe_int(stat.get("doubles"))
    pstat.triples = _safe_int(stat.get("triples"))
    pstat.at_bats = _safe_int(stat.get("atBats"))
    pstat.batters_faced = _safe_int(stat.get("battersFaced"))
    pstat.base_on_balls = _safe_int(stat.get("baseOnBalls"))
    pstat.intentional_walks = _safe_int(stat.get("intentionalWalks"))
    pstat.strikeouts = _safe_int(stat.get("strikeOuts"))
    pstat.hit_by_pitch = _safe_int(stat.get("hitByPitch"))
    pstat.era = _safe_float(stat.get("era"))
    pstat.whip = _safe_float(stat.get("whip"))
    pstat.avg = _safe_float(stat.get("avg"))
    pstat.obp = _safe_float(stat.get("obp"))
    pstat.slg = _safe_float(stat.get("slg"))
    pstat.ops = _safe_float(stat.get("ops"))
    pstat.hits_per_9 = _safe_float(stat.get("hitsPer9Inn"))
    pstat.home_runs_per_9 = _safe_float(stat.get("homeRunsPer9"))
    pstat.strikeouts_per_9 = _safe_float(stat.get("strikeoutsPer9Inn"))
    pstat.walks_per_9 = _safe_float(stat.get("walksPer9Inn"))
    pstat.strikeout_walk_ratio = _safe_float(stat.get("strikeoutWalkRatio"))
    pstat.ground_outs = _safe_int(stat.get("groundOuts"))
    pstat.air_outs = _safe_int(stat.get("airOuts"))
    pstat.ground_into_double_play = _safe_int(stat.get("groundIntoDoublePlay"))
    pstat.wild_pitches = _safe_int(stat.get("wildPitches"))
    pstat.balks = _safe_int(stat.get("balks"))
    pstat.pickoffs = _safe_int(stat.get("pickoffs"))
    pstat.pitches_thrown = _safe_int(stat.get("numberOfPitches"))
    pstat.strikes = _safe_int(stat.get("strikes"))
    pstat.strike_percentage = _safe_float(stat.get("strikePercentage"))
    pstat.pitches_per_inning = _safe_float(stat.get("pitchesPerInning"))
    pstat.stolen_bases = _safe_int(stat.get("stolenBases"))
    pstat.caught_stealing = _safe_int(stat.get("caughtStealing"))
    pstat.caught_stealing_percentage = _safe_float(stat.get("caughtStealingPercentage"))
    if not existing:
        db.add(pstat)


# ── Main pipeline ─────────────────────────────────────────────────────

async def load_all() -> dict:
    """Full pipeline: load all MLB data 2006-2026."""
    results = {}

    async with async_session() as db:
        logger.info("=" * 60)
        logger.info("MLB Stats Ingestion Pipeline")
        logger.info("=" * 60)

        # Step 0: Sync teams and seasons
        logger.info("\n[Step 0] Syncing teams and seasons...")
        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        await db.commit()
        logger.info(f"  Teams: {len(team_map)}, Seasons: {len(season_map)}")
        results["teams"] = len(team_map)
        results["seasons"] = len(season_map)

        # Build team abbreviation map
        team_abbr_by_api_id = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}

        # Step 1: Load rosters
        logger.info("\n[Step 1] Loading player rosters...")
        roster_results = await sync_all_rosters(db, team_map)

        total_players = await _count_players(db)
        results["players"] = total_players
        logger.info(f"  Total distinct players: {total_players}")

        # Steps 2-3: Season stats
        logger.info("\n[Steps 2-3] Loading batting & pitching stats...")
        total_batting = 0
        total_pitching = 0

        for year in YEARS:
            season_id = season_map[year]
            logger.info(f"\n  --- {year} ---")

            # Batting
            await load_batting_season(db, year, season_id, team_map, team_abbr_by_api_id)
            r = await db.execute(
                select(MLBBattingStats).where(MLBBattingStats.season_id == season_id)
            )
            year_batting = len(r.scalars().all())
            total_batting += year_batting
            logger.info(f"  Batting {year}: {year_batting} entries")

            # Pitching
            await load_pitching_season(db, year, season_id, team_map)
            r = await db.execute(
                select(MLBPitchingStats).where(MLBPitchingStats.season_id == season_id)
            )
            year_pitching = len(r.scalars().all())
            total_pitching += year_pitching
            logger.info(f"  Pitching {year}: {year_pitching} entries")

        results["batting_stats"] = total_batting
        results["pitching_stats"] = total_pitching

        # Step 4: Game schedules
        logger.info("\n[Step 4] Loading game schedules...")
        total_games = 0
        for year in YEARS:
            season_id = season_map[year]
            games = await load_games_for_season(db, year, season_id, team_map, team_abbr_by_api_id)
            total_games += games
            logger.info(f"  Games {year}: {games}")
        results["games"] = total_games

        # Final counts
        total_batting = (await db.execute(select(MLBBattingStats))).scalars().all()
        total_pitching = (await db.execute(select(MLBPitchingStats))).scalars().all()
        results["final_batting"] = len(total_batting)
        results["final_pitching"] = len(total_pitching)
        logger.info("\n✅ MLB stats ingestion complete!")
        logger.info(f"  Players: {results.get('players', 0)}")
        logger.info(f"  Batting stat entries: {len(total_batting)}")
        logger.info(f"  Pitching stat entries: {len(total_pitching)}")
        logger.info(f"  Games: {results.get('games', 0)}")

    return results


async def _count_players(db: AsyncSession) -> int:
    r = await db.execute(select(MLBPlayer))
    return len(r.scalars().all())


async def load_batting_only() -> dict:
    """Just load batting and pitching stats (rosters must already exist)."""
    results = {}
    async with async_session() as db:
        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        team_abbr_by_api_id = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}

        total_batting = 0
        total_pitching = 0

        for year in YEARS:
            season_id = season_map[year]
            logger.info(f"\n  --- {year} ---")

            await load_batting_season(db, year, season_id, team_map, team_abbr_by_api_id)
            r = await db.execute(
                select(MLBBattingStats).where(MLBBattingStats.season_id == season_id)
            )
            year_batting = len(r.scalars().all())
            total_batting += year_batting
            logger.info(f"  Batting {year}: {year_batting} entries")

            await load_pitching_season(db, year, season_id, team_map)
            r = await db.execute(
                select(MLBPitchingStats).where(MLBPitchingStats.season_id == season_id)
            )
            year_pitching = len(r.scalars().all())
            total_pitching += year_pitching
            logger.info(f"  Pitching {year}: {year_pitching} entries")

        results["batting"] = total_batting
        results["pitching"] = total_pitching
    return results


async def load_games_only() -> dict:
    """Just load game schedules (requires teams/seasons to exist)."""
    total = 0
    async with async_session() as db:
        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        team_abbr_by_api_id = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}

        for year in YEARS:
            season_id = season_map[year]
            games = await load_games_for_season(db, year, season_id, team_map, team_abbr_by_api_id)
            total += games
            logger.info(f"  {year}: {games} games")
    return total


async def update_probable_pitchers(db: AsyncSession) -> dict:
    """
    Fetch probable pitchers for upcoming (SCHEDULED) games from MLB Stats API.
    Uses the schedule endpoint with hydrate=probablePitcher.
    Returns {"games_updated": int, "updated_game_ids": list[int]}.
    """
    from datetime import date, timedelta
    from sqlalchemy import select
    from app.models.mlb import MLBGames

    today = date.today()
    updated = 0
    updated_game_ids = []

    for offset in range(4):
        check_date = today + timedelta(days=offset)
        date_str = check_date.isoformat()

        url = f"{MLB_API_BASE}/schedule"
        params = {
            "sportId": 1,
            "date": date_str,
            "hydrate": "probablePitcher",
        }
        data = await _api_get(url, params)
        if not data:
            continue

        for date_entry in data.get("dates", []):
            for game in date_entry.get("games", []):
                game_pk = game.get("gamePk")
                if not game_pk:
                    continue

                teams_data = game.get("teams", {})
                home = teams_data.get("home", {})
                away = teams_data.get("away", {})

                home_pitcher = home.get("probablePitcher", {}) or {}
                away_pitcher = away.get("probablePitcher", {}) or {}

                home_pitcher_name = home_pitcher.get("fullName")
                away_pitcher_name = away_pitcher.get("fullName")

                if not home_pitcher_name and not away_pitcher_name:
                    continue

                r = await db.execute(
                    select(MLBGames).where(MLBGames.mlb_game_id == game_pk)
                )
                db_game = r.scalar_one_or_none()
                if not db_game:
                    continue

                changed = False
                if home_pitcher_name and db_game.home_pitcher_name != home_pitcher_name:
                    db_game.home_pitcher_name = home_pitcher_name
                    changed = True
                if away_pitcher_name and db_game.away_pitcher_name != away_pitcher_name:
                    db_game.away_pitcher_name = away_pitcher_name
                    changed = True

                if changed:
                    updated += 1
                    updated_game_ids.append(db_game.id)

    await db.commit()
    logger.info(f"Updated probable pitchers for {updated} upcoming games")
    return {"games_updated": updated, "updated_game_ids": updated_game_ids}


async def update_game_statuses(db: AsyncSession, days_back: int = 7, days_forward: int = 3) -> dict:
    """
    Update game statuses, dates, and scores from MLB Stats API.

    Queries the MLB schedule API for a date range and updates any games
    that have changed status (SCHEDULED → FINAL/IN_PROGRESS/POSTPONED),
    dates (rescheduled), or have new score data.

    Returns {games_updated: int, status_changes: {old: new, ...},
             rescheduled: int, scores_updated: int}.
    """
    from datetime import date as _date, timedelta
    from sqlalchemy import select

    today = _date.today()
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_forward)

    updated = 0
    status_changes = {}
    rescheduled = 0
    scores_updated = 0
    not_found = 0

    current = start
    while current <= end:
        date_str = current.isoformat()
        url = f"{MLB_API_BASE}/schedule"
        params = {
            "sportId": 1,
            "date": date_str,
            "hydrate": "probablePitcher",
        }
        data = await _api_get(url, params)
        if data:
            for date_entry in data.get("dates", []):
                for game in date_entry.get("games", []):
                    game_pk = game.get("gamePk")
                    if not game_pk:
                        continue

                    r = await db.execute(
                        select(MLBGames).where(MLBGames.mlb_game_id == game_pk)
                    )
                    db_game = r.scalar_one_or_none()
                    if not db_game:
                        not_found += 1
                        continue

                    status_data = game.get("status", {})
                    status_code = status_data.get("codedGameState", "S")
                    new_status = GameStatus.SCHEDULED
                    if status_code in ("F", "O", "FT"):
                        new_status = GameStatus.FINAL
                    elif status_code in ("I", "M", "MA"):
                        new_status = GameStatus.IN_PROGRESS
                    elif status_code == "PD":
                        new_status = GameStatus.POSTPONED
                    elif status_code in ("C", "CA"):
                        new_status = GameStatus.CANCELLED

                    changed = False

                    if db_game.status != new_status:
                        old_s = db_game.status.value if hasattr(db_game.status, 'value') else str(db_game.status)
                        new_s = new_status.value
                        status_changes[f"{game_pk}"] = {"from": old_s, "to": new_s}
                        db_game.status = new_status
                        changed = True

                    game_date_str = game.get("gameDate")
                    if game_date_str:
                        try:
                            api_date = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
                            db_game_date = db_game.date
                            if db_game_date and abs((api_date - db_game_date).total_seconds()) > 3600:
                                db_game.date = api_date
                                rescheduled += 1
                                changed = True
                        except ValueError:
                            pass

                    teams_data = game.get("teams", {})
                    for side in ("away", "home"):
                        side_data = teams_data.get(side, {})
                        score = side_data.get("score")
                        if score is not None:
                            try:
                                score_val = int(score)
                            except (ValueError, TypeError):
                                continue
                            if side == "away" and db_game.away_score != score_val:
                                db_game.away_score = score_val
                                scores_updated += 1
                                changed = True
                            elif side == "home" and db_game.home_score != score_val:
                                db_game.home_score = score_val
                                scores_updated += 1
                                changed = True

                    if changed:
                        updated += 1

        current += timedelta(days=1)

    # ── Orphan check: past-date SCHEDULED games not found in date-range schedule ──
    # Games that were rescheduled to dates outside the search window won't appear
    # in the schedule API. We find them by their mlb_game_id directly.
    from sqlalchemy import text as _t, String as _Str, cast as _cast
    from app.models.mlb import MLBSeason
    cutoff = today - timedelta(days=2)
    min_year = today.year  # current season only
    orphans = await db.execute(
        select(MLBGames)
        .join(MLBSeason, MLBGames.season_id == MLBSeason.id)
        .where(
            MLBSeason.year >= min_year,
            MLBGames.date < cutoff,
            _cast(MLBGames.status, _Str).in_(['SCHEDULED', 'IN_PROGRESS']),
            MLBGames.mlb_game_id.isnot(None),
        ).order_by(MLBGames.date).limit(100)
    )
    orphan_games = orphans.scalars().all()
    # Also check FINAL games with NULL scores — they got status-set but scores missed
    missing_score_games = await db.execute(
        select(MLBGames)
        .join(MLBSeason, MLBGames.season_id == MLBSeason.id)
        .where(
            MLBSeason.year >= min_year,
            _cast(MLBGames.status, _Str) == 'FINAL',
            MLBGames.mlb_game_id.isnot(None),
            (MLBGames.home_score.is_(None)) | (MLBGames.away_score.is_(None)),
        ).order_by(MLBGames.date).limit(50)
    )
    missing_score_games_list = missing_score_games.scalars().all()
    if missing_score_games_list:
        logger.info(f"  Found {len(missing_score_games_list)} FINAL games with missing scores — fixing...")

    for db_game in orphan_games + missing_score_games_list:
        game_pk = db_game.mlb_game_id
        url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
        data = await _api_get(url)
        if not data:
            continue
        gd = data.get("gameData", {})
        status_data = gd.get("status", {})
        status_code = status_data.get("codedGameState", "S")
        new_status = GameStatus.SCHEDULED
        if status_code in ("F", "O", "FT"):
            new_status = GameStatus.FINAL
        elif status_code in ("I", "M", "MA"):
            new_status = GameStatus.IN_PROGRESS
        elif status_code == "PD":
            new_status = GameStatus.POSTPONED
        elif status_code in ("C", "CA"):
            new_status = GameStatus.CANCELLED

        changed = False
        if db_game.status != new_status:
            old_s = db_game.status.value if hasattr(db_game.status, 'value') else str(db_game.status)
            new_s = new_status.value
            status_changes[f"orphan_{game_pk}"] = {"from": old_s, "to": new_s}
            db_game.status = new_status
            changed = True

        game_date_str = gd.get("datetime", {}).get("dateTime")
        if game_date_str:
            try:
                api_date = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
                db_game_date = db_game.date
                if db_game_date and abs((api_date - db_game_date).total_seconds()) > 3600:
                    db_game.date = api_date
                    rescheduled += 1
                    changed = True
            except ValueError:
                pass

        # Always try to get scores, even if status didn't change
        live_data_ld = data.get("liveData", {})
        linescore = live_data_ld.get("linescore", {})
        ls_teams = linescore.get("teams", {})
        ls_away = ls_teams.get("away", {})
        ls_home = ls_teams.get("home", {})
        away_run = ls_away.get("runs")
        home_run = ls_home.get("runs")
        
        if away_run is not None and home_run is not None:
            try:
                away_val = int(away_run)
                home_val = int(home_run)
            except (ValueError, TypeError):
                away_val = None
                home_val = None
            
            if away_val is not None and home_val is not None:
                if db_game.away_score != away_val:
                    db_game.away_score = away_val
                    scores_updated += 1
                    changed = True
                if db_game.home_score != home_val:
                    db_game.home_score = home_val
                    scores_updated += 1
                    changed = True
        else:
            # Fallback: try teams data from gameData
            teams_data = gd.get("teams", {})
            for side, team_data in [("away", teams_data.get("away", {})), ("home", teams_data.get("home", {}))]:
                score = team_data.get("score")
                if score is not None:
                    try:
                        score_val = int(score)
                    except (ValueError, TypeError):
                        continue
                    if side == "away" and db_game.away_score != score_val:
                        db_game.away_score = score_val
                        scores_updated += 1
                        changed = True
                    elif side == "home" and db_game.home_score != score_val:
                        db_game.home_score = score_val
                        scores_updated += 1
                        changed = True

        # ── Sync team records (wins/losses) from live API ──
        teams_data = gd.get("teams", {})
        away_record = teams_data.get("away", {}).get("leagueRecord", {})
        home_record = teams_data.get("home", {}).get("leagueRecord", {})
        if away_record and home_record:
            aw = _safe_int(away_record.get("wins"))
            al = _safe_int(away_record.get("losses"))
            hw = _safe_int(home_record.get("wins"))
            hl = _safe_int(home_record.get("losses"))
            if aw is not None and al is not None:
                if db_game.away_wins != aw or db_game.away_losses != al:
                    db_game.away_wins = aw
                    db_game.away_losses = al
                    changed = True
            if hw is not None and hl is not None:
                if db_game.home_wins != hw or db_game.home_losses != hl:
                    db_game.home_wins = hw
                    db_game.home_losses = hl
                    changed = True

        if changed:
            updated += 1

    if updated > 0:
        await db.commit()
        logger.info(f"Updated {updated} game statuses: {len(status_changes)} status changes, {rescheduled} rescheduled dates, {scores_updated} scores")
        if orphan_games:
            logger.info(f"  Checked {len(orphan_games)} orphan games (past-date SCHEDULED with no scores)")
    else:
        logger.info("No game status updates needed")

    return {
        "games_updated": updated,
        "status_changes": status_changes,
        "rescheduled": rescheduled,
        "scores_updated": scores_updated,
        "games_not_in_db": not_found,
        "orphans_found": len(orphan_games),
    }


# ── Active Roster Sync (for 30-day active roster + 40-man) ──────────

INJURY_STATUS_MAP = {
    "D7": "Injured 7-Day",
    "D10": "Injured 10-Day",
    "D15": "Injured 15-Day",
    "D60": "Injured 60-Day",
    "IL7": "Injured 7-Day",
    "IL10": "Injured 10-Day",
    "IL15": "Injured 15-Day",
    "IL60": "Injured 60-Day",
    "ILF": "Injured - Full Season",
    "DL7": "Injured 7-Day",
    "DL10": "Injured 10-Day",
    "DL15": "Injured 15-Day",
    "DL60": "Injured 60-Day",
    "DR": "Day-to-Day - Restricted",
    "DEV": "Development List",
    "SUS": "Suspended",
    "RES": "Restricted List",
    "MIL": "Military Leave",
    "BER": "Bereavement List",
    "P": "Paternity Leave",
}

# Team API IDs for all 30 teams
TEAM_API_IDS = [
    108, 109, 110, 111, 112, 113, 114, 115, 116, 117,
    118, 119, 120, 121, 133, 134, 135, 136, 137, 138,
    139, 140, 141, 142, 143, 144, 145, 146, 147, 158,
]

# Map API team ID to our DB abbreviation
API_ID_TO_ABBR = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}


async def sync_team_roster(
    db: AsyncSession,
    api_team_id: int,
    team_db_id: int,
    year: int,
) -> dict:
    """
    Sync the active 40-man roster for one team from MLB Stats API.
    Updates player.team_id and player.status.
    Returns roster info: {active: [...], injured: [...], total: N}
    """
    url = f"{MLB_API_BASE}/teams/{api_team_id}/roster"
    data = await _api_get(url, {"rosterType": "40Man", "season": year})

    result = {"active": [], "injured": [], "total": 0}
    if not data:
        return result

    for entry in data.get("roster", []):
        person = entry.get("person", {})
        person_id = person.get("id")
        if not person_id:
            continue

        full_name = person.get("fullName", "")
        pos_abbr = entry.get("position", {}).get("abbreviation", "")
        jersey = _safe_int(entry.get("jerseyNumber"))
        status_code = entry.get("status", {}).get("code", "")
        status_desc = INJURY_STATUS_MAP.get(status_code, entry.get("status", {}).get("description", status_code or "Active"))

        # Look up our player record
        r = await db.execute(
            select(MLBPlayer).where(MLBPlayer.mlb_id == person_id)
        )
        player = r.scalar_one_or_none()

        if player:
            # Update team and status
            player.team_id = team_db_id
            player.position = POSITION_MAP.get(pos_abbr, pos_abbr)
            player.active = 1
            if status_code and status_code != "A":
                player.status = status_desc
            elif status_code == "A":
                # Clear injury status if now active
                player.status = None
        else:
            # New player — fetch profile
            profile = await _api_get(f"{MLB_API_BASE}/people/{person_id}")
            person_detail = profile["people"][0] if profile and profile.get("people") else None

            bats = None
            throws = None
            height = None
            weight = None
            birth_date = None
            college = None

            if person_detail:
                bats = person_detail.get("batSide", {}).get("code", "").upper() or None
                throws = person_detail.get("pitchHand", {}).get("code", "").upper() or None
                height = _parse_height(person_detail.get("height"))
                weight = _parse_weight(person_detail.get("weight"))
                if person_detail.get("birthDate"):
                    try:
                        birth_date = datetime.strptime(person_detail["birthDate"], "%Y-%m-%d").date()
                    except ValueError:
                        pass
                if person_detail.get("colleges"):
                    college = person_detail["colleges"][0].get("name")

            player = MLBPlayer(
                mlb_id=person_id,
                name=full_name,
                position=POSITION_MAP.get(pos_abbr, pos_abbr),
                team_id=team_db_id,
                jersey_number=jersey,
                bats=bats,
                throws=throws,
                height=height,
                weight=weight,
                birth_date=birth_date,
                college=college,
                active=1,
                status=status_desc if status_code not in ("A", "RM") else None,
            )
            db.add(player)
            await db.flush()

        if status_code in ("A",):
            result["active"].append(full_name)
            # If player had an active injury record, mark it resolved
            if player and player.id:
                await _resolve_active_injuries(db, player.id)
        elif status_code.startswith("D") or status_code in ("ILF", "DR", "SUS", "RES"):
            result["injured"].append({
                "name": full_name,
                "status": status_desc,
                "position": pos_abbr,
            })
            # Upsert injury record
            if player and player.id:
                await _upsert_injury(db, player.id, team_db_id, status_desc)

        if player:
            result["total"] += 1

    return result


async def _upsert_injury(
    db: AsyncSession,
    player_id: int,
    team_id: int,
    injury_type: str,
) -> None:
    """Upsert an active injury record for a player."""
    from app.models.mlb import MLBInjury
    from sqlalchemy import select

    existing = await db.execute(
        select(MLBInjury).where(
            MLBInjury.player_id == player_id,
            MLBInjury.is_active == True,
        )
    )
    injury = existing.scalar_one_or_none()
    if injury:
        if injury.injury_type != injury_type or injury.team_id != team_id:
            injury.injury_type = injury_type
            injury.team_id = team_id
            await db.flush()
    else:
        new_injury = MLBInjury(
            player_id=player_id,
            team_id=team_id,
            injury_type=injury_type,
            is_active=True,
        )
        db.add(new_injury)
        await db.flush()


async def _resolve_active_injuries(
    db: AsyncSession,
    player_id: int,
) -> None:
    """Mark all active injury records for a player as resolved."""
    from app.models.mlb import MLBInjury
    from sqlalchemy import select

    existing = await db.execute(
        select(MLBInjury).where(
            MLBInjury.player_id == player_id,
            MLBInjury.is_active == True,
        )
    )
    for injury in existing.scalars().all():
        injury.is_active = False
        await db.flush()


async def sync_all_team_rosters(
    db: AsyncSession,
    team_map: dict[int, int],
    year: int = 2026,
) -> dict:
    """Sync active 40-man rosters for all 30 MLB teams."""
    total_active = 0
    total_injured = 0
    total_new = 0
    results = {}

    for api_team_id in TEAM_API_IDS:
        team_db_id = team_map.get(api_team_id)
        if not team_db_id:
            continue

        abbr = API_ID_TO_ABBR.get(api_team_id, "?")
        result = await sync_team_roster(db, api_team_id, team_db_id, year)
        results[abbr] = result
        total_active += len(result["active"])
        total_injured += len(result["injured"])
        logger.info(f"  {abbr}: {result['total']} players ({len(result['active'])} active, {len(result['injured'])} IL)")

        # Commit after each team to avoid huge transactions
        try:
            await db.commit()
        except Exception as e:
            logger.error(f"  {abbr} commit failed: {e}")
            await db.rollback()

    logger.info(f"\n✅ Roster sync complete: {total_active} active, {total_injured} IL across all teams")
    results["_summary"] = {"total_active": total_active, "total_injured": total_injured}
    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    asyncio.run(load_all())
