"""
Per-sport configuration for the scrapers.

Defines what to scrape for each sport: which book pages to visit,
what season year to use, team name mappings, etc.
"""

from dataclasses import dataclass, field

from app.scrapers.models import TeamProp, PlayerSeasonProp, PlayerDailyProp


@dataclass
class SportScrapeConfig:
    """Configuration for a single sport's scrape targets."""

    name: str                        # "mlb" | "nfl" | "nba"
    season_year: int                 # e.g. 2026
    scrape_team_props: bool = True
    scrape_awards: bool = True
    scrape_player_props: bool = True

    # Category of team props to scrape (used by books to find right page)
    futures_category: str = "futures"
    awards_category: str = "awards"
    player_props_category: str = "player-props"


# Sport configurations
# Season year is the "label" year (MLB 2026 = 2026 season)
CONFIGS = {
    "mlb": SportScrapeConfig(
        name="mlb",
        season_year=2026,
        scrape_team_props=True,
        scrape_awards=True,
        scrape_player_props=True,
    ),
    "nfl": SportScrapeConfig(
        name="nfl",
        season_year=2026,
        scrape_team_props=True,
        scrape_awards=True,
        scrape_player_props=False,   # No NFL games until preseason
    ),
    "nba": SportScrapeConfig(
        name="nba",
        season_year=2026,
        scrape_team_props=True,
        scrape_awards=True,
        scrape_player_props=False,   # Offseason
    ),
}


def get_active_configs() -> list[SportScrapeConfig]:
    """Get scrape configs for all configured sports."""
    return list(CONFIGS.values())
