"""NBA betting lines: fetch current-season lines from The Odds API.

Uses the shared odds-ingest logic in ``odds_common`` so NBA ingests API odds
exactly the same way as MLB and NFL.
"""
import logging

from ..core.config import settings as app_settings
from .odds_common import SportConfig, snapshot_opening_lines

logger = logging.getLogger(__name__)

# Map The Odds API team names (franchise name) to our nba.teams abbreviations.
# The Odds API often returns "Los Angeles Clippers" / "LA Clippers"; we alias
# both forms to LAC.
NBA_TEAM_NAME_MAP = {
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
    "Los Angeles Clippers": "LAC",
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


async def snapshot_nba_opening_lines(db, api_key: str = None, days: int = 3):
    """Fetch current-season NBA lines from The Odds API and save per-book rows."""
    import os
    from datetime import date

    if not api_key:
        api_key = os.environ.get("ODDS_API_KEY", "") or app_settings.odds_api_key
    year = date.today().year
    return await snapshot_opening_lines(_nba_config(year), db, api_key, days=days)
