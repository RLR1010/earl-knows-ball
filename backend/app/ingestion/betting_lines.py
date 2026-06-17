"""
Betting lines ingestion pipeline.

Two data sources:
  1. nflverse games.csv — historical lines (spread, O/U, moneyline) back to 2005
  2. The Odds API — current week lines for upcoming games

The nflverse games.csv is the canonical source for historical data.
Our games table was loaded from a different source (ESPN), so we match by
(season + week + home_team + away_team) with historical abbreviation mapping.

Abbreviation mapping (nflverse → our DB):
  LA  → LAR (Los Angeles Rams, 2016+)
  STL → LAR (St. Louis Rams, pre-2016)
  SD  → LAC (San Diego Chargers, pre-2017)
  OAK → LV  (Oakland Raiders, pre-2020)
"""
import csv
import io
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BettingLine, Game, Season, Team

logger = logging.getLogger("earl.betting_lines")

# ── nflverse source ────────────────────────────────────────────────────

NFLVERSE_GAMES_CSV = (
    "https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv"
)

# nflverse abbreviations that differ from our DB
ABBREV_MAP: dict[str, str] = {
    "LA": "LAR",
    "STL": "LAR",
    "SD": "LAC",
    "OAK": "LV",
}


def _map_abbrev(nflverse_abbr: str) -> str:
    """Map nflverse team abbreviation to our DB abbreviation."""
    return ABBREV_MAP.get(nflverse_abbr, nflverse_abbr)


# ── The Odds API ───────────────────────────────────────────────────────

ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# Registration key — user must get their own free key from the-odds-api.com
ODDS_API_KEY = ""  # Set via API param or env


def _implied_probability(american_odds: int) -> Optional[float]:
    """Convert American moneyline odds to implied probability (0-1)."""
    if american_odds == 0 or american_odds is None:
        return None
    if american_odds > 0:
        return round(100 / (american_odds + 100), 4)
    else:
        return round(abs(american_odds) / (abs(american_odds) + 100), 4)


# ── Historical: nflverse games.csv ─────────────────────────────────────


async def _download_games_csv() -> list[dict]:
    """Download the nflverse games.csv and return rows as dicts."""
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(NFLVERSE_GAMES_CSV)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)


async def _get_season_year_map(db: AsyncSession) -> dict[int, int]:
    """Build {year: season_id} map."""
    r = await db.execute(select(Season))
    return {s.year: s.id for s in r.scalars().all()}


async def _get_team_abbrev_map(db: AsyncSession) -> dict[str, int]:
    """Build {abbreviation: team_id} map."""
    r = await db.execute(select(Team))
    return {t.abbreviation: t.id for t in r.scalars().all()}


async def _get_existing_line_keys(db: AsyncSession) -> set[tuple[int, str]]:
    """
    Build set of (game_id, source) already in betting_lines so we skip dupes.
    """
    r = await db.execute(select(BettingLine.game_id, BettingLine.source))
    return set(r.fetchall())


def _safe_float(val: str | None) -> Optional[float]:
    if val is None or val.strip() == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val: str | None) -> Optional[int]:
    if val is None or val.strip() == "":
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


