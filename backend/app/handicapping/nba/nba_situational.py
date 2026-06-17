"""
NBA situational handicap factors — rest, travel, altitude, back-to-backs.

NBA-specific factors:
  - Rest days (back-to-backs are a huge factor in NBA)
  - Travel distance and timezone changes
  - Altitude (Denver home court advantage)
  - Division games (familiarity, rivalry)
  - Conference games
  - Day of week / national TV games
  - Schedule congestion (3 games in 4 nights, 4 in 5, etc.)
  - All-star break / long road trips
"""
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nba import NBAGame, NBATeam, NBASeason

logger = logging.getLogger("earl.nba_situational")

# Timezone offsets (hours from UTC) for NBA cities
NBA_TIMEZONES = {
    "ATL": -5, "BKN": -5, "BOS": -5, "CHA": -5, "CHI": -6,
    "CLE": -5, "DAL": -6, "DEN": -7, "DET": -5, "GSW": -8,
    "HOU": -6, "IND": -5, "LAC": -8, "LAL": -8, "MEM": -6,
    "MIA": -5, "MIL": -6, "MIN": -6, "NJ": -5, "NOP": -6,
    "NYK": -5, "OKC": -6, "ORL": -5, "PHI": -5, "PHX": -7,
    "POR": -8, "SAC": -8, "SAS": -6, "SEA": -8, "TOR": -5,
    "UTA": -7, "WAS": -5,
}

# Approximate coordinates for travel distance
NBA_COORDS = {
    "ATL": (33.8, -84.4), "BKN": (40.7, -73.9), "BOS": (42.4, -71.1),
    "CHA": (35.2, -80.8), "CHI": (41.9, -87.6), "CLE": (41.5, -81.7),
    "DAL": (32.8, -96.8), "DEN": (39.7, -104.9), "DET": (42.3, -83.0),
    "GSW": (37.8, -122.4), "HOU": (29.8, -95.4), "IND": (39.8, -86.2),
    "LAC": (34.0, -118.3), "LAL": (34.0, -118.3), "MEM": (35.1, -90.0),
    "MIA": (25.8, -80.2), "MIL": (43.0, -87.9), "MIN": (45.0, -93.3),
    "NJ": (40.7, -73.9), "NOP": (29.9, -90.1), "NYK": (40.8, -73.9),
    "OKC": (35.5, -97.5), "ORL": (28.5, -81.4), "PHI": (39.9, -75.2),
    "PHX": (33.4, -112.1), "POR": (45.5, -122.7), "SAC": (38.6, -121.5),
    "SAS": (29.4, -98.5), "SEA": (47.6, -122.3), "TOR": (43.6, -79.4),
    "UTA": (40.8, -112.0), "WAS": (38.9, -77.0),
}

