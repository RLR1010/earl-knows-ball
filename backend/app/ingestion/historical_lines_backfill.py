"""
Backfill historical opening + closing lines from The Odds API.

Queries the historical odds endpoint at optimal times:
  - Opening: when lines are first posted (Wed before for NFL, day before for MLB/NBA)
  - Closing: 30 min before game time (NFL/NBA) or 60 min (MLB)

Stores per-sportsbook rows with full metadata: spread, spread odds,
O/U, O/U odds, moneylines, implied probabilities, and bookmaker info.

Usage:
    python -m app.ingestion.historical_lines_backfill --sport nfl --season 2023
    python -m app.ingestion.historical_lines_backfill --sport nfl --start 2021 --end 2025
    python -m app.ingestion.historical_lines_backfill --sport mlb --season 2024
    python -m app.ingestion.historical_lines_backfill --sport nba --season 2023 --closing-only
    python -m app.ingestion.historical_lines_backfill --sport nfl --season 2023 --dry-run
"""
import asyncio, logging, os, sys
from datetime import datetime, timedelta, timezone
from typing import Optional
from collections import defaultdict

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger('earl.historical_backfill')

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# ── Team name → abbreviation maps for each sport ────────────────────────

NFL_TEAM_MAP = {
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
    "Washington Football Team": "WAS", "Washington Redskins": "WAS",
    "Oakland Raiders": "LV", "Los Angeles Chargers": "LAC",
    "San Diego Chargers": "LAC", "St. Louis Rams": "LAR",
}

MLB_TEAM_MAP = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
    "Cleveland Indians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET",
    "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL",
    "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "San Francisco Giants": "SF",
    "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}

NBA_TEAM_MAP = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN", "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET", "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Clippers": "LAC",
    "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN", "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX", "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

# ── Sport config ────────────────────────────────────────────────────────

SPORT_CONFIG = {
    "nfl": {
        "api_sport": "americanfootball_nfl",
        "schema": "nfl",
        "team_map": NFL_TEAM_MAP,
        "closing_offset_minutes": 10,    # query 10 min before game
        "opening_days_before": 6,         # query ~6 days before Sunday (Wednesday)
    },
    "mlb": {
        "api_sport": "baseball_mlb",
        "schema": "mlb",
        "team_map": MLB_TEAM_MAP,
        "closing_offset_minutes": 10,     # 10 min before game time
        "opening_days_before": 1,         # query day before (overridden per sport)
    },
    "nba": {
        "api_sport": "basketball_nba",
        "schema": "nba",
        "team_map": NBA_TEAM_MAP,
        "closing_offset_minutes": 15,     # 15 min before tip
        "opening_days_before": 1,         # query day before (handled per-sport)
    },
}


def implied_prob(american_odds: int) -> Optional[float]:
    """Convert American odds to implied probability (0-1)."""
    if american_odds is None or american_odds == 0:
        return None
    if american_odds > 0:
        return round(100 / (american_odds + 100), 4)
    return round(abs(american_odds) / (abs(american_odds) + 100), 4)


def avg_or_none(vals: list) -> Optional[float]:
    return sum(vals) / len(vals) if vals else None


def median_or_none(vals: list) -> Optional[int]:
    if not vals:
        return None
    return sorted(vals)[len(vals) // 2]


def clamp_minutes(minutes: int) -> int:
    """Ensure offset is at least 1 minute."""
    return max(minutes, 1)


async def fetch_historical_odds(
    client: httpx.AsyncClient,
    api_sport: str,
    date_param: str,
    api_key: str,
) -> list[dict]:
    """Fetch historical odds snapshot from The Odds API."""
    url = f"{ODDS_API_BASE}/historical/sports/{api_sport}/odds"
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
        "date": date_param,
    }
    resp = await client.get(url, params=params)
    if resp.status_code == 401:
        logger.error(f"  API 401 — key invalid or historical not available: {resp.text[:200]}")
        return []
    if resp.status_code != 200:
        logger.warning(f"  API {resp.status_code}: {resp.text[:200]}")
        return []
    data = resp.json()
    return data.get("data", [])