async def ingest_historical_lines(
    db: AsyncSession,
    start_year: int = 2005,
    end_year: Optional[int] = None,
    source_name: str = "nflverse",
) -> dict:
    """
    Ingest historical betting lines from nflverse games.csv.

    Matches games by (season year, week, home_team, away_team) to our existing
    games table. Handles historic team abbreviation changes.

    Returns stats dict.
    """
    if end_year is None:
        end_year = datetime.now(timezone.utc).year

    # Build lookup maps
    season_map = await _get_season_year_map(db)
    team_map = await _get_team_abbrev_map(db)
    existing = await _get_existing_line_keys(db)

    logger.info("Downloading nflverse games.csv...")
    rows = await _download_games_csv()
    logger.info(f"Downloaded {len(rows)} game records")

    # Filter to requested years
    filtered = [r for r in rows if r.get("season") and start_year <= int(r["season"]) <= end_year]
    logger.info(f"Filtered to {len(filtered)} games in {start_year}-{end_year}")

    loaded = 0
    skipped_no_match = 0
    skipped_no_lines = 0
    skipped_duplicate = 0
    errors = 0

    batch_size = 200
    batch = []

    for row in filtered:
        try:
            season_year = int(row["season"])
            week = int(row["week"])
            nfl_away = row.get("away_team", "").strip()
            nfl_home = row.get("home_team", "").strip()

            # Check if this game has any line data
            spread = _safe_float(row.get("spread_line"))
            total = _safe_float(row.get("total_line"))
            home_ml = _safe_int(row.get("home_moneyline"))
            away_ml = _safe_int(row.get("away_moneyline"))
            home_spread_odds = _safe_int(row.get("home_spread_odds"))
            away_spread_odds = _safe_int(row.get("away_spread_odds"))
            over_odds = _safe_int(row.get("over_odds"))
            under_odds = _safe_int(row.get("under_odds"))

            if spread is None and total is None and home_ml is None and away_ml is None:
                skipped_no_lines += 1
                continue

            away_abbr = _map_abbrev(nfl_away)
            home_abbr = _map_abbrev(nfl_home)

            season_id = season_map.get(season_year)
            if not season_id:
                skipped_no_match += 1
                continue

            home_team_id = team_map.get(home_abbr)
            away_team_id = team_map.get(away_abbr)
            if not home_team_id or not away_team_id:
                skipped_no_match += 1
                continue

            # Find matching game in our DB
            r = await db.execute(
                select(Game).where(
                    Game.season_id == season_id,
                    Game.week == week,
                    Game.home_team_id == home_team_id,
                    Game.away_team_id == away_team_id,
                ).limit(1)
            )
            game = r.scalar_one_or_none()
            if not game:
                skipped_no_match += 1
                continue

            # Check for existing line from this source
            if (game.id, source_name) in existing:
                skipped_duplicate += 1
                continue

            # Compute implied probabilities from moneylines
            home_prob = _implied_probability(home_ml) if home_ml else None
            away_prob = _implied_probability(away_ml) if away_ml else None

            line = BettingLine(
                game_id=game.id,
                source=source_name,
                spread=spread,
                spread_home_odds=home_spread_odds,
                spread_away_odds=away_spread_odds,
                over_under=total,
                over_odds=over_odds,
                under_odds=under_odds,
                home_moneyline=home_ml,
                away_moneyline=away_ml,
                home_implied_probability=home_prob,
                away_implied_probability=away_prob,
                recorded_at=datetime.now(timezone.utc),
            )
            batch.append(line)
            loaded += 1

            if len(batch) >= batch_size:
                db.add_all(batch)
                await db.flush()
                batch = []

                # Track existing keys to avoid future lookups
                for bl in batch:
                    existing.add((bl.game_id, source_name))

                logger.info(f"  {loaded} lines loaded so far...")

        except Exception as e:
            logger.error(f"Error processing row: {e}")
            errors += 1

    # Final flush
    if batch:
        db.add_all(batch)
        await db.flush()

    await db.commit()

    logger.info(
        f"Betting lines: {loaded} loaded, {skipped_no_match} no game match, "
        f"{skipped_no_lines} no line data, {skipped_duplicate} duplicate, {errors} errors"
    )
    return {
        "loaded": loaded,
        "skipped_no_match": skipped_no_match,
        "skipped_no_lines": skipped_no_lines,
        "skipped_duplicate": skipped_duplicate,
        "errors": errors,
        "source": source_name,
    }


# ── Current: The Odds API ──────────────────────────────────────────────