# Altitude in feet for altitude adjustment (Denver = 5280, Utah = ~4200)
NBA_ALTITUDE = {
    "ATL": 1050, "BKN": 10, "BOS": 141, "CHA": 748, "CHI": 579,
    "CLE": 653, "DAL": 430, "DEN": 5280, "DET": 585, "GSW": 52,
    "HOU": 43, "IND": 718, "LAC": 262, "LAL": 262, "MEM": 259,
    "MIA": 6, "MIL": 617, "MIN": 815, "NJ": 10, "NOP": 7,
    "NYK": 10, "OKC": 1195, "ORL": 82, "PHI": 28, "PHX": 1086,
    "POR": 50, "SAC": 23, "SAS": 636, "SEA": 141, "TOR": 249,
    "UTA": 4220, "WAS": 10,
}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class NBABetContext:
    """NBA situational factors for a single game."""

    def __init__(self, game: NBAGame, home_abbr: str, away_abbr: str):
        self.game_id = game.id
        self.home_team = home_abbr
        self.away_team = away_abbr
        self.home_rest_days = 0
        self.away_rest_days = 0
        self.rest_advantage = 0  # positive = home better rested
        self.home_is_back_to_back = False   # home played yesterday
        self.away_is_back_to_back = False   # away played yesterday
        self.travel_miles = 0
        self.timezone_diff = 0
        self.is_division_game = False
        self.is_conference_game = False
        self.home_altitude = 0
        self.away_altitude = 0
        self.altitude_diff = 0              # positive = home at higher altitude
        self.home_games_in_days = 1         # games home team plays in last 4 days
        self.away_games_in_days = 1
        self.home_games_in_7 = 1            # games home team plays in last 7 days
        self.away_games_in_7 = 1
        self.is_all_star_break = False
        self.season_phase = "regular"       # early, mid, late, playoff
        self.home_road_trip_end = False     # home team finishing a road trip
        self.away_road_trip_end = False     # away team finishing a road trip
        self.home_road_trip_games = 0       # consecutive road games for home team (usually 0)
        self.away_road_trip_games = 0       # consecutive road games for away team

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_rest_days": self.home_rest_days,
            "away_rest_days": self.away_rest_days,
            "rest_advantage": self.rest_advantage,
            "home_is_back_to_back": self.home_is_back_to_back,
            "away_is_back_to_back": self.away_is_back_to_back,
            "travel_miles": self.travel_miles,
            "timezone_diff": self.timezone_diff,
            "is_division_game": self.is_division_game,
            "is_conference_game": self.is_conference_game,
            "altitude_diff": self.altitude_diff,
            "home_games_in_days": self.home_games_in_days,
            "away_games_in_days": self.away_games_in_days,
            "home_games_in_7": self.home_games_in_7,
            "away_games_in_7": self.away_games_in_7,
            "is_all_star_break": self.is_all_star_break,
            "season_phase": self.season_phase,
            "away_road_trip_games": self.away_road_trip_games,
        }


