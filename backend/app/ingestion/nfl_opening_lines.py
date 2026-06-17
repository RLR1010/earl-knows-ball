"""
Ingest historical NFL opening lines from The Odds API (paid plan).

Queries the historical odds endpoint ~7 days before each week's games
to capture near-opening lines, then stores them in nfl.betting_lines.

Usage:
    python -m app.ingestion.nfl_opening_lines --season 2021
    python -m app.ingestion.nfl_opening_lines --season 2022 --season 2023
    python -m app.ingestion.nfl_opening_lines  (all seasons 2021-2025)
"""
import asyncio, logging, os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('earl.nfl_opening')

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

TEAM_MAP = {
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
    "Oakland Raiders": "LV", "San Diego Chargers": "LAC",
    "St. Louis Rams": "LAR",
}

# Each season: list of (week_num, thursday_date_iso)
SEASON_WEEKS = {
    2021: [(1,"2021-09-09"),(2,"2021-09-16"),(3,"2021-09-23"),(4,"2021-09-30"),
           (5,"2021-10-07"),(6,"2021-10-14"),(7,"2021-10-21"),(8,"2021-10-28"),
           (9,"2021-11-04"),(10,"2021-11-11"),(11,"2021-11-18"),(12,"2021-11-25"),
           (13,"2021-12-02"),(14,"2021-12-09"),(15,"2021-12-16"),(16,"2021-12-23"),
           (17,"2022-01-06"),(18,"2022-01-13")],
    2022: [(1,"2022-09-08"),(2,"2022-09-15"),(3,"2022-09-22"),(4,"2022-09-29"),
           (5,"2022-10-06"),(6,"2022-10-13"),(7,"2022-10-20"),(8,"2022-10-27"),
           (9,"2022-11-03"),(10,"2022-11-10"),(11,"2022-11-17"),(12,"2022-11-24"),
           (13,"2022-12-01"),(14,"2022-12-08"),(15,"2022-12-15"),(16,"2022-12-22"),
           (17,"2023-01-05"),(18,"2023-01-12")],
    2023: [(1,"2023-09-07"),(2,"2023-09-14"),(3,"2023-09-21"),(4,"2023-09-28"),
           (5,"2023-10-05"),(6,"2023-10-12"),(7,"2023-10-19"),(8,"2023-10-26"),
           (9,"2023-11-02"),(10,"2023-11-09"),(11,"2023-11-16"),(12,"2023-11-23"),
           (13,"2023-11-30"),(14,"2023-12-07"),(15,"2023-12-14"),(16,"2023-12-21"),
           (17,"2024-01-04"),(18,"2024-01-11")],
    2024: [(1,"2024-09-05"),(2,"2024-09-12"),(3,"2024-09-19"),(4,"2024-09-26"),
           (5,"2024-10-03"),(6,"2024-10-10"),(7,"2024-10-17"),(8,"2024-10-24"),
           (9,"2024-10-31"),(10,"2024-11-07"),(11,"2024-11-14"),(12,"2024-11-21"),
           (13,"2024-11-28"),(14,"2024-12-05"),(15,"2024-12-12"),(16,"2024-12-19"),
           (17,"2025-01-02"),(18,"2025-01-09")],
    2025: [(1,"2025-09-04"),(2,"2025-09-11"),(3,"2025-09-18"),(4,"2025-09-25"),
           (5,"2025-10-02"),(6,"2025-10-09"),(7,"2025-10-16"),(8,"2025-10-23"),
           (9,"2025-10-30"),(10,"2025-11-06"),(11,"2025-11-13"),(12,"2025-11-20"),
           (13,"2025-11-27"),(14,"2025-12-04"),(15,"2025-12-11"),(16,"2025-12-18"),
           (17,"2026-01-01"),(18,"2026-01-08")],
}

SOURCE_NAME = "the_odds_api_opening"


def implied_prob(american_odds: int) -> float | None:
    if american_odds is None or american_odds == 0:
        return None
    if american_odds > 0:
        return round(100 / (american_odds + 100), 4)
    return round(abs(american_odds) / (abs(american_odds) + 100), 4)