async def ingest_current_lines(
    db: AsyncSession,
    api_key: str,
    source_name: str = "the_odds_api",
    days_from_now: int = 14,
) -> dict:
    """
    Fetch current betting lines from The Odds API for upcoming NFL games.

    The Odds API offers a generous free tier:
      - /v4/sports/americanfootball_nfl/odds — all current lines
      - Free tier: 1,000 requests/month (1 call is enough for all NFL games)
      - Rate limited to 1 req/s

    Returns stats dict. Lines are matched to our games table by team + date.
    """
    if not api_key:
        return {"error": "No API key provided. Get one free at https://the-odds-api.com/", "loaded": 0}

    stats: dict = {"loaded": 0, "games_found": 0, "games_matched": 0, "skipped_duplicate": 0, "errors": 0}

    team_map = await _get_team_abbrev_map(db)
    existing = await _get_existing_line_keys(db)

    # Build reverse map: abbreviation → Game objects per date
    # Group games by date for efficient matching
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_to_plus = (now + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"{ODDS_API_BASE}/sports/americanfootball_nfl/odds"
        f"?regions=us&markets=spreads,h2h,totals"
        f"&oddsFormat=american"
        f"&apiKey={api_key}"
        f"&commenceTimeFrom={date_from}"
        f"&commenceTimeTo={date_to_plus}"
    )

    logger.info(f"Fetching current odds from The Odds API...")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.error(f"Odds API returned {resp.status_code}: {resp.text[:500]}")
                return {"error": f"API error {resp.status_code}", "loaded": 0}

            games_data = resp.json()
    except Exception as e:
        logger.error(f"Odds API request failed: {e}")
        return {"error": str(e), "loaded": 0}

    stats["games_found"] = len(games_data)

    for game_data in games_data:
        try:
            # Parse teams from API response
            home_team = game_data.get("home_team", "")
            away_team = game_data.get("away_team", "")
            commence_time = game_data.get("commence_time", "")

            # The Odds API uses full team names (e.g. "Kansas City Chiefs")
            # Map them to abbreviations using our team map
            home_id = _match_team_by_name(home_team, team_map)
            away_id = _match_team_by_name(away_team, team_map)
            if not home_id or not away_id:
                logger.debug(f"Could not match teams: {away_team} @ {home_team}")
                continue

            # Find matching game in our DB by team IDs + approximate date
            game_date = None
            if commence_time:
                try:
                    game_date = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            if game_date:
                # Match by team IDs and close date (±1 day)
                date_lower = game_date - timedelta(days=1)
                date_upper = game_date + timedelta(days=1)
                r = await db.execute(
                    select(Game).where(
                        Game.home_team_id == home_id,
                        Game.away_team_id == away_id,
                        Game.date >= date_lower,
                        Game.date <= date_upper,
                    ).limit(1)
                )
            else:
                # Match by team IDs only, take nearest future game
                r = await db.execute(
                    select(Game).where(
                        Game.home_team_id == home_id,
                        Game.away_team_id == away_id,
                    ).order_by(Game.date.asc()).limit(1)
                )

            game = r.scalar_one_or_none()
            if not game:
                logger.debug(f"No matching game for {away_team} @ {home_team}")
                stats["games_matched"] += 1  # Actually unmatched, but tracking
                continue

            stats["games_matched"] += 1

            if (game.id, source_name) in existing:
                stats["skipped_duplicate"] += 1
                continue

            # Extract lines from API response
            spread = None
            over_under = None
            home_ml = None
            away_ml = None

            for bookmaker in game_data.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = market.get("key", "")
                    outcomes = market.get("outcomes", [])

                    if key == "spreads":
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                raw_spread = outcome.get("point")
                                # The Odds API: negative point = home team is favorite
                                # Our DB convention (matching nflverse): positive = home favored
                                # So we negate to match nflverse convention
                                spread = -raw_spread if raw_spread is not None else None
                    elif key == "h2h":
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                home_ml = outcome.get("price")
                            elif outcome.get("name") == away_team:
                                away_ml = outcome.get("price")
                    elif key == "totals":
                        for outcome in outcomes:
                            if outcome.get("name") == "Over":
                                over_under = outcome.get("point")

            if spread is None and over_under is None and home_ml is None and away_ml is None:
                continue

            home_prob = _implied_probability(home_ml) if home_ml else None
            away_prob = _implied_probability(away_ml) if away_ml else None

            line = BettingLine(
                game_id=game.id,
                source=source_name,
                spread=spread,
                over_under=over_under,
                home_moneyline=home_ml,
                away_moneyline=away_ml,
                home_implied_probability=home_prob,
                away_implied_probability=away_prob,
                recorded_at=datetime.now(timezone.utc),
            )
            db.add(line)
            existing.add((game.id, source_name))
            stats["loaded"] += 1

        except Exception as e:
            logger.error(f"Error processing Odds API game: {e}")
            stats["errors"] += 1

    await db.commit()
    logger.info(f"Current lines: {stats['loaded']} loaded, {stats['games_matched']} matched")
    return stats


def _match_team_by_name(full_name: str, team_map: dict[str, int]) -> int | None:
    """
    Match a The Odds API full team name to our abbreviation.
    E.g. "Kansas City Chiefs" -> KC
    """
    name_map = {
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
        "Commanders": "WAS", "Raiders": "LV", "Chargers": "LAC", "Rams": "LAR",
    }
    abbr = name_map.get(full_name.strip())
    if abbr:
        return team_map.get(abbr)
    return None


# ── Opening Lines Snapshot ─────────────────────────────────────────────


