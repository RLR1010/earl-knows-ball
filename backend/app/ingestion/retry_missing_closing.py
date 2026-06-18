#!/usr/bin/env python3
"""
Retry missing MLB closing lines — cached API calls, batch match.
Queries each unique timestamp once, then matches all games against cached data.
"""
import os, sys, asyncio, httpx, logging
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

env_path = os.path.join(os.path.dirname(__file__), '..', '..', '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                if '#' in v: v = v.split('#')[0]
                v = v.strip()
                os.environ.setdefault(k, v)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)

API_KEY = os.environ.get("ODDS_API_KEY")
if not API_KEY: log.error("ODDS_API_KEY not set"); sys.exit(1)

SYNC_DB_URL = os.environ.get("SYNC_DATABASE_URL",
    "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")
BASE_URL = "https://api.the-odds-api.com/v4/historical/sports/baseball_mlb/odds"

TEAM_MAP = {**{
    "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves", "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox", "CHC": "Chicago Cubs", "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians", "COL": "Colorado Rockies", "CWS": "Chicago White Sox",
    "DET": "Detroit Tigers", "HOU": "Houston Astros", "KC": "Kansas City Royals",
    "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers", "MIA": "Miami Marlins",
    "MIL": "Milwaukee Brewers", "MIN": "Minnesota Twins", "NYM": "New York Mets",
    "NYY": "New York Yankees", "OAK": "Oakland Athletics", "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres", "SEA": "Seattle Mariners",
    "SF": "San Francisco Giants", "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays",
    "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays", "WSH": "Washington Nationals",
    "MON": "Montreal Expos",
}}
NAME_TO_ABBR = {v: k for k, v in TEAM_MAP.items()}
NAME_TO_ABBR["Cleveland Indians"] = "CLE"

OFFSETS = [7200, 3600, 10800, 14400, 5400, 18000, 2700]

def make_api_games_map(api_data):
    """Build a lookup: (home_team, away_team) -> entry, for entries with bookmakers."""
    m = {}
    for entry in api_data.get("data", []):
        if entry.get("bookmakers"):
            key = (entry.get("home_team"), entry.get("away_team"))
            m[key] = entry
    return m

def match_team(home_abbr, away_abbr):
    """Return the (home_name, away_name) we expect in API, plus aliases."""
    home_names = [TEAM_MAP.get(home_abbr, ""), home_abbr]
    away_names = [TEAM_MAP.get(away_abbr, ""), away_abbr]
    if home_abbr == "CLE": home_names.append("Cleveland Indians")
    if away_abbr == "CLE": away_names.append("Cleveland Indians")
    return home_names, away_names

def extract_lines(match, home_abbr):
    home_team = match.get("home_team", "")
    spread = total = ml_home = ml_away = None
    spread_home = spread_away = None
    for bm in match.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            key = mkt.get("key", ""); outcomes = mkt.get("outcomes", [])
            if key == "spreads":
                for o in outcomes:
                    if o.get("name") == home_team:
                        spread = o.get("point") if spread is None else spread
                        spread_home = o.get("price") if spread_home is None else spread_home
                    else:
                        spread_away = o.get("price") if spread_away is None else spread_away
            elif key == "totals":
                over = next((o for o in outcomes if o.get("name") == "Over"), None)
                if over and total is None: total = over.get("point")
            elif key == "h2h":
                for o in outcomes:
                    if o.get("name") == home_team:
                        ml_home = o.get("price") if ml_home is None else ml_home
                    else:
                        ml_away = o.get("price") if ml_away is None else ml_away
        if spread is not None and total is not None and ml_home is not None: break
    return spread, total, ml_home, ml_away, spread_home, spread_away

