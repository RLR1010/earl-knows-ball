"""
DFS salary scraper: DraftKings + FanDuel player salaries → DB.

DraftKings API pattern:
  1. GET /lobby/getcontests?sport=NFL → contests with draftGroupId
  2. GET /draftgroups/v1/{draftGroupId}/draftables → players with salaries

FanDuel API pattern:
  GET /api/fixtures?include_extra=true → contests with player salaries
  (Requires origin header, lightweight anti-bot)

Off-season note: NFL DFS contests aren't active until ~August each year.
When no contests are found, the scraper falls back cleanly with a message.
The pipeline is built; it'll auto-activate when NFL season starts.
"""
import asyncio
import csv
import io
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DfsSalary, Player, Team, Game, Season

logger = logging.getLogger("earl.dfs_salaries")

# ── API endpoints ──────────────────────────────────────────────────────

DK_LOBBY_URL = "https://www.draftkings.com/lobby/getcontests?sport=NFL"
DK_DRAFTABLES_TPL = "https://api.draftkings.com/draftgroups/v1/{}/draftables"
FD_FIXTURES_URL = "https://api.fanduel.com/fixtures?include_extra=true"
FD_MOBILE_URL = "https://mobile-api.fanduel.com/v1/fixtures"

# ── Sample historical data (for off-season testing/verification) ───────
# Week 1, 2024 NFL season — DraftKings main slate
# Selected representative players across positions to keep it small

SAMPLE_DK_DATA = """player_name,position,salary,team,opponent,week,slate_type
Patrick Mahomes,QB,8300,KC,BAL,1,main
Josh Allen,QB,7900,BUF,ARI,1,main
Jalen Hurts,QB,8200,PHI,GB,1,main
Christian McCaffrey,RB,9600,SF,NYJ,1,main
Bijan Robinson,RB,8100,ATL,PIT,1,main
Saquon Barkley,RB,7200,PHI,GB,1,main
Tyreek Hill,WR,9000,MIA,JAX,1,main
Justin Jefferson,WR,8800,MIN,NYG,1,main
Ja'Marr Chase,WR,8400,CIN,NE,1,main
Travis Kelce,TE,7500,KC,BAL,1,main
Sam LaPorta,TE,6800,DET,LAR,1,main
Mark Andrews,TE,6400,BAL,KC,1,main
Jake Moody,K,4800,SF,NYJ,1,main
Harrison Butker,K,4700,KC,BAL,1,main
Justin Tucker,K,5000,BAL,KC,1,main
49ers D/ST,DST,3400,SF,NYJ,1,main
Ravens D/ST,DST,3100,BAL,KC,1,main
Steelers D/ST,DST,2900,PIT,ATL,1,main
"""

SAMPLE_FD_DATA = """player_name,position,salary,team,opponent,week,slate_type
Patrick Mahomes,QB,9000,KC,BAL,1,main
Josh Allen,QB,8700,BUF,ARI,1,main
Jalen Hurts,QB,8800,PHI,GB,1,main
Christian McCaffrey,RB,12000,SF,NYJ,1,main
Bijan Robinson,RB,9500,ATL,PIT,1,main
Saquon Barkley,RB,8500,PHI,GB,1,main
Tyreek Hill,WR,10000,MIA,JAX,1,main
Justin Jefferson,WR,9500,MIN,NYG,1,main
Ja'Marr Chase,WR,9200,CIN,NE,1,main
Travis Kelce,TE,8200,KC,BAL,1,main
Sam LaPorta,TE,7500,DET,LAR,1,main
Mark Andrews,TE,7000,BAL,KC,1,main
"""


# ── Helpers ────────────────────────────────────────────────────────────

