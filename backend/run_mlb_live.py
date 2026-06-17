#!/usr/bin/env python3
"""
MLB Live Pre-game Pipeline

Runs daily to capture opening lines, closing lines (~30 min before game time),
starting lineups, and run predictions.

Usage:
    python3 run_mlb_live.py                           # Morning: opening lines
    python3 run_mlb_live.py --pregame                 # Per-game: closing + lineups + predictions
    python3 run_mlb_live.py --lineups-only            # Just lineups (for testing)
"""
import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("APP_ENV", "production")
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine as _create_async_engine
from app.handicapping.mlb.mlb_engine import MLBHandicapper
from app.models.mlb import MLBSeason

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("earl.mlb_live")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
STATS_API = "https://statsapi.mlb.com"

# Team name -> abbreviation mapping (same as ingestion)
MLB_TEAM_MAP = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL",
    "Baltimore Orioles": "BAL", "Boston Red Sox": "BOS",
    "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE",
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
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WAS",
}

# MLB team ID -> abbreviation
MLB_ID_MAP = {
    108: "LAA", 109: "ARI", 110: "BAL", 111: "BOS",
    112: "CHC", 113: "CIN", 114: "CLE", 115: "COL",
    116: "DET", 117: "HOU", 118: "KC", 119: "LAD",
    120: "WSH", 121: "NYM", 133: "OAK", 134: "PIT",
    135: "SD", 136: "SEA", 137: "SF", 138: "STL",
    139: "TB", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CWS", 146: "MIA",
    147: "NYY", 158: "MIL",
}


def get_api_key(free: bool = True) -> str:
    """Get the appropriate Odds API key."""
    if free:
        return settings.odds_api_key_free or os.environ.get("ODDS_API_KEY_FREE", "")
    return settings.odds_api_key or os.environ.get("ODDS_API_KEY", "")