async def main():
    import asyncpg
    conn = await asyncpg.connect(SYNC_DB_URL)
    
    rows = await conn.fetch("""
        SELECT g.id, g.date, ht.abbreviation as home_abbr, at.abbreviation as away_abbr
        FROM mlb.games g
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE NOT EXISTS (
            SELECT 1 FROM mlb.betting_lines WHERE source='the_odds_api_closing' AND game_id = g.id
        )
        AND g.date < NOW()
        AND EXTRACT(YEAR FROM g.date) BETWEEN 2021 AND 2024
        ORDER BY g.date
    """)
    log.info(f"Found {len(rows)} games without closing lines")
    
    # Build per-game timestamps to query (avoid duplicates)
    games = []
    for r in rows:
        gdt = r["date"].replace(tzinfo=timezone.utc) if r["date"].tzinfo is None else r["date"]
        games.append((r["id"], gdt, r["home_abbr"], r["away_abbr"]))
    
    # Build unique timestamps and which games each timestamp serves
    # For each game, we try offsets in order and stop at first hit.
    # We'll query all offsets for all games, caching responses.
    ts_map = {}  # timestamp_str -> (offset_seconds, games_needing_this)
    for gid, gdt, home, away in games:
        for offset in OFFSETS:
            qt = gdt - timedelta(seconds=offset)
            ts_key = qt.strftime("%Y-%m-%dT%H:%M:%SZ")
            if ts_key not in ts_map:
                ts_map[ts_key] = {"offset": offset, "games": []}
            ts_map[ts_key]["games"].append((gid, gdt, home, away))
    
    log.info(f"Will query {len(ts_map)} unique timestamps for {len(games)} games")
    
    cached_responses = {}  # timestamp_str -> api_games_map
    found = 0
    missing = 0
    
    async with httpx.AsyncClient(timeout=30) as client:
        for ts_key, info in sorted(ts_map.items()):
            url = (f"{BASE_URL}?apiKey={API_KEY}&regions=us"
                   f"&markets=h2h,spreads,totals&oddsFormat=american&date={ts_key}")
            try:
                r = await client.get(url)
                if r.status_code == 200:
                    cached_responses[ts_key] = make_api_games_map(r.json())
                else:
                    cached_responses[ts_key] = {}
            except:
                cached_responses[ts_key] = {}
        
        # Now process each game, caching line insertion for batch insert
        insert_values = []
        for gid, gdt, home, away in games:
            match = None
            solved = False
            for offset in OFFSETS:
                qt = gdt - timedelta(seconds=offset)
                ts_key = qt.strftime("%Y-%m-%dT%H:%M:%SZ")
                api_map = cached_responses.get(ts_key, {})
                home_names, away_names = match_team(home, away)
                for hn in home_names:
                    for an in away_names:
                        entry = api_map.get((hn, an)) or api_map.get((an, hn))
                        if entry:
                            match = entry
                            solved = True
                            break
                    if solved: break
                if solved: break
            
            if match:
                spread, total, ml_home, ml_away, spread_home, spread_away = extract_lines(match, home)
                if spread is not None or total is not None or ml_home is not None:
                    rec_at = gdt - timedelta(hours=1)
                    insert_values.append((gid, spread, spread_home, spread_away, total, ml_home, ml_away, rec_at))
                    found += 1
                else:
                    missing += 1
                    log.info(f"  ⚠️ Game {gid}: {away}@{home} ({gdt.date()}) — matched but empty lines")
            else:
                missing += 1
                log.info(f"  ❌ Game {gid}: {away}@{home} ({gdt.date()}) — no closing lines")
    
    # Batch insert all found lines
    if insert_values:
        await conn.executemany("""
            INSERT INTO mlb.betting_lines 
                (game_id, source, spread, spread_home_odds, spread_away_odds,
                 over_under, home_moneyline, away_moneyline, recorded_at)
            VALUES ($1, 'the_odds_api_closing', $2, $3, $4, $5, $6, $7, $8)
        """, insert_values)
        log.info(f"\nInserted {len(insert_values)} closing lines. Missing: {missing}. Total: {len(games)}")
    else:
        log.info(f"\nNo closing lines found. Missing: {missing}")
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
