"""
Get closing lines for 5 games — 10 min before game time.
"""
import asyncio, httpx, logging, os
from datetime import datetime, timedelta, timezone
import asyncpg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

API_KEY = os.environ.get("ODDS_API_KEY", "965e3dd1bf2f0813fb208335a18f4ee3")
DB = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

GAMES = {34962: ("Washington Nationals", "New York Mets"),
         34975: ("Washington Nationals", "New York Mets"),
         35171: ("Oakland Athletics", "Minnesota Twins"),
         36215: ("Tampa Bay Rays", "Cleveland Guardians"),
         37751: ("Chicago Cubs", "Los Angeles Dodgers")}

def extract_lines(match, home_team):
    for bm in match.get("bookmakers", []):
        spread = total = ml_home = ml_away = None
        spread_home = spread_away = over_odds = under_odds = None
        for mkt in bm.get("markets", []):
            key, outcomes = mkt.get("key", ""), mkt.get("outcomes", [])
            if key == "spreads":
                for o in outcomes:
                    if o.get("name") == home_team:
                        if spread is None: spread = o.get("point")
                        if spread_home is None: spread_home = o.get("price")
                    else:
                        if spread_away is None: spread_away = o.get("price")
            elif key == "totals":
                for o in outcomes:
                    if o.get("name") == "Over":
                        if total is None: total = o.get("point")
                        if over_odds is None: over_odds = o.get("price")
                    elif o.get("name") == "Under" and under_odds is None:
                        under_odds = o.get("price")
            elif key == "h2h":
                for o in outcomes:
                    if o.get("name") == home_team:
                        if ml_home is None: ml_home = o.get("price")
                    else:
                        if ml_away is None: ml_away = o.get("price")
        if spread is not None and total is not None and ml_home is not None:
            return (spread, total, ml_home, ml_away, spread_home, spread_away,
                    over_odds, under_odds, bm.get("title"))
    return None

async def main():
    conn = await asyncpg.connect(DB)
    try:
        # get game times
        rows = await conn.fetch("SELECT id, date FROM mlb.games WHERE id = ANY($1::int[])", list(GAMES.keys()))
        insert_values = []
        async with httpx.AsyncClient(timeout=30) as client:
            for gid, gdate in rows:
                home_name, away_name = GAMES[gid]
                if gdate.tzinfo is None:
                    gdt = gdate.replace(tzinfo=timezone.utc)
                else:
                    gdt = gdate
                
                # Try timestamps around game time: -15, -10, -5, and game time
                found = False
                for offset in [-15, -10, -5, 0]:
                    ts = gdt + timedelta(minutes=offset)
                    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                    url = (f"https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"
                           f"?apiKey={API_KEY}"
                           f"&regions=us&markets=h2h,spreads,totals"
                           f"&oddsFormat=american&date={ts_str}")
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            continue
                        data = resp.json()
                    except Exception as e:
                        log.warning(f"Err at {ts_str}: {e}")
                        continue
                    for match in data.get("data", []):
                        if match.get("home_team") == home_name and match.get("away_team") == away_name:
                            result = extract_lines(match, home_name)
                            if result:
                                (spread, total, ml_home, ml_away,
                                 spread_home, spread_away, over_odds, under_odds, sportsbook) = result
                                rec_at = gdt - timedelta(minutes=10)
                                api_last = match.get("last_update")
                                insert_values.append((gid, spread, spread_home, spread_away,
                                                      total, ml_home, ml_away,
                                                      over_odds, under_odds, sportsbook, rec_at, api_last))
                                log.info(f"✅ {gid} {away_name}@{home_name} — {sportsbook} spr={spread} OU={total} (ts={ts_str})")
                                found = True
                                break
                    if found:
                        break
                if not found:
                    log.warning(f"❌ {gid} {away_name}@{home_name} — not found in API")

        if insert_values:
            await conn.execute(
                "DELETE FROM mlb.betting_lines WHERE source='the_odds_api_closing' AND game_id = ANY($1::int[])",
                [v[0] for v in insert_values])
            await conn.executemany("""
                INSERT INTO mlb.betting_lines
                    (game_id, source, spread, spread_home_odds, spread_away_odds,
                     over_under, home_moneyline, away_moneyline,
                     over_odds, under_odds, sportsbook, recorded_at, api_last_update)
                VALUES ($1, 'the_odds_api_closing', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            """, insert_values)
            await conn.execute("""
                UPDATE mlb.betting_lines_consolidated
                SET closing_spread = bl.spread,
                    closing_spread_sportsbook = bl.sportsbook,
                    closing_ou = bl.over_under,
                    closing_ou_sportsbook = bl.sportsbook,
                    closing_home_ml = bl.home_moneyline,
                    closing_away_ml = bl.away_moneyline,
                    closing_over_odds = bl.over_odds,
                    closing_under_odds = bl.under_odds
                FROM mlb.betting_lines bl
                WHERE bl.game_id = mlb.betting_lines_consolidated.game_id
                AND bl.source = 'the_odds_api_closing'
                AND bl.id = (
                    SELECT MIN(bl2.id) FROM mlb.betting_lines bl2
                    WHERE bl2.game_id = bl.game_id AND bl2.source = 'the_odds_api_closing'
                )
            """)
            log.info(f"✅ Inserted {len(insert_values)} rows + updated consolidated")
        else:
            log.info("⚠️ No rows to insert")

    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