def match_event_to_game(
    ev: dict,
    team_map: dict,
    team_id_map: dict,
    game_lookup: dict,
) -> Optional[int]:
    """Match an API event to a DB game_id by teams + date."""
    home_name = ev.get("home_team", "")
    away_name = ev.get("away_team", "")
    home_abbr = team_map.get(home_name)
    away_abbr = team_map.get(away_name)
    if not home_abbr or not away_abbr:
        return None
    home_id = team_id_map.get(home_abbr)
    away_id = team_id_map.get(away_abbr)
    if not home_id or not away_id:
        return None

    ct = ev.get("commence_time", "")
    try:
        game_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        game_date = game_dt.date()
    except (ValueError, TypeError):
        return None

    # Direct match
    gid = game_lookup.get((home_id, away_id, game_date))
    # Try ±1 day offset (timezone issues)
    if not gid:
        for delta in [timedelta(days=-1), timedelta(days=1)]:
            gid = game_lookup.get((home_id, away_id, game_date + delta))
            if gid:
                break
    return gid


def extract_lines(ev: dict) -> list[dict]:
    """
    Extract per-sportsbook lines from an API event.

    Returns list of dicts with keys:
        sportsbook, last_update,
        spread, spread_home_odds, spread_away_odds,
        over_under, over_odds, under_odds,
        home_moneyline, away_moneyline,
        home_implied_probability, away_implied_probability
    """
    home_name = ev.get("home_team", "")
    away_name = ev.get("away_team", "")
    rows = []

    for bk in ev.get("bookmakers", []):
        row = {
            "sportsbook": bk["key"],
            "last_update": bk.get("last_update", ""),
            "spread": None, "spread_home_odds": None, "spread_away_odds": None,
            "over_under": None, "over_odds": None, "under_odds": None,
            "home_moneyline": None, "away_moneyline": None,
            "home_implied_probability": None, "away_implied_probability": None,
        }

        for market in bk.get("markets", []):
            key = market["key"]
            outcomes = market.get("outcomes", [])

            if key == "spreads":
                for o in outcomes:
                    name = o.get("name", "")
                    if name == home_name:
                        # The Odds API: negative = home favorite. DB matches standard convention.
                        row["spread"] = o.get("point") if o.get("point") is not None else None
                        row["spread_home_odds"] = o.get("price")
                    elif name == away_name:
                        row["spread_away_odds"] = o.get("price")

            elif key == "totals":
                for o in outcomes:
                    name = o.get("name", "")
                    if name == "Over":
                        row["over_under"] = o.get("point")
                        row["over_odds"] = o.get("price")
                    elif name == "Under":
                        row["under_odds"] = o.get("price")

            elif key == "h2h":
                for o in outcomes:
                    name = o.get("name", "")
                    price = o.get("price")
                    if name == home_name:
                        row["home_moneyline"] = price
                        row["home_implied_probability"] = implied_prob(price)
                    elif name == away_name:
                        row["away_moneyline"] = price
                        row["away_implied_probability"] = implied_prob(price)

        rows.append(row)

    return rows


def insert_sql(schema: str) -> str:
    """Return the INSERT statement for the given schema."""
    cols = [
        "game_id", "source", "sportsbook",
        "spread", "spread_home_odds", "spread_away_odds",
        "over_under", "over_odds", "under_odds",
        "home_moneyline", "away_moneyline",
        "home_implied_probability", "away_implied_probability",
        "is_opening", "api_last_update", "recorded_at",
    ]
    placeholders = ", ".join(f":{c}" for c in cols)
    return f"""
        INSERT INTO {schema}.betting_lines
        ({', '.join(cols)})
        VALUES ({placeholders})
    """


# ── Opening line timestamps per sport ────────────────────────────────