TEAM_ABBREV_MAP = {
    # Full names
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


def _abbrev_for_team(name: str) -> str:
    """Map a team name/abbreviation to a standard abbreviation."""
    name = name.strip()
    if name in TEAM_ABBREV_MAP:
        return TEAM_ABBREV_MAP[name]
    # Already an abbreviation?
    if len(name) <= 4 and name.isupper():
        return name
    return name


def _normalize_dk_team(dk_team: str) -> str:
    """Normalize DraftKings team abbreviation."""
    m = {
        "JAC": "JAX", "JAGS": "JAX", "CLV": "CLE",
        "BLT": "BAL", "HST": "HOU", "NWE": "NE",
        "KCC": "KC", "GNB": "GB", "TBB": "TB",
        "SFO": "SF", "NOS": "NO", "LVR": "LV",
        "LAR": "LAR", "LAC": "LAC",
    }
    return m.get(dk_team.upper(), dk_team.upper())


async def _get_lookup_maps(db: AsyncSession) -> dict:
    """Build team, player, season lookup maps."""
    # Team: abbreviation → id
    r = await db.execute(select(Team))
    team_map = {t.abbreviation: t.id for t in r.scalars().all()}

    # Player: name → id (might be fuzzy)
    r = await db.execute(select(Player))
    player_map = {}
    for p in r.scalars().all():
        key = p.name.lower().strip()
        player_map[key] = p.id
        # Also store by name without dots/spaces for fuzzy matching
        clean = re.sub(r'[.\s\'\-]', '', key)
        player_map[clean] = p.id

    # Season: year → id
    r = await db.execute(select(Season))
    season_map = {s.year: s.id for s in r.scalars().all()}

    return {
        "team_map": team_map,
        "player_map": player_map,
        "season_map": season_map,
    }


def _lookup_player_id(name: str, player_map: dict) -> Optional[int]:
    """Fuzzy lookup a player name in our player map."""
    key = name.lower().strip()
    if key in player_map:
        return player_map[key]
    # Try removing suffixes like Jr., Sr., III
    key2 = re.sub(r'\b(jr|sr|ii|iii|iv)\b\.?\s*$', '', key).strip()
    if key2 in player_map:
        return player_map[key2]
    # Try without dots/spaces/hyphens
    key3 = re.sub(r'[.\s\'\-]', '', key)
    if key3 in player_map:
        return player_map[key3]
    # Try first letter firstname + lastname
    parts = key.split()
    if len(parts) >= 2:
        key4 = parts[0][0] + parts[-1]
        if key4 in player_map:
            return player_map[key4]
    return None


def _find_game(team_abbr: str, opponent_abbr: str, year: int, week: int, team_map: dict) -> dict:
    """
    Build a game lookup key. Actual matching happens against the DB.
    Returns a dict with team_ids for later lookup.
    """
    team_id = team_map.get(_abbrev_for_team(team_abbr))
    opp_id = team_map.get(_abbrev_for_team(opponent_abbr))
    return {"team_id": team_id, "opponent_id": opp_id}


async def _match_game(
    db: AsyncSession,
    team_abbr: str,
    opponent_abbr: str,
    season_id: int,
    week: int,
    team_map: dict,
) -> Optional[int]:
    """Find a game in our DB matching (team, opponent, season, week)."""
    team_id = team_map.get(_abbrev_for_team(team_abbr))
    opp_id = team_map.get(_abbrev_for_team(opponent_abbr))

    if not team_id or not opp_id:
        return None

    # Try both directions — the salary data might list the player's team
    # as home or away regardless of actual home/away designation
    r = await db.execute(
        select(Game.id).where(
            Game.season_id == season_id,
            Game.week == week,
            ((Game.home_team_id == team_id) & (Game.away_team_id == opp_id))
            | ((Game.home_team_id == opp_id) & (Game.away_team_id == team_id)),
        ).limit(1)
    )
    return r.scalar_one_or_none()


def _parse_slate_from_game_time(game_time_str: str) -> str:
    """Determine slate from game time."""
    if not game_time_str:
        return "main"
    try:
        dt = datetime.fromisoformat(game_time_str.replace("Z", "+00:00"))
        hour = dt.hour + dt.minute / 60
        # ET assumption for simplicity
        et_hour = (hour - 5) % 24  # approximate ET
        if et_hour < 13:
            return "early"
        elif et_hour < 16:
            return "afternoon"
        elif et_hour < 20:
            return "primetime"
        else:
            return "late"
    except (ValueError, TypeError):
        return "main"


def _parse_dk_commercial_time(dk_time_str: str) -> Optional[datetime]:
    """Parse DK's /Date(ms)/ format."""
    if not dk_time_str:
        return None
    m = re.search(r'/Date\((\d+)\)/', dk_time_str)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc)
    return None


