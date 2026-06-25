"""
Fix 28 games missing closing lines with proper sportsbook, over_odds, under_odds.
Uses psycopg directly for the multi-row insert.
"""
import asyncio, httpx, logging
from datetime import datetime, timedelta, timezone
import asyncpg

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

import os
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "965e3dd1bf2f0813fb208335a18f4ee3")
HISTORICAL_URL = "https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"
REQUIRED_MARKETS = "h2h,spreads,totals"
DATABASE_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

GAME_IDS = [
    34940, 34941, 34962, 34975, 34989,
    35131, 35133, 35163, 35171,
    35449, 35691, 35989,
    36215, 36303, 36801, 36931, 36932,
    37511, 37512, 37751, 37753, 37754, 37755, 37948,
    42110,
    42314, 42425,
    43966,
]


def extract_lines(match, home_team):
    for bm in match.get("bookmakers", []):
        spread = total = ml_home = ml_away = None
        spread_home = spread_away = None
        over_odds = under_odds = None
        for mkt in bm.get("markets", []):
            key = mkt.get("key", "")
            outcomes = mkt.get("outcomes", [])
            if key == "spreads":
                for o in outcomes:
                    name = o.get("name", "")
                    if name == home_team:
                        if spread is None: spread = o.get("point")
                        if spread_home is None: spread_home = o.get("price")
                    else:
                        if spread_away is None: spread_away = o.get("price")
            elif key == "totals":
                for o in outcomes:
                    name = o.get("name", "")
                    if name == "Over":
                        if total is None: total = o.get("point")
                        if over_odds is None: over_odds = o.get("price")
                    elif name == "Under":
                        if under_odds is None: under_odds = o.get("price")
            elif key == "h2h":
                for o in outcomes:
                    name = o.get("name", "")
                    if name == home_team:
                        if ml_home is None: ml_home = o.get("price")
                    else:
                        if ml_away is None: ml_away = o.get("price")
        if spread is not None and total is not None and ml_home is not None:
            return (spread, total, ml_home, ml_away,
                    spread_home, spread_away, over_odds, under_odds, bm.get("title"))
    return None


def gen_timestamps(game_dt):
    """30-min intervals from 48h before to 1h before game time."""
    if game_dt.tzinfo is None:
        game_dt = game_dt.replace(tzinfo=timezone.utc)
    start = game_dt - timedelta(hours=48)
    end = game_dt - timedelta(hours=1)
    ts = start
    while ts <= end:
        yield ts
        ts += timedelta(minutes=30)


async def main():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        # Fetch game details
        rows = await conn.fetch("""
            SELECT g.id, g.date, ht.name as home_name, ht.abbreviation as home_abbr,
                   at.name as away_name, at.abbreviation as away_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.id = ANY($1::int[])
            ORDER BY g.date
        """, GAME_IDS)
        log.info(f"Fetched {len(rows)} games")

        insert_values = []
        found = 0
        missed = 0

        async with httpx.AsyncClient(timeout=30) as client:
            for g in rows:
                game_id, game_date, home_name, home_abbr, away_name, away_abbr = g
                matched = False

                for ts in gen_timestamps(game_date):
                    url = (
                        f"{HISTORICAL_URL}?apiKey={ODDS_API_KEY}"
                        f"&regions=us&markets={REQUIRED_MARKETS}"
                        f"&oddsFormat=american&date={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                    )
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                    except Exception as e:
                        log.warning(f"  Request error at {ts}: {e}")
                        continue

                    for match in data.get("data", []):
                        api_home = match.get("home_team", "")
                        api_away = match.get("away_team", "")
                        if api_home == home_name and api_away == away_name:
                            result = extract_lines(match, home_name)
                            if result:
                                (spread, total, ml_home, ml_away,
                                 spread_home, spread_away, over_odds, under_odds, sportsbook) = result
                                rec_at = (game_date.replace(tzinfo=timezone.utc) if game_date.tzinfo is None else game_date) - timedelta(hours=1)
                                api_last_update = match.get("last_update")
                                insert_values.append((
                                    game_id, spread, spread_home, spread_away,
                                    total, ml_home, ml_away,
                                    over_odds, under_odds, sportsbook, rec_at, api_last_update
                                ))
                                found += 1
                                matched = True
                                log.info(f"  ✅ {game_id} {away_abbr}@{home_abbr} ({str(game_date.date())}) — {sportsbook} spr={spread} OU={total}")
                                break
                    if matched:
                        break

                if not matched:
                    # Try swapped home/away
                    for ts in gen_timestamps(game_date):
                        url = (
                            f"{HISTORICAL_URL}?apiKey={ODDS_API_KEY}"
                            f"&regions=us&markets={REQUIRED_MARKETS}"
                            f"&oddsFormat=american&date={ts.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                        )
                        try:
                            resp = await client.get(url)
                            if resp.status_code != 200:
                                continue
                            data = resp.json()
                        except Exception:
                            continue
                        for match in data.get("data", []):
                            api_home = match.get("home_team", "")
                            api_away = match.get("away_team", "")
                            if api_home == away_name and api_away == home_name:
                                result = extract_lines(match, away_name)
                                if result:
                                    (spread, total, ml_home, ml_away,
                                     spread_home, spread_away, over_odds, under_odds, sportsbook) = result
                                    rec_at = (game_date.replace(tzinfo=timezone.utc) if game_date.tzinfo is None else game_date) - timedelta(hours=1)
                                    api_last_update = match.get("last_update")
                                    insert_values.append((
                                        game_id, spread, spread_home, spread_away,
                                        total, ml_home, ml_away,
                                        over_odds, under_odds, sportsbook, rec_at, api_last_update
                                    ))
                                    found += 1
                                    matched = True
                                    log.info(f"  ✅ {game_id} {away_abbr}@{home_abbr} (swapped) ({str(game_date.date())}) — {sportsbook} spr={spread} OU={total}")
                                    break
                        if matched:
                            break

                if not matched:
                    missed += 1
                    log.warning(f"  ❌ {game_id} {away_abbr}@{home_abbr} ({str(game_date.date())}) — not found")

        log.info(f"\n📊 Found: {found}, Missed: {missed}, Rows ready: {len(insert_values)}")

        if insert_values:
            # Delete any existing closing rows for these games
            ids_to_delete = [v[0] for v in insert_values]
            await conn.execute(
                "DELETE FROM mlb.betting_lines WHERE source = 'the_odds_api_closing' AND game_id = ANY($1::int[])",
                ids_to_delete
            )
            log.info(f"Deleted existing closing lines for {len(ids_to_delete)} games")

            await conn.executemany("""
                INSERT INTO mlb.betting_lines 
                    (game_id, source, spread, spread_home_odds, spread_away_odds,
                     over_under, home_moneyline, away_moneyline,
                     over_odds, under_odds, sportsbook, recorded_at, api_last_update)
                VALUES ($1, 'the_odds_api_closing', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """, insert_values)

            log.info(f"✅ Committed {len(insert_values)} rows for {found} games")

        print(f"\n✅ Final: {found} inserted, {missed} missed")

    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
