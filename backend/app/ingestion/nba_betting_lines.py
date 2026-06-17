"""NBA betting lines ingestion.

Source: The Odds API for current lines (same key as NFL/MLB).
Historical: GitHub dataset or SBR-style CSV for past seasons.
"""
import os
import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBABettingLine, NBAGame, NBATeam, NBASeason

logger = logging.getLogger("earl.nba_betting_lines")

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

NBA_TEAM_NAME_MAP: dict[str, str] = {
    "Atlanta Hawks": "ATL",
    "Boston Celtics": "BOS",
    "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA",
    "Chicago Bulls": "CHI",
    "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL",
    "Denver Nuggets": "DEN",
    "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW",
    "Houston Rockets": "HOU",
    "Indiana Pacers": "IND",
    "LA Clippers": "LAC",
    "Los Angeles Lakers": "LAL",
    "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA",
    "Milwaukee Bucks": "MIL",
    "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP",
    "New York Knicks": "NYK",
    "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL",
    "Philadelphia 76ers": "PHI",
    "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR",
    "Sacramento Kings": "SAC",
    "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR",
    "Utah Jazz": "UTA",
    "Washington Wizards": "WAS",
}

SHORT_NAME_MAP: dict[str, str] = {
    "ATL": "ATL", "BOS": "BOS", "BKN": "BKN", "CHA": "CHA",
    "CHI": "CHI", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GSW": "GSW", "HOU": "HOU", "IND": "IND",
    "LAC": "LAC", "LAL": "LAL", "MEM": "MEM", "MIA": "MIA",
    "MIL": "MIL", "MIN": "MIN", "NOP": "NOP", "NYK": "NYK",
    "OKC": "OKC", "ORL": "ORL", "PHI": "PHI", "PHX": "PHX",
    "POR": "POR", "SAC": "SAC", "SAS": "SAS", "TOR": "TOR",
    "UTA": "UTA", "WAS": "WAS",
}