# ── DraftKings scraper ────────────────────────────────────────────────


async def _find_nfl_draft_groups() -> list[dict]:
    """
    Find NFL draft groups from the DK lobby.
    Returns list of {id, name, start_time, game_type}.
    During the off-season, returns empty list (expected).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(DK_LOBBY_URL)
            if resp.status_code != 200:
                logger.warning(f"DK lobby returned {resp.status_code}")
                return []

            data = resp.json()
            contests = data.get("Contests", [])
            if not contests:
                return []

            # Filter to NFL-specific contests (not Madden, not qualifiers)
            nfl_contests = [
                c for c in contests
                if not _is_madden_contest(c.get("n", ""))
                and c.get("gameType") in ("Classic", "NFL Classic", "Best Ball")
            ]

            # If we have Best Ball but no Classic NFL, try Best Ball draft groups
            if not nfl_contests:
                best_ball = [
                    c for c in contests
                    if "best ball" in c.get("n", "").lower()
                    and "madden" not in c.get("n", "").lower()
                ]
                if best_ball:
                    # Best Ball uses different draft groups
                    pass
                return []

            # Extract unique draft group IDs
            seen = set()
            groups = []
            for c in nfl_contests:
                dg_id = c.get("dg")
                if dg_id and dg_id not in seen:
                    seen.add(dg_id)
                    groups.append({
                        "id": dg_id,
                        "name": c.get("n", ""),
                        "start_time": _parse_dk_commercial_time(c.get("sd", "")),
                        "game_type": c.get("gameType", ""),
                    })
            return groups

    except Exception as e:
        logger.error(f"Error finding DK draft groups: {e}")
        return []


def _is_madden_contest(name: str) -> bool:
    """Filter out Madden (video game) contests."""
    return "madden" in name.lower() or "stream" in name.lower()


async def _fetch_dk_draftables(draft_group_id: int) -> list[dict]:
    """Fetch player data from a DK draft group."""
    try:
        url = DK_DRAFTABLES_TPL.format(draft_group_id)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                },
            )
            if resp.status_code != 200:
                logger.warning(f"DK draftables returned {resp.status_code}")
                return []

            data = resp.json()
            return data.get("draftables", [])

    except Exception as e:
        logger.error(f"Error fetching DK draft group {draft_group_id}: {e}")
        return []


async def scrape_draftkings(
    db: AsyncSession,
    clear_existing: bool = False,
    test_mode: bool = False,
) -> dict:
    """
    Scrape player salaries from DraftKings.

    During the NFL season, this finds active contests and pulls salaries.
    In the off-season (May-July), no contests are available.

    Args:
        clear_existing: If True, clears existing DK salaries before loading.
        test_mode: If True, loads sample historical data for verification.

    Returns stats dict.
    """
    stats = {"platform": "draftkings", "players_loaded": 0, "contests_found": 0, "errors": 0}

    if clear_existing:
        await db.execute(delete(DfsSalary).where(DfsSalary.platform == "draftkings"))
        await db.flush()

    maps = await _get_lookup_maps(db)
    team_map = maps["team_map"]
    player_map = maps["player_map"]

    if test_mode:
        logger.info("Using sample DraftKings data (test mode)")
        return await _load_csv_data(
            db=db, platform="draftkings",
            csv_data=SAMPLE_DK_DATA,
            maps=maps,
            stats=stats,
        )

    # Find NFL draft groups
    groups = await _find_nfl_draft_groups()
    stats["contests_found"] = len(groups)

    if not groups:
        logger.info("No NFL DraftKings contests found (expected in off-season). Use test_mode=True to verify pipeline.")
        stats["message"] = "No NFL contests active (off-season). Try test_mode=True."
        return stats

    # Fetch each draft group
    all_players = []
    for group in groups:
        draftables = await _fetch_dk_draftables(group["id"])
        logger.info(f"  DG {group['id']}: {len(draftables)} players")
        if not draftables:
            continue
        all_players.extend(draftables)

        for p in draftables:
            try:
                name = p.get("displayName", p.get("name", ""))
                if not name:
                    continue

                position = p.get("position", "")
                salary = p.get("salary")
                if not position or not salary:
                    continue

                dk_team = p.get("teamAbbreviation", "")
                dk_opponent = p.get("opponent", "")
                team_abbr = _normalize_dk_team(dk_team)
                opp_abbr = _normalize_dk_team(dk_opponent) if dk_opponent else None

                # Derive week/season from group start time
                game_time = group.get("start_time")
                # For the current season
                now = datetime.now(timezone.utc)
                year = now.year if now.month >= 9 else now.year - 1  # NFL season year
                season_id = maps["season_map"].get(year)
                week = None  # Will need to look up from game

                game_id = None
                if team_abbr and opp_abbr and season_id:
                    game_id = await _match_game(db, team_abbr, opp_abbr, season_id, 1, team_map)

                player_id = _lookup_player_id(name, player_map)
                team_id = team_map.get(team_abbr)
                opp_id = team_map.get(opp_abbr) if opp_abbr else None

                slate = _parse_slate_from_game_time(str(game_time)) if game_time else "main"

                salary_entry = DfsSalary(
                    platform="draftkings",
                    player_name=name,
                    player_id=player_id,
                    position=position,
                    salary=int(salary),
                    team_id=team_id,
                    opponent_id=opp_id,
                    game_id=game_id,
                    week=week,
                    season_id=season_id,
                    slate_type=slate,
                    game_time=game_time,
                )
                db.add(salary_entry)
                stats["players_loaded"] += 1

            except Exception as e:
                logger.warning(f"Error processing DK player: {e}")
                stats["errors"] += 1

            if stats["players_loaded"] % 50 == 0:
                await db.flush()

    await db.commit()
    logger.info(f"DraftKings: {stats['players_loaded']} salaries loaded")
    return stats


# ── FanDuel scraper ────────────────────────────────────────────────────

async def _find_fd_fixtures() -> list[dict]:
    """Find active FanDuel NFL fixtures/contests."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Try mobile API first (less bot protection)
            resp = await client.get(
                FD_MOBILE_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Dart/3.0 (dart:io)",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                fixtures = data if isinstance(data, list) else data.get("fixtures", [])
                return [
                    f for f in fixtures
                    if f.get("sport", "").upper() in ("NFL", "FOOTBALL")
                ]

        # Fallback: try web API
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                FD_FIXTURES_URL,
                headers={
                    "Accept": "application/json",
                    "Origin": "https://www.fanduel.com",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                fixtures = data.get("fixtures", [])
                # Filter to NFL — fixture 'sport' field
                return [f for f in fixtures if f.get("sport", "").upper() in ("NFL", "FOOTBALL")]
            else:
                logger.warning(f"FanDuel fixtures returned {resp.status_code}")
                return []

    except Exception as e:
        logger.error(f"Error finding FanDuel fixtures: {e}")
        return []


async def scrape_fanduel(
    db: AsyncSession,
    clear_existing: bool = False,
    test_mode: bool = False,
) -> dict:
    """
    Scrape player salaries from FanDuel.

    During the NFL season, this finds active contests and pulls salaries.
    In the off-season (May-July), no contests are available.

    Args:
        clear_existing: If True, clears existing FD salaries before loading.
        test_mode: If True, loads sample historical data for verification.

    Returns stats dict.
    """
    stats = {"platform": "fanduel", "players_loaded": 0, "contests_found": 0, "errors": 0}

    if clear_existing:
        await db.execute(delete(DfsSalary).where(DfsSalary.platform == "fanduel"))
        await db.flush()

    maps = await _get_lookup_maps(db)
    team_map = maps["team_map"]

    if test_mode:
        logger.info("Using sample FanDuel data (test mode)")
        return await _load_csv_data(
            db=db, platform="fanduel",
            csv_data=SAMPLE_FD_DATA,
            maps=maps,
            stats=stats,
        )

    fixtures = await _find_fd_fixtures()
    stats["contests_found"] = len(fixtures)

    if not fixtures:
        logger.info("No NFL FanDuel contests found (expected in off-season). Use test_mode=True to verify pipeline.")
        stats["message"] = "No NFL contests active (off-season). Try test_mode=True."
        return stats

    # Process each fixture
    for fixture in fixtures:
        fixture_id = fixture.get("id")
        if not fixture_id:
            continue

        # Fetch player list for this fixture
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"https://api.fanduel.com/fixtures/{fixture_id}/player-list",
                    headers={
                        "Accept": "application/json",
                        "Origin": "https://www.fanduel.com",
                    },
                )
                if resp.status_code != 200:
                    continue
                players_data = resp.json()
        except Exception as e:
            logger.warning(f"Error fetching FD player list for fixture {fixture_id}: {e}")
            stats["errors"] += 1
            continue

        players = players_data if isinstance(players_data, list) else players_data.get("players", [])

        for p in players:
            try:
                name = p.get("name", "")
                position = p.get("position", "")
                salary = p.get("salary", {}).get("value")
                team_abbr = p.get("team", {}).get("abbreviation", "")
                opp_abbr = p.get("opponent", {}).get("abbreviation", "")

                if not name or not position or not salary:
                    continue

                team_id = team_map.get(team_abbr.upper())
                opp_id = team_map.get(opp_abbr.upper())
                player_id = _lookup_player_id(name, maps["player_map"])

                salary_entry = DfsSalary(
                    platform="fanduel",
                    player_name=name,
                    player_id=player_id,
                    position=position,
                    salary=int(salary),
                    team_id=team_id,
                    opponent_id=opp_id,
                    scraped_at=datetime.now(timezone.utc),
                )
                db.add(salary_entry)
                stats["players_loaded"] += 1

            except Exception as e:
                logger.warning(f"Error processing FD player: {e}")
                stats["errors"] += 1

    await db.commit()
    logger.info(f"FanDuel: {stats['players_loaded']} salaries loaded")
    return stats


# ── Test mode: CSV loader ──────────────────────────────────────────────

async def _load_csv_data(
    db: AsyncSession,
    platform: str,
    csv_data: str,
    maps: dict,
    stats: dict,
) -> dict:
    """Load DFS salaries from a CSV string (test mode)."""
    team_map = maps["team_map"]
    player_map = maps["player_map"]
    season_map = maps["season_map"]

    reader = csv.DictReader(io.StringIO(csv_data))
    batch = []
    for row in reader:
        try:
            name = row.get("player_name", "").strip()
            pos = row.get("position", "").strip()
            salary_str = row.get("salary", "").strip()
            team_abbr = _normalize_dk_team(row.get("team", "").strip())
            opp_abbr = _normalize_dk_team(row.get("opponent", "").strip())
            week_str = row.get("week", "").strip()
            slate = row.get("slate_type", "main").strip()

            if not name or not pos or not salary_str:
                continue

            salary = int(salary_str)
            week = int(week_str)
            year = 2024  # Sample data is from 2024

            team_id = team_map.get(team_abbr)
            opp_id = team_map.get(opp_abbr)
            season_id = season_map.get(year)
            player_id = _lookup_player_id(name, player_map)

            # Find game
            game_id = None
            if team_id and opp_id and season_id and week:
                game_id = await _match_game(db, team_abbr, opp_abbr, season_id, week, team_map)

            entry = DfsSalary(
                platform=platform,
                player_name=name,
                player_id=player_id,
                position=pos,
                salary=salary,
                team_id=team_id,
                opponent_id=opp_id,
                game_id=game_id,
                week=week,
                season_id=season_id,
                slate_type=slate,
                scraped_at=datetime.now(timezone.utc),
            )
            batch.append(entry)

        except Exception as e:
            logger.warning(f"Error in CSV row: {e}")
            stats["errors"] += 1

    db.add_all(batch)
    await db.commit()
    stats["players_loaded"] = len(batch)
    logger.info(f"{platform}: {len(batch)} sample salaries loaded")
    return stats


# ── Combined scraper ───────────────────────────────────────────────────

async def scrape_all_dfs(
    db: AsyncSession,
    clear_existing: bool = False,
    test_mode: bool = False,
) -> dict:
    """Scrape salaries from all platforms."""
    results = {}

    dk = await scrape_draftkings(db, clear_existing=clear_existing, test_mode=test_mode)
    results["draftkings"] = dk

    fd = await scrape_fanduel(db, clear_existing=clear_existing, test_mode=test_mode)
    results["fanduel"] = fd

    total = results["draftkings"].get("players_loaded", 0) + results["fanduel"].get("players_loaded", 0)
    return {"results": results, "total_loaded": total}
