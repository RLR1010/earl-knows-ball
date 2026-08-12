"""Daily NBA player-props ingestion from The Odds API.

Standalone script run by the admin task system (``nba-props-daily``).

Run:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/run_nba_props_daily.py

Reads ODDS_API_KEY from backend/.env (loaded by app.db_urls). Fetches player
props for upcoming NBA games (per-event endpoint) and stores them to
nba.player_daily_props.

NOTE: out of season (no live NBA games booked), props are skipped gracefully —
this is expected between June and October. The prop-bet writeup pipeline
(`nba/writeups/generator.py::_generate_props_article`) and the
`get_game_player_props` chat tool only light up once these rows exist.
"""
import asyncio
import logging
from datetime import date

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.db_urls import ASYNC_DATABASE_URL
from app.ingestion.odds_common import SportConfig
from app.ingestion.odds_props import NBA_PROP_MARKETS, snapshot_player_props
from app.core.config import settings as app_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nba_props_daily")

# The Odds API NBA franchise names -> our nba.teams.abbreviation
NBA_TEAM_NAME_MAP = {
    "Atlanta Hawks": "ATL", "Boston Celtics": "BOS", "Brooklyn Nets": "BKN",
    "Charlotte Hornets": "CHA", "Chicago Bulls": "CHI", "Cleveland Cavaliers": "CLE",
    "Dallas Mavericks": "DAL", "Denver Nuggets": "DEN", "Detroit Pistons": "DET",
    "Golden State Warriors": "GSW", "Houston Rockets": "HOU", "Indiana Pacers": "IND",
    "LA Clippers": "LAC", "Los Angeles Lakers": "LAL", "Memphis Grizzlies": "MEM",
    "Miami Heat": "MIA", "Milwaukee Bucks": "MIL", "Minnesota Timberwolves": "MIN",
    "New Orleans Pelicans": "NOP", "New York Knicks": "NYK", "Oklahoma City Thunder": "OKC",
    "Orlando Magic": "ORL", "Philadelphia 76ers": "PHI", "Phoenix Suns": "PHX",
    "Portland Trail Blazers": "POR", "Sacramento Kings": "SAC", "San Antonio Spurs": "SAS",
    "Toronto Raptors": "TOR", "Utah Jazz": "UTA", "Washington Wizards": "WAS",
}


def _nba_config(year: int) -> SportConfig:
    return SportConfig(
        name="NBA",
        odds_key="basketball_nba",
        schema="nba",
        bets_table="nba.betting_lines",
        games="nba.games",
        seasons="nba.seasons",
        teams="nba.teams",
        team_name_map=NBA_TEAM_NAME_MAP,
        year=year,
    )


async def run() -> None:
    engine = create_async_engine(ASYNC_DATABASE_URL)
    year = date.today().year
    cfg = _nba_config(year)
    api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key
    async with async_sessionmaker(engine)() as db:
        props = await snapshot_player_props(cfg, db, api_key, markets=NBA_PROP_MARKETS)
    await engine.dispose()
    logger.info(f"NBA props result: {props}")


if __name__ == "__main__":
    import os

    asyncio.run(run())