def aggregate_lines(events: list[dict], team_id_map: dict[str, int],
                    existing: set[int]) -> list[dict]:
    """
    Aggregate lines across bookmakers for each game.
    Returns list of dicts: {game_id, spread, over_under, home_moneyline, away_moneyline, ...}
    Averages spread/O/U across books, takes first moneyline.
    """
    games_data: dict[int, list[dict]] = {}

    for ev in events:
        home_abbr = TEAM_MAP.get(ev.get("home_team", ""))
        away_abbr = TEAM_MAP.get(ev.get("away_team", ""))
        if not home_abbr or not away_abbr:
            continue
        home_id = team_id_map.get(home_abbr)
        away_id = team_id_map.get(away_abbr)
        if not home_id or not away_id:
            continue
        key = (home_id, away_id)

        # This game isn't in our DB by team matchup — store by name for later matching
        if key not in games_data:
            games_data[key] = []
            games_data.setdefault(key, [])

        for bk in ev.get("bookmakers", []):
            markets = {m["key"]: m["outcomes"] for m in bk.get("markets", [])}
            entry = {"bk": bk["key"]}

            for o in markets.get("spreads", []):
                if o.get("name") == ev["home_team"]:
                    entry["spread"] = o.get("point")
            for o in markets.get("totals", []):
                if o.get("name") == "Over":
                    entry["over_under"] = o.get("point")
            for o in markets.get("h2h", []):
                if o.get("name") == ev["home_team"]:
                    entry["home_ml"] = o.get("price")
                else:
                    entry["away_ml"] = o.get("price")

            games_data[key].append(entry)

    return games_data