def get_opening_times_nfl(games: list) -> dict[int, datetime]:
    """
    For NFL: opening lines are typically posted by Wednesday before each week.
    Group games by week (from game.week field) and return a Wednesday query time.
    """
    weeks = defaultdict(list)
    for g in games:
        # Get the season year from the game's date
        year = g.date.year
        # NFL week spans Thu-Wed, so a Dec 30 game might be in week 17 of 2023
        # but we need the year of the Thursday. Use the earlier year if game is in Jan.
        # Simple approach: use the game date year, adjusted for Jan games (which belong
        # to the previous year's season)
        adj_year = year - 1 if g.date.month == 1 and g.week in (17, 18, 19, 20, 21, 22) else year
        weeks[(adj_year, g.week)].append(g)

    opening_times = {}
    for (year, week), week_games in weeks.items():
        # Find the Thursday of this week — it's the earliest game in the week
        dates = sorted(g.date for g in week_games)
        if not dates:
            continue
        first_game = dates[0]
        # Wednesday before at 17:00 UTC (12 PM ET)
        wed = first_game - timedelta(days=1)
        query_time = wed.replace(hour=17, minute=0, second=0, microsecond=0)
        for g in week_games:
            opening_times[g.id] = query_time

    return opening_times


def get_opening_times_other(games: list, days_before: int = 1) -> dict[int, datetime]:
    """
    For NBA: opening lines typically go up the day before.
    Query at 17:00 UTC (12 PM ET / 9 AM PT) the day before.
    """
    times = {}
    for g in games:
        qt = g.date - timedelta(days=days_before)
        query_time = qt.replace(hour=17, minute=0, second=0, microsecond=0)
        times[g.id] = query_time
    return times


