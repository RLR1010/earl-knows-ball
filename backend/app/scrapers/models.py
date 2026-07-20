"""
Pydantic models for scraped sportsbook data.

These define the contract between scrapers → DB writer → database.
All scrapers return normalized model instances regardless of source.
"""

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class TeamProp(BaseModel):
    """Futures/betting props for a team: championship, playoffs, win totals."""

    sport: str  # "mlb" | "nfl" | "nba"
    season_year: int
    team_name: str  # Looked up against teams table
    bookmaker: str  # "draftkings" | "fanduel"
    championship_odds: Optional[int] = None
    make_playoffs_odds: Optional[int] = None
    miss_playoffs_odds: Optional[int] = None
    win_total: Optional[Decimal] = None
    win_total_over_odds: Optional[int] = None
    win_total_under_odds: Optional[int] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class PlayerSeasonProp(BaseModel):
    """Award/season-long prop for a player (MVP, Cy Young, etc.)."""

    sport: str
    season_year: int
    player_name: str
    team_name: Optional[str] = None
    prop_type: str  # e.g. "mvp_al", "cy_young_nl", "rookie_nl"
    bookmaker: str
    odds: int  # American odds
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


class PlayerDailyProp(BaseModel):
    """Game-level player prop (strikeouts, hits, points, etc.).

    Supports both standard O/U (two rows: one over, one under) and
    tiered threshold props (one row per threshold with direction='tiered').
    """

    sport: str
    game_id: str
    player_name: str
    team_name: Optional[str] = None
    prop_type: str  # e.g. "strikeouts", "hits", "home_runs"
    bookmaker: str
    line: Decimal           # threshold (0.5 for O/U, 2 for 2+ Hits)
    odds: int               # American odds
    direction: str = "tiered"  # 'over' | 'under' | 'tiered'
    scraped_at: datetime = Field(default_factory=datetime.utcnow)


# Type alias for any scraped result
ScrapedResult = TeamProp | PlayerSeasonProp | PlayerDailyProp
