"""
Targeted backfill: single API call per date window to cover OAK games.
Much lighter than per_game_backfill — no big loops, no ORM.
"""
import asyncio, logging, os, json
from datetime import datetime, timedelta, timezone
import httpx

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger('earl.backfill_oak')
log = logger.info

ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

# The Odds API uses "Athletics" (just the nickname)
TEAM_MAP = {"Athletics": "OAK", "Milwaukee Brewers": "MIL", "Colorado Rockies": "COL",
    "Pittsburgh Pirates": "PIT", "Los Angeles Angels": "LAA", "San Francisco Giants": "SF",
    "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA", "Detroit Tigers": "DET",
    "Chicago White Sox": "CWS", "Washington Nationals": "WSH", "Arizona Diamondbacks": "ARI",
    "Minnesota Twins": "MIN", "Boston Red Sox": "BOS", "Tampa Bay Rays": "TB",
    "Texas Rangers": "TEX", "Kansas City Royals": "KC", "Houston Astros": "HOU",
    "Baltimore Orioles": "BAL", "Seattle Mariners": "SEA", "Toronto Blue Jays": "TOR",
    "Cleveland Guardians": "CLE"}

DSN = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

async def run():
    import asyncpg
    conn = await asyncpg.connect(DSN)
    
    # Get OAK games missing any lines
    games = await conn.fetch("""
        SELECT g.id, g.date, ht.abbreviation as home, at.abbreviation as away
        FROM mlb.games g
        JOIN mlb.seasons s ON s.id = g.season_id
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE s.year = 2026 AND (ht.abbreviation = 'OAK' OR at.abbreviation = 'OAK')
          AND NOT EXISTS (SELECT 1 FROM mlb.betting_lines bl WHERE bl.game_id = g.id)
        ORDER BY g.date
    """)
    log(f"Found {len(games)} OAK games with no lines")
    
    # Build unique date windows for API calls (one call per snapshot)
    from collections import defaultdict
    date_windows = defaultdict(list)
    for g in games:
        dt = g["date"]
        # Opening: 12 hours before game
        opening = dt - timedelta(hours=12)
        # Closing: 5 minutes before game
        closing = dt - timedelta(minutes=5)
        date_windows[opening.strftime("%Y-%m-%dT%H:%M:%SZ")].append(("opening", g))
        date_windows[closing.strftime("%Y-%m-%dT%H:%M:%SZ")].append(("closing", g))
    
    log(f"Need {len(date_windows)} API calls")
    
    loaded = 0
    async with httpx.AsyncClient(timeout=20) as client:
        for date_str in sorted(date_windows.keys()):
            url = f"{ODDS_API_BASE}/historical/sports/baseball_mlb/odds?apiKey={ODDS_API_KEY}&regions=us&markets=h2h,spreads,totals&oddsFormat=american&date={date_str}"
            resp = await client.get(url)
            if resp.status_code != 200:
                log(f"  {date_str[:10]}: HTTP {resp.status_code}")
                continue
            
            events = resp.json().get("data", [])
            if not events:
                continue
            
            # Build lookup: (home_abbr, away_abbr) → event
            event_map = {}
            for ev in events:
                ha = TEAM_MAP.get(ev.get("home_team", ""))
                aa = TEAM_MAP.get(ev.get("away_team", ""))
                if ha and aa:
                    event_map[(ha, aa)] = ev
            
            for snap_type, g in date_windows[date_str]:
                key = (g["home"], g["away"])
                ev = event_map.get(key)
                if not ev:
                    continue
                
                source = "the_odds_api_opening" if snap_type == "opening" else "the_odds_api_closing"
                
                for bk in ev.get("bookmakers", []):
                    home_name = ev.get("home_team", "")
                    away_name = ev.get("away_team", "")
                    row = {
                        "game_id": g["id"],
                        "source": source,
                        "sportsbook": bk["key"],
                        "spread": None, "over_under": None,
                        "home_moneyline": None, "away_moneyline": None,
                        "home_implied_probability": None, "away_implied_probability": None,
                        "is_opening": "t" if snap_type == "opening" else "f",
                        "recorded_at": datetime.now(timezone.utc),
                    }
                    for market in bk.get("markets", []):
                        key = market["key"]
                        outcomes = market.get("outcomes", [])
                        if key == "spreads":
                            for o in outcomes:
                                nm = o.get("name", "")
                                if nm == home_name:
                                    row["spread"] = o.get("point")
                                elif nm == away_name:
                                    pass
                        elif key == "totals":
                            for o in outcomes:
                                if o.get("name") == "Over":
                                    row["over_under"] = o.get("point")
                        elif key == "h2h":
                            for o in outcomes:
                                nm = o.get("name", "")
                                if nm == home_name:
                                    row["home_moneyline"] = o.get("price")
                                elif nm == away_name:
                                    row["away_moneyline"] = o.get("price")
                    
                    cols = list(row.keys())
                    vals = [row[c] for c in cols]
                    placeholders = ",".join(f"${i+1}" for i in range(len(cols)))
                    try:
                        await conn.execute(
                            f"INSERT INTO mlb.betting_lines ({','.join(cols)}) VALUES ({placeholders})",
                            *vals,
                        )
                        loaded += 1
                    except Exception as e:
                        log(f"  Insert error game {g['id']}: {e}")
            
            await asyncio.sleep(0.05)
    
    log(f"\n✅ Loaded {loaded} lines for OAK games")
    await conn.close()

asyncio.run(run())