async def run():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, action="append")
    args = parser.parse_args()
    seasons = args.season or [2021, 2022, 2023, 2024, 2025]

    if not ODDS_API_KEY:
        logger.error("ODDS_API_KEY not set in .env")
        return

    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy import text as sql_text
    DB = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"
    engine = create_async_engine(DB)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        logger.info(f"Seasons: {seasons}")
        logger.info(f"API key: {ODDS_API_KEY[:8]}...")

        # Get team ID map
        r = await db.execute(sql_text("SELECT abbreviation, id FROM nfl.teams"))
        team_id_map = {row.abbreviation: row.id for row in r.fetchall()}
        logger.info(f"Teams: {len(team_id_map)}")

        total_lines = 0
        total_skipped = 0
        total_api_calls = 0

        async with httpx.AsyncClient(timeout=60.0) as client:
            for season in seasons:
                weeks = SEASON_WEEKS.get(season, [])
                if not weeks:
                    logger.warning(f"No week data for {season}")
                    continue

                # Get season ID
                r = await db.execute(
                    sql_text("SELECT id FROM nfl.seasons WHERE year = :y"),
                    {"y": season},
                )
                season_row = r.fetchone()
                if not season_row:
                    logger.warning(f"Season {season} not in DB, skipping")
                    continue
                season_id = season_row.id

                # Get all games for this season
                r = await db.execute(
                    sql_text("SELECT id, home_team_id, away_team_id, date FROM nfl.games WHERE season_id = :sid"),
                    {"sid": season_id},
                )
                games = r.fetchall()
                # Index by (home_id, away_id, date_day) for matching
                game_lookup: dict[tuple[int, int, int], int] = {}
                for g in games:
                    day = g.date.toordinal() if hasattr(g.date, 'toordinal') else g.date.timetuple().tm_yday
                    game_lookup[(g.home_team_id, g.away_team_id, g.date.date())] = g.id

                logger.info(f"Season {season}: {len(games)} games, {len(weeks)} weeks")

                for week_num, thurs_str in weeks:
                    thursday = datetime.fromisoformat(thurs_str).replace(tzinfo=timezone.utc)
                    query_date = thursday - timedelta(days=7)
                    date_param = query_date.strftime("%Y-%m-%dT12:00:00Z")

                    logger.info(f"  W{week_num}: {date_param}")

                    try:
                        resp = await client.get(
                            f"{ODDS_API_BASE}/historical/sports/americanfootball_nfl/odds",
                            params={
                                "apiKey": ODDS_API_KEY,
                                "regions": "us",
                                "markets": "h2h,spreads,totals",
                                "oddsFormat": "american",
                                "date": date_param,
                            },
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        total_api_calls += 1
                    except Exception as e:
                        logger.error(f"    API error: {e}")
                        continue

                    events = data.get("data", [])
                    if not events:
                        continue

                    logger.info(f"    {len(events)} events")

                    batch = []
                    # Get existing opening lines for this season to avoid duplicates
                    r = await db.execute(
                        sql_text("SELECT game_id FROM nfl.betting_lines WHERE source = :src AND game_id IN (SELECT id FROM nfl.games WHERE season_id = :sid)"),
                        {"src": SOURCE_NAME, "sid": season_id},
                    )
                    existing = {row.game_id for row in r.fetchall()}

                    for ev in events:
                        home_abbr = TEAM_MAP.get(ev.get("home_team", ""))
                        away_abbr = TEAM_MAP.get(ev.get("away_team", ""))
                        if not home_abbr or not away_abbr:
                            continue
                        home_id = team_id_map.get(home_abbr)
                        away_id = team_id_map.get(away_abbr)
                        if not home_id or not away_id:
                            continue

                        # Match to DB game by home/away + date
                        ct = ev.get("commence_time", "")
                        try:
                            game_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                            game_date = game_dt.date()
                        except:
                            continue

                        gid = game_lookup.get((home_id, away_id, game_date))
                        if not gid:
                            # Try ±1 day (some timezone offsets shift the date)
                            for delta in [timedelta(days=-1), timedelta(days=1)]:
                                alt_date = game_date + delta
                                gid = game_lookup.get((home_id, away_id, alt_date))
                                if gid:
                                    break

                        if not gid:
                            continue

                        # Aggregate lines across all bookmakers
                        spreads = []
                        over_unders = []
                        home_mls = []
                        away_mls = []

                        for bk in ev.get("bookmakers", []):
                            for market in bk.get("markets", []):
                                key = market["key"]
                                outcomes = market.get("outcomes", [])
                                if key == "spreads":
                                    for o in outcomes:
                                        if o.get("name") == ev["home_team"]:
                                            spreads.append(o.get("point"))
                                elif key == "totals":
                                    for o in outcomes:
                                        if o.get("name") == "Over":
                                            over_unders.append(o.get("point"))
                                elif key == "h2h":
                                    for o in outcomes:
                                        if o.get("name") == ev["home_team"]:
                                            home_mls.append(o.get("price"))
                                        elif o.get("name") == ev["away_team"]:
                                            away_mls.append(o.get("price"))

                        if not spreads and not over_unders and not home_mls:
                            continue

                        # Check if already exists
                        if gid in existing:
                            total_skipped += 1
                            continue
                        existing.add(gid)

                        # Average spreads/O/U, take median moneyline
                        spread = sum(spreads) / len(spreads) if spreads else None
                        ou = sum(over_unders) / len(over_unders) if over_unders else None
                        home_ml = sorted(home_mls)[len(home_mls)//2] if home_mls else None
                        away_ml = sorted(away_mls)[len(away_mls)//2] if away_mls else None

                        batch.append({
                            "game_id": gid,
                            "source": SOURCE_NAME,
                            "spread": round(spread, 1) if spread else None,
                            "over_under": round(ou, 1) if ou else None,
                            "home_moneyline": home_ml,
                            "away_moneyline": away_ml,
                            "home_implied_probability": implied_prob(home_ml),
                            "away_implied_probability": implied_prob(away_ml),
                            "recorded_at": datetime.now(timezone.utc),
                        })

                    if batch:
                        await db.execute(
                            sql_text("""
                                INSERT INTO nfl.betting_lines
                                (game_id, source, spread, over_under, home_moneyline, away_moneyline,
                                 home_implied_probability, away_implied_probability, recorded_at)
                                VALUES (:game_id, :source, :spread, :over_under, :home_moneyline, :away_moneyline,
                                        :home_implied_probability, :away_implied_probability, :recorded_at)
                            """),
                            batch,
                        )
                        await db.flush()
                        total_lines += len(batch)
                        logger.info(f"    Inserted {len(batch)} lines")

        await db.commit()
        logger.info(f"\nDone! {total_lines} lines inserted, {total_skipped} skipped, {total_api_calls} API calls")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
