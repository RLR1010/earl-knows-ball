"""Daily NFL player-props + Super Bowl futures ingestion from The Odds API.

Standalone script run by the admin task system (``nfl-props-daily``).

Run:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/run_nfl_props_daily.py

Reads ODDS_API_KEY from backend/.env (loaded by app.db_urls). Fetches player
props for upcoming NFL games (per-event endpoint) plus the Super Bowl
championship futures, and stores them to nfl.player_daily_props / nfl.team_props.

Out of season (no live games), props are skipped gracefully; the Super Bowl
futures still load year-round.
"""
import asyncio
import logging
import os
from datetime import date

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db_urls import ASYNC_DATABASE_URL
from app.ingestion.odds_common import SportConfig
from app.ingestion.odds_props import NFL_PROP_MARKETS, snapshot_player_props, snapshot_team_futures
from app.core.config import settings as app_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nfl_props_daily")

# The Odds API NFL franchise names -> our nfl.teams.abbreviation
NFL_TEAM_NAME_MAP = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}

SUPER_BOWL_SPORT_KEY = "americanfootball_nfl_super_bowl_winner"


def _nfl_config(year: int) -> SportConfig:
    return SportConfig(
        name="NFL",
        odds_key="americanfootball_nfl",
        schema="nfl",
        bets_table="nfl.betting_lines",
        games="nfl.games",
        seasons="nfl.seasons",
        teams="nfl.teams",
        team_name_map=NFL_TEAM_NAME_MAP,
        year=year,
    )


async def run() -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    year = date.today().year
    cfg = _nfl_config(year)
    api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key
    async with async_sessionmaker(engine)() as db:
        props = await snapshot_player_props(cfg, db, api_key, markets=NFL_PROP_MARKETS)
        futures = await snapshot_team_futures(cfg, db, api_key, SUPER_BOWL_SPORT_KEY)
    await engine.dispose()
    logger.info(f"NFL props result: {props}")
    logger.info(f"NFL futures result: {futures}")


if __name__ == "__main__":
    asyncio.run(run())