class NBASituationalAnalyzer:
    """Analyzes situational factors for NBA games."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[NBABetContext]:
        """Analyze situational factors for a single NBA game."""
        r = await self.db.execute(
            select(NBAGame).where(NBAGame.id == game_id)
        )
        game = r.scalar_one_or_none()
        if not game:
            return None

        # Get team abbreviations
        hr = await self.db.execute(select(NBATeam).where(NBATeam.id == game.home_team_id))
        ar = await self.db.execute(select(NBATeam).where(NBATeam.id == game.away_team_id))
        home_team_obj = hr.scalar_one_or_none()
        away_team_obj = ar.scalar_one_or_none()
        if not home_team_obj or not away_team_obj:
            return None

        home_abbr = home_team_obj.abbreviation
        away_abbr = away_team_obj.abbreviation

        ctx = NBABetContext(game, home_abbr, away_abbr)

        # Rest days and back-to-back
        ctx.home_rest_days = await self._rest_days_since(game, game.home_team_id)
        ctx.away_rest_days = await self._rest_days_since(game, game.away_team_id)
        ctx.rest_advantage = ctx.home_rest_days - ctx.away_rest_days
        ctx.home_is_back_to_back = ctx.home_rest_days == 0
        ctx.away_is_back_to_back = ctx.away_rest_days == 0

        # Schedule congestion: games in last 4 days
        ctx.home_games_in_days = 1 + await self._games_since(game, game.home_team_id, days_back=4)
        ctx.away_games_in_days = 1 + await self._games_since(game, game.away_team_id, days_back=4)
        ctx.home_games_in_7 = 1 + await self._games_since(game, game.home_team_id, days_back=7)
        ctx.away_games_in_7 = 1 + await self._games_since(game, game.away_team_id, days_back=7)

        # Road trip length
        ctx.away_road_trip_games = await self._consecutive_road_games(game, game.away_team_id)
        ctx.home_road_trip_games = await self._consecutive_road_games(game, game.home_team_id)

        # Travel distance
        h_coords = NBA_COORDS.get(home_abbr)
        a_coords = NBA_COORDS.get(away_abbr)
        if h_coords and a_coords:
            ctx.travel_miles = round(_haversine_miles(a_coords[0], a_coords[1], h_coords[0], h_coords[1]))

        # Timezone difference
        h_tz = NBA_TIMEZONES.get(home_abbr, -5)
        a_tz = NBA_TIMEZONES.get(away_abbr, -5)
        ctx.timezone_diff = h_tz - a_tz

        # Division game
        if home_team_obj.division and away_team_obj.division:
            ctx.is_division_game = home_team_obj.division == away_team_obj.division

        # Conference game
        if home_team_obj.conference and away_team_obj.conference:
            ctx.is_conference_game = home_team_obj.conference == away_team_obj.conference

        # Altitude
        ctx.home_altitude = NBA_ALTITUDE.get(home_abbr, 0)
        ctx.away_altitude = NBA_ALTITUDE.get(away_abbr, 0)
        ctx.altitude_diff = ctx.home_altitude - ctx.away_altitude

        # Season phase
        if game.date:
            month = game.date.month
            day = game.date.day
            if month <= 1:
                ctx.season_phase = "early"
            elif month <= 3:
                ctx.season_phase = "mid"
            elif month <= 4:
                ctx.season_phase = "late"
            else:
                ctx.season_phase = "playoff"
                # Check if playoff game
                if game.game_type not in ("P", "PO", "PS"):
                    ctx.season_phase = "regular" if month >= 10 else "early"

            # All-star break: roughly mid-February
            if month == 2 and 15 <= day <= 22:
                ctx.is_all_star_break = True

        return ctx

    async def analyze_date(self, game_date: str) -> list[NBABetContext]:
        """Analyze situations for all games on a given date."""
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dstart = dt
        dend = dt + timedelta(days=1)
        r = await self.db.execute(
            select(NBAGame).where(
                NBAGame.date >= dstart,
                NBAGame.date < dend,
            ).order_by(NBAGame.date)
        )
        games = r.scalars().all()
        results = []
        for game in games:
            ctx = await self.analyze_game(game.id)
            if ctx:
                results.append(ctx)
        return results

    async def _rest_days_since(self, game: NBAGame, team_id: int) -> int:
        """Count days since this team's last game."""
        if not game.date:
            return 1
        r = await self.db.execute(
            select(NBAGame.date).where(
                NBAGame.date < game.date,
                ((NBAGame.home_team_id == team_id) | (NBAGame.away_team_id == team_id)),
            ).order_by(NBAGame.date.desc()).limit(1)
        )
        last_date = r.scalar_one_or_none()
        if not last_date:
            return 3  # Season opener or first game available
        diff = (game.date - last_date).days
        return max(1, diff)

    async def _games_since(self, game: NBAGame, team_id: int, days_back: int) -> int:
        """Count games this team has played in the last N days (excluding this game)."""
        if not game.date:
            return 0
        cutoff = game.date - timedelta(days=days_back)
        r = await self.db.execute(
            select(NBAGame).where(
                NBAGame.date >= cutoff,
                NBAGame.date < game.date,
                ((NBAGame.home_team_id == team_id) | (NBAGame.away_team_id == team_id)),
                NBAGame.home_score.isnot(None),
                NBAGame.away_score.isnot(None),
            )
        )
        return len(r.scalars().all())

    async def _consecutive_road_games(self, game: NBAGame, team_id: int) -> int:
        """Count consecutive road games this team has played going into this game."""
        if not game.date:
            return 0
        count = 0
        # Look backwards from this game
        r = await self.db.execute(
            select(NBAGame).where(
                NBAGame.date < game.date,
                NBAGame.away_team_id == team_id,
            ).order_by(NBAGame.date.desc()).limit(5)
        )
        road_games = r.scalars().all()
        for rg in road_games:
            if rg.date and rg.date >= game.date - timedelta(days=10):
                count += 1
            else:
                break
        return count