async def snapshot_opening_lines(game_date: date, api_key: str) -> dict:
    """Pull opening lines from The Odds API for games on a given date."""
    if not api_key:
        return {"error": "No free API key available", "loaded": 0}

    commence_from = game_date.strftime("%Y-%m-%dT10:00:00Z")
    commence_to = (game_date + timedelta(days=1)).strftime("%Y-%m-%dT08:00:00Z")

    url = (
        f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
        f"?regions=us&markets=totals,spreads,h2h"
        f"&oddsFormat=american"
        f"&apiKey={api_key}"
        f"&commenceTimeFrom={commence_from}"
        f"&commenceTimeTo={commence_to}"
    )

    logger.info("Fetching MLB opening lines...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.error(f"  API error: {resp.status_code}")
            return {"error": str(resp.status_code), "loaded": 0}

        games = resp.json()

    # Check remaining credits
    remaining = resp.headers.get("x-requests-remaining", "?")
    logger.info(f"  Found {len(games)} games, credits remaining: {remaining}")

    stats = {"games_found": len(games), "games_matched": 0, "lines_added": 0}
    return stats


async def fetch_mlb_schedule(game_date: date) -> list[dict]:
    """Fetch today's MLB schedule from Stats API."""
    date_str = game_date.strftime("%m/%d/%Y")
    url = f"{STATS_API}/api/v1/schedule?date={date_str}&sportId=1"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.error(f"  Schedule API error: {resp.status_code}")
            return []
        data = resp.json()

    games = []
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            away_id = g["teams"]["away"]["team"]["id"]
            home_id = g["teams"]["home"]["team"]["id"]
            pk = g["gamePk"]
            game_date_str = g.get("gameDate", "")
            status = g.get("status", {}).get("detailedState", "")
            away_name = g["teams"]["away"]["team"]["name"]
            home_name = g["teams"]["home"]["team"]["name"]

            # Probable pitchers
            away_sp = g["teams"]["away"].get("probablePitcher", {})
            home_sp = g["teams"]["home"].get("probablePitcher", {})

            # Parse start time
            try:
                start_dt = datetime.fromisoformat(game_date_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                start_dt = None

            games.append({
                "game_pk": pk,
                "away_id": away_id,
                "home_id": home_id,
                "away_abbr": MLB_ID_MAP.get(away_id),
                "home_abbr": MLB_ID_MAP.get(home_id),
                "away_name": away_name,
                "home_name": home_name,
                "start_time": start_dt,
                "status": status,
                "away_sp_name": away_sp.get("fullName"),
                "away_sp_id": away_sp.get("id"),
                "home_sp_name": home_sp.get("fullName"),
                "home_sp_id": home_sp.get("id"),
            })

    games.sort(key=lambda g: g["start_time"] or datetime.max.replace(tzinfo=timezone.utc))
    return games


async def fetch_closing_line(game: dict, api_key: str) -> Optional[float]:
    """Fetch closing line for a specific game ~30 min before start."""
    if not api_key:
        return None

    # Use commenceTimeFrom/To to target this game
    start = game["start_time"]
    if not start:
        return None

    ct_from = (start - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ct_to = (start + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"{ODDS_API_BASE}/sports/baseball_mlb/odds"
        f"?regions=us&markets=totals"
        f"&oddsFormat=american"
        f"&apiKey={api_key}"
        f"&commenceTimeFrom={ct_from}"
        f"&commenceTimeTo={ct_to}"
    )

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(f"  Closing line API error for {game['away_abbr']}@{game['home_abbr']}: {resp.status_code}")
            return None

        games_data = resp.json()
        if not games_data:
            return None

        # Find matching game
        match = None
        for g in games_data:
            home_abbr = MLB_TEAM_MAP.get(g.get("home_team"))
            away_abbr = MLB_TEAM_MAP.get(g.get("away_team"))
            if home_abbr == game["home_abbr"] and away_abbr == game["away_abbr"]:
                match = g
                break

        if not match:
            return None

        # Extract O/U from first bookmaker's totals market
        for bookmaker in match.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "totals":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            return outcome.get("point")
        return None


async def fetch_lineups(game_pk: int) -> dict:
    """Fetch lineups for a game from MLB Stats API."""
    url = f"{STATS_API}/api/v1.1/game/{game_pk}/feed/live"
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            return {}

    data = resp.json()
    gd = data.get("gameData", {})
    ld = data.get("liveData", {})

    pp = gd.get("probablePitchers", {})
    box = ld.get("boxscore", {})

    result = {
        "away_sp": pp.get("away", {}).get("fullName"),
        "home_sp": pp.get("home", {}).get("fullName"),
        "away_order": [],
        "home_order": [],
    }

    for side in ["away", "home"]:
        team_box = box.get("teams", {}).get(side, {})
        batters = team_box.get("batters", [])
        players = team_box.get("players", {})

        for pid in batters:
            pk = f"ID{pid}"
            p = players.get(pk, {})
            name = p.get("person", {}).get("fullName", "?")
            pos = p.get("position", {}).get("abbreviation", "?")
            order = p.get("battingOrder", 0)
            if order:
                result[f"{side}_order"].append({
                    "name": name, "pos": pos, "order": int(order)
                })

        result[f"{side}_order"].sort(key=lambda x: x["order"])

    return result


async def run_opening_lines():
    """Morning pipeline: fetch opening lines for today."""
    today = date.today()
    api_key = get_api_key(free=True)
    if not api_key:
        logger.error("No free API key available")
        return

    logger.info(f"=== MLB Opening Lines: {today} ===")
    result = await snapshot_opening_lines(today, api_key)
    logger.info(f"Done: {json.dumps(result)}")


async def run_pregame():
    """Per-game pipeline: closing lines + lineups + predictions."""
    today = date.today()
    api_key = get_api_key(free=True)
    now = datetime.now(timezone.utc)
    db_url = settings.database_url

    logger.info(f"=== MLB Pre-game: {today} ===")
    games = await fetch_mlb_schedule(today)
    logger.info(f"  {len(games)} games today")

    processed = 0
    for game in games:
        if not game["start_time"]:
            continue

        mins_until = (game["start_time"] - now).total_seconds() / 60
        logger.info(f"  {game['away_abbr']} @ {game['home_abbr']}: "
                    f"starts {game['start_time'].strftime('%H:%M')} UTC "
                    f"({mins_until:.0f} min from now)")

        # Only process games starting in the next 60-120 minutes
        if mins_until < -120:
            logger.info(f"    Skipping (already started)")
            continue
        if mins_until > 120:
            logger.info(f"    Skipping (too far away)")
            continue

        # 1. Get closing line from Odds API
        logger.info(f"    Fetching closing line...")
        ou = await fetch_closing_line(game, api_key)
        logger.info(f"    Closing O/U: {ou}")

        # 2. Get lineups from MLB Stats API
        logger.info(f"    Fetching lineups...")
        lineups = await fetch_lineups(game["game_pk"])
        away_sp = lineups.get('away_sp', '?')
        home_sp = lineups.get('home_sp', '?')
        away_order = len(lineups.get("away_order", []))
        home_order = len(lineups.get("home_order", []))
        logger.info(f"    SP: {away_sp} @ {home_sp}")
        logger.info(f"    Lineups: {away_order} vs {home_order} batters")

        # 3. Save closing line to betting_lines table
        if ou and game["home_abbr"] and game["away_abbr"]:
            try:
                engine = create_async_engine(db_url)
                async with engine.begin() as conn:
                    # Match game to our DB
                    r = await conn.execute(text("""
                        SELECT g.id FROM mlb.games g
                        JOIN mlb.seasons s ON s.id = g.season_id
                        WHERE s.year = :year
                          AND g.date >= :start AND g.date <= :end
                        ORDER BY abs(EXTRACT(EPOCH FROM g.date - :game_time))
                        LIMIT 1
                    """), {
                        "year": today.year,
                        "start": game["start_time"] - timedelta(hours=6),
                        "end": game["start_time"] + timedelta(hours=6),
                        "game_time": game["start_time"],
                    })
                    row = r.fetchone()
                    if row:
                        game_id = row[0]
                        # Check if closing line already exists
                        r2 = await conn.execute(text("""
                            SELECT id FROM mlb.betting_lines
                            WHERE game_id = :gid AND source = 'mlb_live_pipeline'
                        """), {"gid": game_id})
                        if not r2.fetchone():
                            await conn.execute(text("""
                                INSERT INTO mlb.betting_lines
                                (game_id, source, sportsbook, over_under, is_opening, recorded_at)
                                VALUES (:gid, 'mlb_live_pipeline', 'the_odds_api', :ou, 'false', :now)
                            """), {
                                "gid": game_id, "ou": ou, "now": datetime.now(timezone.utc)
                            })
                            logger.info(f"      Saved closing O/U={ou} for game {game_id}")
                        else:
                            logger.info(f"      Closing line already exists for game {game_id}")
                    else:
                        logger.warning(f"      Could not match game to DB")
                await engine.dispose()
            except Exception as e:
                logger.error(f"      DB error: {e}")

        # 4. Save starting pitchers info
        if away_sp and home_sp:
            try:
                engine2 = create_async_engine(db_url)
                async with engine2.begin() as conn:
                    r = await conn.execute(text("""
                        SELECT g.id FROM mlb.games g
                        JOIN mlb.seasons s ON s.id = g.season_id
                        WHERE s.year = :year AND g.date >= :start AND g.date <= :end
                        ORDER BY abs(EXTRACT(EPOCH FROM g.date - :game_time)) LIMIT 1
                    """), {
                        "year": today.year,
                        "start": game["start_time"] - timedelta(hours=6),
                        "end": game["start_time"] + timedelta(hours=6),
                        "game_time": game["start_time"],
                    })
                    row = r.fetchone()
                    if row:
                        game_id = row[0]
                        await conn.execute(text("""
                            UPDATE mlb.games
                            SET home_pitcher_name = :hsp, away_pitcher_name = :asp
                            WHERE id = :gid
                        """), {
                            "gid": game_id, "hsp": home_sp, "asp": away_sp
                        })
                        logger.info(f"      Saved SP: {away_sp} @ {home_sp} for game {game_id}")
                await engine2.dispose()
            except Exception as e2:
                logger.warning(f"      Could not save SP (maybe no home_pitcher_name column): {e2}")

        # 5. Run prediction via MLBHandicapper (all three models: ATS/OU/ML)
        await run_prediction_for_game(game, db_url)

        processed += 1

    logger.info(f"Pre-game done: {processed} games processed")


async def run_lineups_only():
    """Test: just fetch and print lineups."""
    today = date.today()
    games = await fetch_mlb_schedule(today)
    logger.info(f"=== Lineups for {today}: {len(games)} games ===")

    for game in games:
        if not game["game_pk"]:
            continue
        lineups = await fetch_lineups(game["game_pk"])
        sp_away = lineups.get("away_sp") or "TBD"
        sp_home = lineups.get("home_sp") or "TBD"
        logger.info(f"  {game['away_abbr']} @ {game['home_abbr']}: "
                    f"{sp_away} @ {sp_home}")
        if lineups.get("away_order"):
            top3 = [p["name"] for p in lineups["away_order"][:3]]
            logger.info(f"    Away top 3: {', '.join(top3)}")


async def run_prediction_for_game(game: dict, db_url: str):
    """Run MLBHandicapper prediction for a single game and save to DB."""
    try:
        engine = _create_async_engine(db_url)
        async with AsyncSession(engine) as db:
            date_str = game["start_time"].strftime("%Y-%m-%d")
            handicapper = MLBHandicapper(db)
            cards = await handicapper.handicap_date(date_str, num_games=10)
            gid = None
            for c in cards:
                if hasattr(c, 'game') and hasattr(c.game, 'date'):
                    # Match by teams
                    pass
                if c.away_team == game["away_abbr"] and c.home_team == game["home_abbr"]:
                    gid = c.game_id
                    if c.predicted_home_runs is not None:
                        logger.info(f"      Prediction: {c.away_team} {c.predicted_away_runs:.1f} @ {c.home_team} {c.predicted_home_runs:.1f} (total={c.predicted_total:.1f})")
                        logger.info(f"      Picks: OU={c.over_under_pick} ({c.ou_confidence:.0%}), RL={c.run_line_pick}")
                    break
            if not gid:
                logger.info(f"      No pick card generated for {game['away_abbr']} @ {game['home_abbr']}")
            await db.commit()
        await engine.dispose()
    except Exception as e:
        logger.error(f"      Prediction error: {e}")


if __name__ == "__main__":
    mode = "--pregame" in sys.argv
    lineups_only = "--lineups-only" in sys.argv

    if lineups_only:
        asyncio.run(run_lineups_only())
    elif mode:
        asyncio.run(run_pregame())
    else:
        asyncio.run(run_opening_lines())
