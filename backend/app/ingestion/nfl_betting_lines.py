"""NFL betting lines: fetch current-season lines from The Odds API.

Uses the shared odds-ingest logic in ``odds_common`` so NFL ingests API odds
exactly the same way as MLB and NBA.
"""
import logging

from ..core.config import settings as app_settings
from .odds_common import SportConfig, snapshot_opening_lines

logger = logging.getLogger(__name__)

# Map The Odds API team names (franchise name) to our nfl.teams abbreviations.
NFL_TEAM_NAME_MAP = {
    "Arizona Cardinals": "ARI",
    "Atlanta Falcons": "ATL",
    "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF",
    "Carolina Panthers": "CAR",
    "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN",
    "Cleveland Browns": "CLE",
    "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN",
    "Detroit Lions": "DET",
    "Green Bay Packers": "GB",
    "Houston Texans": "HOU",
    "Indianapolis Colts": "IND",
    "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC",
    "Las Vegas Raiders": "LV",
    "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR",
    "Miami Dolphins": "MIA",
    "Minnesota Vikings": "MIN",
    "New England Patriots": "NE",
    "New Orleans Saints": "NO",
    "New York Giants": "NYG",
    "New York Jets": "NYJ",
    "Philadelphia Eagles": "PHI",
    "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF",
    "Seattle Seahawks": "SEA",
    "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN",
    "Washington Commanders": "WAS",
}


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


async def snapshot_nfl_opening_lines(db, api_key: str = None, days: int = 3):
    """Fetch current-season NFL lines from The Odds API and save per-book rows."""
    import os
    from datetime import date

    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key
    year = date.today().year
    return await snapshot_opening_lines(_nfl_config(year), db, api_key, days=days)
