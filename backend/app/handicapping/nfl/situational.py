"""
Situational handicap factors — rest, travel, division, venue.

Each game gets a SituationalContext that scores the non-statistical
edges that pro handicappers bake into their models.
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Game, Team, Season

logger = logging.getLogger("earl.situational")

# ── Travel helpers ─────────────────────────────────────────────────────

# Timezone offsets (hours from UTC) for each team's home stadium
TEAM_TIMEZONES = {
    "ARI": -7, "ATL": -5, "BAL": -5, "BUF": -5,
    "CAR": -5, "CHI": -6, "CIN": -5, "CLE": -5,
    "DAL": -6, "DEN": -7, "DET": -5, "GB": -6,
    "HOU": -6, "IND": -5, "JAX": -5, "KC": -6,
    "LAC": -8, "LAR": -8, "LV": -8, "MIA": -5,
    "MIN": -6, "NE": -5, "NO": -6, "NYG": -5,
    "NYJ": -5, "PHI": -5, "PIT": -5, "SEA": -8,
    "SF": -8, "TB": -5, "TEN": -6, "WAS": -5,
}

# Approximate latitude/longitude for travel distance
TEAM_COORDS = {
    "ARI": (33.5, -112.1), "ATL": (33.8, -84.4), "BAL": (39.3, -76.6), "BUF": (42.8, -78.9),
    "CAR": (35.2, -80.9), "CHI": (41.9, -87.6), "CIN": (39.1, -84.5), "CLE": (41.5, -81.7),
    "DAL": (32.8, -96.8), "DEN": (39.7, -105.0), "DET": (42.3, -83.0), "GB": (44.5, -88.0),
    "HOU": (29.7, -95.4), "IND": (39.8, -86.2), "JAX": (30.3, -81.7), "KC": (39.1, -94.5),
    "LAC": (32.8, -117.1), "LAR": (33.9, -118.3), "LV": (36.1, -115.2), "MIA": (25.8, -80.2),
    "MIN": (44.9, -93.2), "NE": (42.1, -71.3), "NO": (29.9, -90.1), "NYG": (40.8, -74.1),
    "NYJ": (40.8, -74.1), "PHI": (39.9, -75.2), "PIT": (40.4, -80.0), "SEA": (47.6, -122.3),
    "SF": (37.4, -121.9), "TB": (27.9, -82.5), "TEN": (36.2, -86.8), "WAS": (38.9, -76.9),
}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate great-circle distance between two points in miles."""
    import math
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# ── Situational Context ────────────────────────────────────────────────

class GameSituation:
    """Situational factors for a single game."""

    def __init__(self, game_id: int):
        self.game_id = game_id
        # Rest
        self.home_rest_days: Optional[float] = None
        self.away_rest_days: Optional[float] = None
        self.rest_differential: Optional[float] = None  # positive = home better rested
        # Travel
        self.travel_distance_miles: Optional[float] = None
        self.tz_diff_hours: Optional[int] = None  # positive = away team crossing time zones eastward
        self.travel_advantage: Optional[str] = None  # "home" or "away"
        # Context
        self.is_division: bool = False
        self.is_conference: bool = False
        self.is_short_week: bool = False  # game on Thu/Fri/Sat
        self.home_off_bye: bool = False
        self.away_off_bye: bool = False
        self.is_dome: Optional[bool] = None
        self.roof_type: Optional[str] = None
        self.surface: Optional[str] = None
        # Summary
        self.situation_score: Optional[float] = None  # weighted composite

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home_rest_days": self.home_rest_days,
            "away_rest_days": self.away_rest_days,
            "rest_differential": self.rest_differential,
            "travel_distance_miles": self.travel_distance_miles,
            "tz_diff_hours": self.tz_diff_hours,
            "travel_advantage": self.travel_advantage,
            "is_division": self.is_division,
            "is_conference": self.is_conference,
            "is_short_week": self.is_short_week,
            "home_off_bye": self.home_off_bye,
            "away_off_bye": self.away_off_bye,
            "is_dome": self.is_dome,
            "roof_type": self.roof_type,
            "surface": self.surface,
            "situation_score": self.situation_score,
        }