async def fetch_current_lines(
    db: AsyncSession,
    api_key: str,
    region: str = "us",
    days_from_now: int = 7,
) -> dict:
    """Fetch current NBA lines from The Odds API and store."""
    api_key = api_key or ODDS_API_KEY
    if not api_key:
        return {"error": "No API key provided. Set ODDS_API_KEY in .env or pass api_key param."}

    odds_url = (
        f"{ODDS_API_BASE}/sports/basketball_nba/odds"
        f"?apiKey={api_key}&regions={region}"
        f"&markets=h2h,spreads,totals&oddsFormat=american"
        f"&daysFrom={days_from_now}"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(odds_url)
        if resp.status_code != 200:
            return {"error": f"Odds API returned {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()

    loaded = 0
    skipped = 0

    for event in data:
        try:
            event_id = event.get("id", "")
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = NBA_TEAM_NAME_MAP.get(home_name)
            away_abbr = NBA_TEAM_NAME_MAP.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            # Look up game
            result = await db.execute(
                select(NBAGame).join(NBASeason).where(
                    NBASeason.year == 2025,
                    NBAGame.nba_game_id == event_id,
                )
            )
            game = result.scalar_one_or_none()
            if not game:
                # Try matching by teams + date
                commence = datetime.fromisoformat(event["commence_time"].replace("Z", "+00:00"))
                result = await db.execute(
                    select(NBAGame).join(NBASeason).where(
                        NBASeason.year == 2025,
                        NBAGame.date >= commence - timedelta(hours=6),
                        NBAGame.date <= commence + timedelta(hours=6),
                    )
                )
                game = result.scalar_one_or_none()

            if not game:
                skipped += 1
                continue

            # Check for existing line
            existing = await db.execute(
                select(NBABettingLine).where(
                    NBABettingLine.game_id == game.id,
                    NBABettingLine.source == "the_odds_api_current",
                )
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            # Extract best market prices
            spread, over_under = None, None
            home_ml, away_ml = None, None
            spread_home_odds, spread_away_odds = None, None
            over_odds, under_odds = None, None

            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = market.get("key")
                    outcomes = {o["name"]: o for o in market.get("outcomes", [])}
                    if key == "h2h":
                        home_out = outcomes.get(home_name)
                        away_out = outcomes.get(away_name)
                        if home_out: home_ml = home_out.get("price")
                        if away_out: away_ml = away_out.get("price")
                    elif key == "spreads":
                        home_out = outcomes.get(home_name)
                        away_out = outcomes.get(away_name)
                        if home_out:
                            spread = home_out.get("point")
                            spread_home_odds = home_out.get("price")
                        if away_out:
                            spread_away_odds = away_out.get("price")
                    elif key == "totals":
                        over_out = outcomes.get("Over")
                        under_out = outcomes.get("Under")
                        if over_out:
                            over_under = over_out.get("point")
                            over_odds = over_out.get("price")
                        if under_out:
                            under_odds = under_out.get("price")

            def implied_prob(american_odds):
                if american_odds is None:
                    return None
                if american_odds > 0:
                    return round(100 / (american_odds + 100), 4)
                return round(-american_odds / (-american_odds + 100), 4)

            line = NBABettingLine(
                game_id=game.id,
                source="the_odds_api_current",
                sportsbook="consensus",
                spread=spread,
                spread_home_odds=spread_home_odds,
                spread_away_odds=spread_away_odds,
                over_under=over_under,
                over_odds=over_odds,
                under_odds=under_odds,
                home_moneyline=home_ml,
                away_moneyline=away_ml,
                home_implied_probability=implied_prob(home_ml),
                away_implied_probability=implied_prob(away_ml),
                is_opening="false",
                recorded_at=datetime.now(timezone.utc),
            )
            db.add(line)
            loaded += 1
        except Exception as e:
            logger.warning(f"Error on event: {e}")

    await db.commit()
    logger.info(f"NBA current lines: {loaded} loaded, {skipped} skipped")
    return {"loaded": loaded, "skipped": skipped}


async def snapshot_nba_opening_lines(db: AsyncSession, api_key: str = "") -> dict:
    """Snapshot NBA opening lines from The Odds API (deduplicated by game).
    
    Uses the odds endpoint with `dateFormat=iso` to get opening lines.
    """
    api_key = api_key or ODDS_API_KEY
    if not api_key:
        return {"error": "No API key"}

    odds_url = (
        f"{ODDS_API_BASE}/sports/basketball_nba/odds"
        f"?apiKey={api_key}&regions=us"
        f"&markets=h2h,spreads,totals&oddsFormat=american"
    )

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(odds_url)
        if resp.status_code != 200:
            return {"error": f"API {resp.status_code}: {resp.text[:200]}"}
        data = resp.json()

    loaded = 0
    skipped = 0

    for event in data:
        try:
            event_id = event.get("id", "")
            home_name = event.get("home_team", "")
            away_name = event.get("away_team", "")
            home_abbr = NBA_TEAM_NAME_MAP.get(home_name)
            away_abbr = NBA_TEAM_NAME_MAP.get(away_name)
            if not home_abbr or not away_abbr:
                continue

            result = await db.execute(
                select(NBAGame).where(NBAGame.nba_game_id == event_id)
            )
            game = result.scalar_one_or_none()
            if not game:
                skipped += 1
                continue

            # Check if opening line already saved
            existing = await db.execute(
                select(NBABettingLine).where(
                    NBABettingLine.game_id == game.id,
                    NBABettingLine.source == "the_odds_api_opening",
                )
            )
            if existing.scalar_one_or_none():
                skipped += 1
                continue

            spread, over_under = None, None
            home_ml, away_ml = None, None

            for bookmaker in event.get("bookmakers", []):
                for market in bookmaker.get("markets", []):
                    key = market.get("key")
                    outcomes = {o["name"]: o for o in market.get("outcomes", [])}
                    if key == "h2h":
                        home_out = outcomes.get(home_name)
                        away_out = outcomes.get(away_name)
                        if home_out: home_ml = home_out.get("price")
                        if away_out: away_ml = away_out.get("price")
                    elif key == "spreads":
                        home_out = outcomes.get(home_name)
                        if home_out: spread = home_out.get("point")
                    elif key == "totals":
                        over_out = outcomes.get("Over")
                        if over_out: over_under = over_out.get("point")

            line = NBABettingLine(
                game_id=game.id,
                source="the_odds_api_opening",
                sportsbook="consensus",
                opening_spread=spread,
                opening_total=over_under,
                opening_home_moneyline=home_ml,
                opening_away_moneyline=away_ml,
                is_opening="true",
                recorded_at=datetime.now(timezone.utc),
            )
            db.add(line)
            loaded += 1
        except Exception as e:
            logger.warning(f"Error on opening line: {e}")

    await db.commit()
    return {"loaded": loaded, "skipped": skipped, "total_events": len(data)}


async def quick_test():
    from app.database import async_session
    async with async_session() as db:
        result = await fetch_current_lines(db, api_key="")
        print(result)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(quick_test())