async def backfill_sport(
    sport: str,
    start_season: int,
    end_season: int,
    api_key: str,
    dry_run: bool = False,
    skip_opening: bool = False,
    skip_closing: bool = False,
):
    """Main backfill routine for a sport over a range of seasons."""
    cfg = SPORT_CONFIG.get(sport)
    if not cfg:
        logger.error(f"Unknown sport: {sport}")
        return

    api_sport = cfg["api_sport"]
    schema = cfg["schema"]
    team_map = cfg["team_map"]
    close_offset = cfg["closing_offset_minutes"]
    open_days = cfg["opening_days_before"]

    DB_URL = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"
    engine = create_async_engine(DB_URL, pool_size=5, max_overflow=10)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)

    async with SessionMaker() as db:
        # Get team ID map
        r = await db.execute(sql_text(f"SELECT abbreviation, id FROM {schema}.teams"))
        team_id_map = {row.abbreviation: row.id for row in r.fetchall()}

        # Get existing game IDs already stored (per source)
        existing_sources = {}
        for src in ["the_odds_api_opening", "the_odds_api_closing"]:
            r = await db.execute(
                sql_text(f"SELECT DISTINCT game_id FROM {schema}.betting_lines WHERE source = :src"),
                {"src": src},
            )
            existing_sources[src] = {row.game_id for row in r.fetchall()}

        # MLB/NBA don't have a week column; select conditionally
        week_col = "g.week" if schema == "nfl" else "NULL::integer AS week"
        sql = sql_text(f"""
            SELECT g.id, g.date, g.season_id, {week_col}, s.year,
                   ht.abbreviation AS ht, at.abbreviation AS at
            FROM {schema}.games g
            JOIN {schema}.seasons s ON s.id = g.season_id
            JOIN {schema}.teams ht ON ht.id = g.home_team_id
            JOIN {schema}.teams at ON at.id = g.away_team_id
            WHERE s.year BETWEEN :start AND :end
            ORDER BY g.date
        """)
        r = await db.execute(sql, {"start": start_season, "end": end_season})
        all_games = r.fetchall()
        logger.info(f"{sport.upper()} {start_season}-{end_season}: {len(all_games)} games")

        # Build game lookup by (home_id, away_id, date)
        game_lookup = {}
        for g in all_games:
            gid = g.id
            ht_id = team_id_map.get(g.ht)
            at_id = team_id_map.get(g.at)
            if ht_id and at_id:
                game_lookup[(ht_id, at_id, g.date.date())] = gid
                # Also store reverse for matching API events that list away first
                game_lookup[(at_id, ht_id, g.date.date())] = gid

        # Get game-by-id lookup
        game_by_id = {g.id: g for g in all_games}

    async with httpx.AsyncClient(timeout=30.0) as client:
        stats = {
            "api_calls": 0,
            "opening_inserted": 0,
            "closing_inserted": 0,
            "opening_skipped_existing": 0,
            "closing_skipped_existing": 0,
            "opening_no_match": 0,
            "closing_no_match": 0,
            "credits_remaining": 0,
        }

        # ── OPENING LINES ──
        if not skip_opening:
            if sport == "nfl":
                opening_times = get_opening_times_nfl(all_games)
            elif sport == "mlb":
                # MLB: query on game day morning (12:00 UTC = 8 AM ET)
                opening_times = {g.id: g.date.replace(hour=12, minute=0, second=0, microsecond=0) for g in all_games}
            else:
                # NBA: query day before at 12:00 UTC to avoid UTC date wrapping issues
                opening_times = {g.id: (g.date - timedelta(days=open_days)).replace(hour=12, minute=0, second=0, microsecond=0) for g in all_games}

            # Group games by unique opening query timestamps
            time_buckets = defaultdict(list)
            for g in all_games:
                if g.id not in existing_sources.get("the_odds_api_opening", set()):
                    qt = opening_times.get(g.id)
                    if qt:
                        time_buckets[qt].append(g)

            logger.info(f"\nOpening lines: {sum(len(v) for v in time_buckets.values())} games "
                        f"to backfill across {len(time_buckets)} time buckets")

            for bucket_idx, (query_time, bucket_games) in enumerate(sorted(time_buckets.items())):
                date_param = query_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                events = await fetch_historical_odds(client, api_sport, date_param, api_key)
                stats["api_calls"] += 1

                if not events:
                    logger.warning(f"  Opening bucket {bucket_idx}: no events returned")
                    stats["opening_no_match"] += len(bucket_games)
                    continue

                logger.info(f"  Opening bucket {bucket_idx}: {len(events)} events, "
                            f"targeting {len(bucket_games)} games")

                if dry_run:
                    continue

                batch = []
                for ev in events:
                    gid = match_event_to_game(ev, team_map, team_id_map, game_lookup)
                    if not gid:
                        continue

                    game_row = game_by_id.get(gid)
                    if not game_row:
                        continue

                    # Check if this game is in our current bucket
                    if game_row.id not in {gg.id for gg in bucket_games}:
                        continue

                    lines = extract_lines(ev)
                    for line in lines:
                        batch.append(dict(
                            game_id=gid,
                            source="the_odds_api_opening",
                            sportsbook=line["sportsbook"],
                            spread=line["spread"],
                            spread_home_odds=line["spread_home_odds"],
                            spread_away_odds=line["spread_away_odds"],
                            over_under=line["over_under"],
                            over_odds=line["over_odds"],
                            under_odds=line["under_odds"],
                            home_moneyline=line["home_moneyline"],
                            away_moneyline=line["away_moneyline"],
                            home_implied_probability=line["home_implied_probability"],
                            away_implied_probability=line["away_implied_probability"],
                            is_opening="t",
                            api_last_update=(
                                datetime.fromisoformat(line["last_update"].replace("Z", "+00:00"))
                                if line["last_update"] else query_time
                            ),
                            recorded_at=datetime.now(timezone.utc),
                        ))

                if batch:
                    async with SessionMaker() as db:
                        await db.execute(sql_text(insert_sql(schema)), batch)
                        await db.commit()
                    stats["opening_inserted"] += len(batch)
                    if batch:
                        logger.info(f"    Inserted {len(batch)} opening lines")

            logger.info(f"Opening done: {stats['opening_inserted']} lines inserted")

        # ── CLOSING LINES ──
        if not skip_closing:
            # Group games by game_time windows (cluster by start time)
            time_windows = defaultdict(list)
            for g in all_games:
                if g.id not in existing_sources.get("the_odds_api_closing", set()):
                    # Closing query time = game_time - offset
                    close_time = g.date - timedelta(minutes=clamp_minutes(close_offset))
                    # Round to nearest 15 min for NFL/NBA. MLB uses exact times.
                    if sport != 'mlb':
                        close_time = close_time.replace(
                            minute=(close_time.minute // 15) * 15,
                            second=0, microsecond=0,
                        )
                    time_windows[close_time].append(g)

            logger.info(f"\nClosing lines: {sum(len(v) for v in time_windows.values())} games "
                        f"to backfill across {len(time_windows)} time windows")

            for window_idx, (query_time, window_games) in enumerate(sorted(time_windows.items())):
                date_param = query_time.strftime("%Y-%m-%dT%H:%M:%SZ")
                window_game_ids = {g.id for g in window_games}

                events = await fetch_historical_odds(client, api_sport, date_param, api_key)
                stats["api_calls"] += 1

                if not events:
                    logger.warning(f"  Closing window {window_idx} ({date_param}): "
                                   f"no events, {len(window_games)} games missed")
                    stats["closing_no_match"] += len(window_games)
                    continue

                # Log credits
                if stats["credits_remaining"] == 0 and hasattr(events, 'headers'):
                    pass  # credits tracking

                if dry_run:
                    continue

                batch = []
                for ev in events:
                    gid = match_event_to_game(ev, team_map, team_id_map, game_lookup)
                    if not gid or gid not in window_game_ids:
                        continue

                    lines = extract_lines(ev)
                    for line in lines:
                        batch.append(dict(
                            game_id=gid,
                            source="the_odds_api_closing",
                            sportsbook=line["sportsbook"],
                            spread=line["spread"],
                            spread_home_odds=line["spread_home_odds"],
                            spread_away_odds=line["spread_away_odds"],
                            over_under=line["over_under"],
                            over_odds=line["over_odds"],
                            under_odds=line["under_odds"],
                            home_moneyline=line["home_moneyline"],
                            away_moneyline=line["away_moneyline"],
                            home_implied_probability=line["home_implied_probability"],
                            away_implied_probability=line["away_implied_probability"],
                            is_opening="f",
                            api_last_update=(
                                datetime.fromisoformat(line["last_update"].replace("Z", "+00:00"))
                                if line["last_update"] else query_time
                            ),
                            recorded_at=datetime.now(timezone.utc),
                        ))

                if batch:
                    async with SessionMaker() as db:
                        await db.execute(sql_text(insert_sql(schema)), batch)
                        await db.commit()
                    stats["closing_inserted"] += len(batch)
                    logger.info(f"  Closing window {window_idx} ({date_param}): "
                                f"{len(batch)} lines from {len(events)} events")

            logger.info(f"Closing done: {stats['closing_inserted']} lines inserted")

        # ── Summary ──
        logger.info(f"\n{'─' * 60}")
        logger.info(f"{sport.upper()} {start_season}-{end_season} SUMMARY")
        logger.info(f"  API calls:          {stats['api_calls']}")
        logger.info(f"  Opening lines:      {stats['opening_inserted']}")
        logger.info(f"  Closing lines:      {stats['closing_inserted']}")
        if stats["opening_skipped_existing"]:
            logger.info(f"  Opening skipped:    {stats['opening_skipped_existing']}")
        if stats["closing_skipped_existing"]:
            logger.info(f"  Closing skipped:    {stats['closing_skipped_existing']}")

    await engine.dispose()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill historical lines from The Odds API")
    parser.add_argument("--sport", choices=["nfl", "mlb", "nba"], required=True)
    parser.add_argument("--season", type=int, action="append",
                        help="Specific season(s) to backfill")
    parser.add_argument("--start", type=int, default=2021)
    parser.add_argument("--end", type=int, default=2025)
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't insert, just report what would be done")
    parser.add_argument("--closing-only", action="store_true",
                        help="Only backfill closing lines (skip opening)")
    parser.add_argument("--opening-only", action="store_true",
                        help="Only backfill opening lines (skip closing)")
    args = parser.parse_args()

    if not ODDS_API_KEY:
        logger.error("ODDS_API_KEY not set in environment")
        sys.exit(1)

    if args.season:
        seasons = sorted(set(args.season))
    else:
        seasons = list(range(args.start, args.end + 1))

    start_s = min(seasons)
    end_s = max(seasons)

    asyncio.run(backfill_sport(
        sport=args.sport,
        start_season=start_s,
        end_season=end_s,
        api_key=ODDS_API_KEY,
        dry_run=args.dry_run,
        skip_opening=args.closing_only,
        skip_closing=args.opening_only,
    ))


if __name__ == "__main__":
    main()