class SituationalAnalyzer:
    """Builds situational context for one or more games."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[GameSituation]:
        """Analyze a single game."""
        r = await self.db.execute(select(Game).where(Game.id == game_id))
        game = r.scalar_one_or_none()
        if not game:
            return None

        hr = await self.db.execute(select(Team).where(Team.id == game.home_team_id))
        home_team = hr.scalar_one()
        ar = await self.db.execute(select(Team).where(Team.id == game.away_team_id))
        away_team = ar.scalar_one()

        return await self._analyze(game, home_team, away_team)

    async def analyze_week(self, year: int, week: int, season_id: Optional[int] = None) -> list[GameSituation]:
        """Analyze all games in a week."""
        if not season_id:
            r = await self.db.execute(select(Season).where(Season.year == year))
            season = r.scalar_one_or_none()
            if not season:
                return []
            season_id = season.id

        r = await self.db.execute(
            select(Game)
            .where(Game.season_id == season_id, Game.week == week)
            .order_by(Game.date)
        )
        games = r.scalars().all()
        results = []
        for game in games:
            hr = await self.db.execute(select(Team).where(Team.id == game.home_team_id))
            home_team = hr.scalar_one()
            ar = await self.db.execute(select(Team).where(Team.id == game.away_team_id))
            away_team = ar.scalar_one()
            situation = await self._analyze(game, home_team, away_team)
            if situation:
                results.append(situation)
        return results

    async def _get_team_abbrev(self, team_id: int) -> str:
        r = await self.db.execute(select(Team).where(Team.id == team_id))
        t = r.scalar_one()
        return t.abbreviation

    async def _analyze(self, game: Game, home_team: Team, away_team: Team) -> GameSituation:
        ctx = GameSituation(game.id)
        ctx.roof_type = game.roof_type
        ctx.surface = game.surface
        ctx.is_dome = game.roof_type == "dome"

        home_abbr = home_team.abbreviation
        away_abbr = away_team.abbreviation

        # Division / conference
        ctx.is_division = home_team.division == away_team.division
        ctx.is_conference = home_team.conference == away_team.conference

        # Rest days
        ctx.home_rest_days = await self._rest_days(game.home_team_id, game.date, game.season_id)
        ctx.away_rest_days = await self._rest_days(game.away_team_id, game.date, game.season_id)
        if ctx.home_rest_days is not None and ctx.away_rest_days is not None:
            ctx.rest_differential = round(ctx.home_rest_days - ctx.away_rest_days, 1)

        # Short week
        if game.date:
            dow = game.date.weekday()  # 0=Mon ... 6=Sun
            ctx.is_short_week = dow in (3, 4, 5)  # Thu=3, Fri=4, Sat=5

        # Bye week detection (rest >= 13 days)
        if ctx.home_rest_days and ctx.home_rest_days >= 13:
            ctx.home_off_bye = True
        if ctx.away_rest_days and ctx.away_rest_days >= 13:
            ctx.away_off_bye = True

        # Travel distance
        ctx.travel_distance_miles = round(
            _haversine_miles(*TEAM_COORDS.get(away_abbr, (0, 0)), *TEAM_COORDS.get(home_abbr, (0, 0)))
        )
        if ctx.travel_distance_miles < 50:
            ctx.travel_distance_miles = 0  # same city teams

        # Time zone difference
        home_tz = TEAM_TIMEZONES.get(home_abbr, -5)
        away_tz = TEAM_TIMEZONES.get(away_abbr, -5)
        ctx.tz_diff_hours = home_tz - away_tz

        # Travel advantage
        if ctx.travel_distance_miles > 200:
            if ctx.tz_diff_hours >= 3:
                ctx.travel_advantage = "home"  # west coast team traveling east
            elif ctx.tz_diff_hours <= -3:
                ctx.travel_advantage = "home"  # east coast team traveling west (less bad)
            else:
                ctx.travel_advantage = "slight_home"
        else:
            ctx.travel_advantage = "neutral"

        # Composite situation score (positive = home advantage)
        score = 0.0
        if ctx.rest_differential:
            score += ctx.rest_differential * 0.5  # ~0.5 pts per rest day advantage
        if ctx.home_off_bye:
            score += 2.5  # ~2.5 pts for coming off bye
        if ctx.away_off_bye:
            score -= 2.5
        if ctx.is_short_week and ctx.home_rest_days and ctx.away_rest_days:
            if ctx.home_rest_days < 5:
                score -= 1.0  # home on short week too
            else:
                score += 1.0  # home well-rested vs short-week away
        if ctx.is_division:
            score += 0.5  # division games are more competitive at home
        if ctx.tz_diff_hours and abs(ctx.tz_diff_hours) >= 3:
            score += 0.5  # home team benefits from away team travel fatigue
        if ctx.travel_distance_miles and ctx.travel_distance_miles > 1500:
            score += 0.5

        ctx.situation_score = round(score, 2)
        return ctx

    async def _rest_days(self, team_id: int, game_date: datetime, season_id: int) -> Optional[float]:
        """Calculate days since this team's previous game."""
        if not game_date:
            return None
        r = await self.db.execute(
            select(Game.date)
            .where(
                (Game.home_team_id == team_id) | (Game.away_team_id == team_id),
                Game.season_id == season_id,
                Game.date < game_date,
                Game.status == "FINAL",
            )
            .order_by(Game.date.desc())
            .limit(1)
        )
        last_date = r.scalar_one_or_none()
        if last_date:
            delta = game_date - last_date
            return delta.total_seconds() / 86400  # days
        # If no previous game this season, give default rest (12 days = new season)
        return 12.0
