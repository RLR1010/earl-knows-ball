"""Daily MLB player-props + World Series futures ingestion from The Odds API.

Standalone script run by the admin task system (``mlb-props-daily``).

Run:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/run_mlb_props_daily.py

Reads ODDS_API_KEY from backend/.env (loaded by app.db_urls). Fetches player
props for today's MLB games (per-event endpoint) plus the World Series
championship futures, and stores them to mlb.player_daily_props / mlb.team_props.
"""
import asyncio
import logging
from datetime import date

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db_urls import ASYNC_DATABASE_URL
from app.ingestion.odds_common import SportConfig
from app.ingestion.odds_props import MLB_PROP_MARKETS, snapshot_player_props, snapshot_team_futures

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("mlb_props_daily")

# The Odds API MLB franchise names -> our mlb.teams.abbreviation
MLB_TEAM_NAME_MAP = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Oakland Athletics": "OAK", "Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
    "San Francisco Giants": "SF", "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}

WORLD_SERIES_SPORT_KEY = "baseball_mlb_world_series_winner"


def _mlb_config(year: int) -> SportConfig:
    return SportConfig(
        name="MLB",
        odds_key="baseball_mlb",
        schema="mlb",
        bets_table="mlb.betting_lines",
        games="mlb.games",
        seasons="mlb.seasons",
        teams="mlb.teams",
        team_name_map=MLB_TEAM_NAME_MAP,
        year=year,
    )


async def run() -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    year = date.today().year
    cfg = _mlb_config(year)
    async with async_sessionmaker(engine)() as db:
        props = await snapshot_player_props(cfg, db, "", markets=MLB_PROP_MARKETS)
        futures = await snapshot_team_futures(cfg, db, "", WORLD_SERIES_SPORT_KEY)
    await engine.dispose()
    logger.info(f"MLB props result: {props}")
    logger.info(f"MLB futures result: {futures}")


if __name__ == "__main__":
    asyncio.run(run())
