"""
Find missing MLB opening lines by trying multiple snapshot offsets.
For each game lacking opening data, tries offsets: 12h, 18h, 24h, 36h, 48h before game time.
"""
import asyncio, logging, os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import httpx
from sqlalchemy import text as sql_text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
logger = logging.getLogger('earl.mlb_opening_fill')

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DB_URL = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"

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

TRIAL_OFFSETS = [12, 18, 24, 36, 48]  # hours before game


def implied_prob(american_odds: int) -> float | None:
    if american_odds is None or american_odds == 0: return None
    return round(100 / (american_odds + 100), 4) if american_odds > 0 else round(abs(american_odds) / (abs(american_odds) + 100), 4)


async def run():
    if not ODDS_API_KEY:
        logger.error("ODDS_API_KEY not set"); return

    engine = create_async_engine(DB_URL)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        r = await db.execute(sql_text("SELECT abbreviation, id FROM mlb.teams"))
        team_id_map = {row.abbreviation: row.id for row in r.fetchall()}

        r = await db.execute(sql_text("""
            SELECT g.id, g.date, ht.abbreviation AS ht, at.abbreviation AS at
            FROM mlb.games g
            JOIN mlb.seasons s ON s.id = g.season_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE s.year BETWEEN 2021 AND 2025
              AND g.id NOT IN (SELECT game_id FROM mlb.betting_lines WHERE source = 'the_odds_api_opening')
            ORDER BY g.date
        """))
        missing = r.fetchall()
        logger.info(f"Games missing opening data: {len(missing)}")

        # Build game lookup by (home_abbr, away_abbr, date)
        game_lookup = {}
        for g in missing:
            game_lookup[(g.ht, g.at, g.date.date())] = g
            # Also store reversed (API might list in any order)
            game_lookup[(g.at, g.ht, g.date.date())] = g

    # ── Try each offset ────────────────────────────────────────────────
    Session = async_sessionmaker(engine, expire_on_commit=False)

    insert_sql = """
        INSERT INTO mlb.betting_lines
        (game_id, source, sportsbook, spread, spread_home_odds, spread_away_odds,
         over_under, over_odds, under_odds, home_moneyline, away_moneyline,
         home_implied_probability, away_implied_probability,
         is_opening, api_last_update, recorded_at)
        VALUES (:game_id, :source, :sportsbook, :spread, :spread_home_odds, :spread_away_odds,
                :over_under, :over_odds, :under_odds, :home_moneyline, :away_moneyline,
                :home_implied_probability, :away_implied_probability,
                :is_opening, :api_last_update, :recorded_at)
    """

    total_inserted = 0
    total_calls = 0
    total_found = set()

    async with httpx.AsyncClient(timeout=30.0) as client:
        for offset_hrs in TRIAL_OFFSETS:
            remaining = [g for g in missing if g.id not in total_found]
            if not remaining:
                break

            logger.info(f"\nTrying {offset_hrs}h before: {len(remaining)} games remaining")

            # Group by unique query timestamp (date + offset)
            buckets = defaultdict(list)
            for g in remaining:
                qt = g.date - timedelta(hours=offset_hrs)
                buckets[qt].append(g)

            for bucket_idx, (qt, bucket_games) in enumerate(sorted(buckets.items())):
                if bucket_idx > 0 and bucket_idx % 200 == 0:
                    logger.info(f"  {offset_hrs}h: bucket {bucket_idx}/{len(buckets)} ({total_inserted} lines)")

                date_param = qt.strftime("%Y-%m-%dT%H:%M:%SZ")
                resp = await client.get(
                    f"{ODDS_API_BASE}/historical/sports/baseball_mlb/odds",
                    params={
                        "apiKey": ODDS_API_KEY,
                        "regions": "us",
                        "markets": "h2h,spreads,totals",
                        "oddsFormat": "american",
                        "date": date_param,
                    },
                )
                total_calls += 1
                if resp.status_code != 200:
                    continue

                events = resp.json().get("data", [])
                if not events:
                    continue

                batch = []
                bucket_ids = {g.id for g in bucket_games}

                for ev in events:
                    home_name = ev.get("home_team", "")
                    away_name = ev.get("away_team", "")
                    home_abbr = MLB_TEAM_MAP.get(home_name)
                    away_abbr = MLB_TEAM_MAP.get(away_name)
                    if not home_abbr or not away_abbr:
                        continue

                    ct = ev.get("commence_time", "")
                    try:
                        game_date = datetime.fromisoformat(ct.replace("Z", "+00:00")).date()
                    except:
                        continue

                    # Match to a missing game
                    matched_game = game_lookup.get((home_abbr, away_abbr, game_date))
                    if not matched_game:
                        # Try date ± 1
                        for delta in [timedelta(days=-1), timedelta(days=1)]:
                            matched_game = game_lookup.get((home_abbr, away_abbr, game_date + delta))
                            if matched_game:
                                break

                    if not matched_game or matched_game.id not in bucket_ids:
                        continue

                    gid = matched_game.id
                    if gid in total_found:
                        continue
                    total_found.add(gid)

                    for bk in ev.get("bookmakers", []):
                        row = {
                            "game_id": gid, "source": "the_odds_api_opening",
                            "sportsbook": bk["key"],
                            "spread": None, "spread_home_odds": None, "spread_away_odds": None,
                            "over_under": None, "over_odds": None, "under_odds": None,
                            "home_moneyline": None, "away_moneyline": None,
                            "home_implied_probability": None, "away_implied_probability": None,
                            "is_opening": "t",
                            "api_last_update": qt,
                            "recorded_at": datetime.now(timezone.utc),
                        }

                        for market in bk.get("markets", []):
                            key = market["key"]
                            outcomes = market.get("outcomes", [])
                            if key == "spreads":
                                for o in outcomes:
                                    if o.get("name") == home_name:
                                        row["spread"] = -o.get("point") if o.get("point") is not None else None
                                        row["spread_home_odds"] = o.get("price")
                                    elif o.get("name") == away_name:
                                        row["spread_away_odds"] = o.get("price")
                            elif key == "totals":
                                for o in outcomes:
                                    if o.get("name") == "Over":
                                        row["over_under"] = o.get("point")
                                        row["over_odds"] = o.get("price")
                                    elif o.get("name") == "Under":
                                        row["under_odds"] = o.get("price")
                            elif key == "h2h":
                                for o in outcomes:
                                    price = o.get("price")
                                    if o.get("name") == home_name:
                                        row["home_moneyline"] = price
                                        row["home_implied_probability"] = implied_prob(price)
                                    elif o.get("name") == away_name:
                                        row["away_moneyline"] = price
                                        row["away_implied_probability"] = implied_prob(price)

                        if any([row["spread"] is not None, row["over_under"] is not None, row["home_moneyline"] is not None]):
                            batch.append(row)

                if batch:
                    async with Session() as db:
                        await db.execute(sql_text(insert_sql), batch)
                        await db.commit()
                    total_inserted += len(batch)
                    logger.info(f"  Bucket {bucket_idx}: found {len([g for g in bucket_games if g.id in total_found])} games ({len(batch)} lines)")

    # ── Summary ──
    logger.info(f"\n{'='*50}")
    logger.info(f"MLB Opening Fill Complete")
    logger.info(f"Total lines inserted: {total_inserted}")
    logger.info(f"Total API calls: {total_calls}")
    logger.info(f"Games found: {len(total_found)} / {len(missing)}")

    async with Session() as db:
        for year in [2021, 2022, 2023, 2024, 2025]:
            r = await db.execute(sql_text("""
                SELECT COUNT(DISTINCT g.id) FROM mlb.games g
                JOIN mlb.seasons s ON s.id = g.season_id
                LEFT JOIN mlb.betting_lines op ON op.game_id = g.id AND op.source = 'the_odds_api_opening'
                WHERE s.year = :y AND op.game_id IS NOT NULL
            """), {"y": year})
            done = r.scalar()
            logger.info(f"  {year}: {done}/2430 with opening lines")
        logger.info(f"Done!")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(run())