async def snapshot_opening_lines(
    db: AsyncSession,
    api_key: str,
    days_from_now: int = 14,
) -> dict:
    """
    Snapshot opening lines from The Odds API for upcoming NFL games.

    Designed to run every Tuesday during the NFL season as lines are first
    posted for the upcoming week. Saves lines with source='the_odds_api_opening'
    so they can be distinguished from later snapshots and closing lines.

    Only saves lines for games that don't already have an opening line saved.
    """
    from datetime import timedelta

    if not api_key:
        return {"error": "No API key provided.", "loaded": 0}

    stats: dict = {"loaded": 0, "games_found": 0, "games_matched": 0, "skipped_existing": 0, "errors": 0}

    team_map = await _get_team_abbrev_map(db)
    source_name = "the_odds_api_opening"

    # Only consider games that don't already have an opening line
    r = await db.execute(
        select(BettingLine.game_id)
        .where(BettingLine.source == source_name)
    )
    already_opened = {row[0] for row in r.fetchall()}

    now = datetime.now(timezone.utc)
    date_from = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date_to = (now + timedelta(days=days_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"{ODDS_API_BASE}/sports/americanfootball_nfl/odds"
        f"?regions=us&markets=spreads,h2h,totals"
        f"&oddsFormat=american"
        f"&apiKey={api_key}"
        f"&commenceTimeFrom={date_from}"
        f"&commenceTimeTo={date_to}"
    )

    logger.info("Fetching opening lines from The Odds API...")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.error(f"Odds API returned {resp.status_code}: {resp.text[:500]}")
                return {"error": f"API error {resp.status_code}", "loaded": 0}
            games_data = resp.json()
    except Exception as e:
        logger.error(f"Odds API request failed: {e}")
        return {"error": str(e), "loaded": 0}

    stats["games_found"] = len(games_data)

    for game_data in games_data:
        try:
            home_team = game_data.get("home_team", "")
            away_team = game_data.get("away_team", "")
            commence_time = game_data.get("commence_time", "")

            home_id = _match_team_by_name(home_team, team_map)
            away_id = _match_team_by_name(away_team, team_map)
            if not home_id or not away_id:
                continue

            game_date = None
            if commence_time:
                try:
                    game_date = datetime.fromisoformat(commence_time.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            if game_date:
                date_lower = game_date - timedelta(days=1)
                date_upper = game_date + timedelta(days=1)
                r = await db.execute(
                    select(Game).where(
                        Game.home_team_id == home_id,
                        Game.away_team_id == away_id,
                        Game.date >= date_lower,
                        Game.date <= date_upper,
                    ).limit(1)
                )
            else:
                r = await db.execute(
                    select(Game).where(
                        Game.home_team_id == home_id,
                        Game.away_team_id == away_id,
                    ).order_by(Game.date.asc()).limit(1)
                )

            game = r.scalar_one_or_none()
            if not game:
                continue

            stats["games_matched"] += 1

            if game.id in already_opened:
                stats["skipped_existing"] += 1
                continue

            # Extract opening lines from first bookmaker's markets
            spread = None
            over_under = None
            home_ml = None
            away_ml = None

            for bookmaker in game_data.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = market.get("key", "")
                    outcomes = market.get("outcomes", [])

                    if key == "spreads":
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                raw_spread = outcome.get("point")
                                # The Odds API: negative = home favorite
                                # Our DB convention (matching nflverse): positive = home favored
                                spread = -raw_spread if raw_spread is not None else None
                    elif key == "h2h":
                        for outcome in outcomes:
                            if outcome.get("name") == home_team:
                                home_ml = outcome.get("price")
                            elif outcome.get("name") == away_team:
                                away_ml = outcome.get("price")
                    elif key == "totals":
                        for outcome in outcomes:
                            if outcome.get("name") == "Over":
                                over_under = outcome.get("point")

            if spread is None and over_under is None and home_ml is None and away_ml is None:
                continue

            home_prob = _implied_probability(home_ml) if home_ml else None
            away_prob = _implied_probability(away_ml) if away_ml else None

            line = BettingLine(
                game_id=game.id,
                source=source_name,
                spread=spread,
                over_under=over_under,
                home_moneyline=home_ml,
                away_moneyline=away_ml,
                home_implied_probability=home_prob,
                away_implied_probability=away_prob,
                recorded_at=datetime.now(timezone.utc),
            )
            db.add(line)
            already_opened.add(game.id)
            stats["loaded"] += 1

        except Exception as e:
            logger.error(f"Error processing Odds API game: {e}")
            stats["errors"] += 1

    await db.commit()
    logger.info(f"Opening lines: {stats['loaded']} new, {stats['skipped_existing']} already saved")
    return stats
