"""
MLB situational handicap factors — rest, travel, bullpen, day/night, venue.

MLB-specific factors:
  - Rest days (teams play ~6 games/week, rest is a premium)
  - Day/night split (hitters see better in day games)
  - Home/away park factors
  - Division games (familiarity)
  - Bullpen usage (tired pen = late-inning vulnerability)
  - Weather (roof type, temperature)
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import MLBGames, MLBTeam, MLBSeason

logger = logging.getLogger("earl.mlb_situational")

# All time-of-day / weekday classification is done in the game's LOCAL time.
# `game.date` is a timestamptz returned by asyncpg as a UTC-aware datetime, so it
# MUST be converted to a real timezone before reading .hour; otherwise evening
# starts roll past midnight UTC and bucket wrong (the old code added a hardcoded
# standard-time offset to the UTC hour, which also ignored DST and went negative
# for West Coast night games).
_TZ_UTC = ZoneInfo("UTC")
_TZ_ET = ZoneInfo("America/New_York")

# Ballpark IANA timezones (DST-aware), keyed by home team abbreviation.
MLB_VENUE_TZ = {
    "ARI": "America/Phoenix", "ATL": "America/New_York", "BAL": "America/New_York",
    "BOS": "America/New_York", "CHC": "America/Chicago", "CIN": "America/New_York",
    "CLE": "America/New_York", "COL": "America/Denver", "CWS": "America/Chicago",
    "DET": "America/New_York", "HOU": "America/Chicago", "KC": "America/Chicago",
    "LAA": "America/Los_Angeles", "LAD": "America/Los_Angeles", "MIA": "America/New_York",
    "MIL": "America/Chicago", "MIN": "America/Chicago", "NYM": "America/New_York",
    "NYY": "America/New_York", "OAK": "America/Los_Angeles", "ATH": "America/Los_Angeles",
    "PHI": "America/New_York", "PIT": "America/New_York", "SD": "America/Los_Angeles",
    "SEA": "America/Los_Angeles", "SF": "America/Los_Angeles", "STL": "America/Chicago",
    "TB": "America/New_York", "TEX": "America/Chicago", "TOR": "America/New_York",
    "WSH": "America/New_York",
}


def _venue_local_hour(dt, home_abbr):
    """Hour of a game timestamp in the HOME ballpark's local time (None-safe)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_UTC)
    tz = ZoneInfo(MLB_VENUE_TZ.get(home_abbr, "America/New_York"))
    return dt.astimezone(tz).hour


def _venue_tz_offset_hours(dt, abbr):
    """DST-aware UTC offset (in hours) for a team's timezone at the game instant."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_UTC)
    tz = ZoneInfo(MLB_VENUE_TZ.get(abbr, "America/New_York"))
    off = dt.astimezone(tz).utcoffset()
    return off.total_seconds() / 3600.0 if off is not None else None


def _et_day(dt):
    """US Eastern calendar date of a game timestamp (None-safe)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_UTC)
    return dt.astimezone(_TZ_ET).date()

# Timezone offsets (hours from UTC) for MLB ballparks
MLB_TIMEZONES = {
    "ARI": -7, "ATL": -5, "BAL": -5, "BOS": -5,
    "CHC": -6, "CIN": -5, "CLE": -5, "COL": -7,
    "CWS": -6, "DET": -5, "HOU": -6, "KC": -6,
    "LAA": -8, "LAD": -8, "MIA": -5, "MIL": -6,
    "MIN": -6, "NYM": -5, "NYY": -5, "OAK": -8,
    "PHI": -5, "PIT": -5, "SD": -8, "SEA": -8,
    "SF": -8, "STL": -6, "TB": -5, "TEX": -6,
    "TOR": -5, "WSH": -5,
}

# Approximate coordinates for travel distance
MLB_COORDS = {
    "ARI": (33.4, -112.1), "ATL": (33.7, -84.4), "BAL": (39.3, -76.6), "BOS": (42.3, -71.1),
    "CHC": (41.9, -87.7), "CIN": (39.1, -84.5), "CLE": (41.5, -81.7), "COL": (39.8, -104.9),
    "CWS": (41.8, -87.6), "DET": (42.3, -83.0), "HOU": (29.8, -95.4), "KC": (39.1, -94.5),
    "LAA": (33.8, -117.9), "LAD": (34.1, -118.2), "MIA": (25.8, -80.2), "MIL": (43.0, -87.9),
    "MIN": (44.9, -93.2), "NYM": (40.8, -73.8), "NYY": (40.8, -73.8), "OAK": (37.8, -122.2),
    "PHI": (39.9, -75.2), "PIT": (40.4, -80.0), "SD": (32.7, -117.2), "SEA": (47.6, -122.3),
    "SF": (37.8, -122.4), "STL": (38.6, -90.2), "TB": (27.8, -82.7), "TEX": (32.8, -97.1),
    "TOR": (43.6, -79.4), "WSH": (38.9, -76.9),
}


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


class MLBBetContext:
    """MLB situational factors for a single game."""

    def __init__(self, game: MLBGames, home_abbr: str, away_abbr: str):
        self.game_id = game.id
        self.home_team = home_abbr
        self.away_team = away_abbr
        self.home_rest_days = 0
        self.away_rest_days = 0
        self.rest_advantage = 0  # positive = home better rested
        self.travel_miles = 0
        self.timezone_diff = 0
        self.is_day_game = False
        self.is_division_game = False
        self.is_dome = False  # roof_type = "Closed" or "Retractable" and closed
        self.temperature = None
        self.doubleheader = False
        self.season_phase = "regular"  # early, mid, late, playoff

    def to_dict(self) -> dict:
        return {
            "game_id": self.game_id,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "home_rest_days": self.home_rest_days,
            "away_rest_days": self.away_rest_days,
            "rest_advantage": self.rest_advantage,
            "travel_miles": self.travel_miles,
            "timezone_diff": self.timezone_diff,
            "is_day_game": self.is_day_game,
            "is_division_game": self.is_division_game,
            "is_dome": self.is_dome,
            "temperature": self.temperature,
            "doubleheader": self.doubleheader,
            "season_phase": self.season_phase,
        }


class MLBSituationalAnalyzer:
    """Analyzes situational factors for MLB games."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def analyze_game(self, game_id: int) -> Optional[MLBBetContext]:
        """Analyze situational factors for a single MLB game."""
        r = await self.db.execute(
            select(MLBGames).where(MLBGames.id == game_id)
        )
        game = r.scalar_one_or_none()
        if not game:
            return None

        # Get team abbreviations
        hr = await self.db.execute(select(MLBTeam).where(MLBTeam.id == game.home_team_id))
        ar = await self.db.execute(select(MLBTeam).where(MLBTeam.id == game.away_team_id))
        home_team_obj = hr.scalar_one_or_none()
        away_team_obj = ar.scalar_one_or_none()
        if not home_team_obj or not away_team_obj:
            return None

        home_abbr = home_team_obj.abbreviation
        away_abbr = away_team_obj.abbreviation

        ctx = MLBBetContext(game, home_abbr, away_abbr)

        # Rest days: find each team's most recent game before this one
        ctx.home_rest_days = await self._rest_days_since(game, game.home_team_id)
        ctx.away_rest_days = await self._rest_days_since(game, game.away_team_id)
        ctx.rest_advantage = ctx.home_rest_days - ctx.away_rest_days

        # Travel distance
        h_coords = MLB_COORDS.get(home_abbr)
        a_coords = MLB_COORDS.get(away_abbr)
        if h_coords and a_coords:
            ctx.travel_miles = round(_haversine_miles(a_coords[0], a_coords[1], h_coords[0], h_coords[1]))

        # Timezone difference
        # DST-aware offset difference (hardcoded standard-time offsets ignored DST
        # and Arizona, so this could be off by an hour).
        h_off = _venue_tz_offset_hours(game.date, home_abbr)
        a_off = _venue_tz_offset_hours(game.date, away_abbr)
        if h_off is not None and a_off is not None:
            ctx.timezone_diff = round(h_off - a_off)

        # Day/night game — use the authoritative scheduled-day flag ingested into
        # `game.day_night` (venue-local; SAME source the model's data_loader and
        # populate_rolling use). Only fall back to the venue-local clock if that
        # column is missing -- NEVER a UTC hour, and NOT US-Eastern time, which
        # would relabel West Coast afternoon games (1pm/4pm PT) as night games.
        dn = (game.day_night or "").strip().lower()
        if dn in ("day", "d"):
            ctx.is_day_game = True
        elif dn in ("night", "n"):
            ctx.is_day_game = False
        else:
            _hr = _venue_local_hour(game.date, home_abbr)
            if _hr is not None:
                ctx.is_day_game = _hr < 17

        # Division game
        if home_team_obj.division and away_team_obj.division:
            ctx.is_division_game = (
                home_team_obj.division == away_team_obj.division
                and home_team_obj.league == away_team_obj.league
            )

        # Dome/weather
        ctx.is_dome = game.roof_type in ("Closed", "Dome")
        ctx.temperature = game.temperature

        # Doubleheader
        ctx.doubleheader = game.game_number > 0

        # Season phase
        if game.date:
            month = game.date.month
            if month <= 4:
                ctx.season_phase = "early"
            elif month <= 7:
                ctx.season_phase = "mid"
            elif month <= 9:
                ctx.season_phase = "late"
            elif month >= 10:
                ctx.season_phase = "playoff"
                # Check if it's actually playoffs
                if game.game_type not in ("P", "PO", "PS"):
                    ctx.season_phase = "late"

        return ctx

    async def analyze_date(self, game_date: str) -> list[MLBBetContext]:
        """Analyze situations for all games on a given date."""
        from app.database import async_session
        # Query games on this date using a range
        dt = datetime.strptime(game_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dstart = dt
        dend = dt + timedelta(days=1)
        r = await self.db.execute(
            select(MLBGames).where(
                MLBGames.date >= dstart,
                MLBGames.date < dend,
            ).order_by(MLBGames.date)
        )
        games = r.scalars().all()
        results = []
        for game in games:
            ctx = await self.analyze_game(game.id)
            if ctx:
                results.append(ctx)
        return results

    async def _rest_days_since(self, game: MLBGames, team_id: int) -> int:
        """Calendar days between this game and the team's previous game (1 = back-to-back).

        Matches the site-wide convention (MLB `handicapping/mlb/data_loader.py`
        `rest_days` = ET local-date gap). Uses ET calendar dates and the same season,
        so an evening game can't be misread as back-to-back (raw UTC arithmetic
        collapsed a sub-48h gap to `.days == 1`) and a season opener doesn't pick up
        last season's finale.
        """
        if not game.date:
            return 3
        r = await self.db.execute(
            select(MLBGames.date).where(
                MLBGames.season_id == game.season_id,
                MLBGames.date < game.date,
                ((MLBGames.home_team_id == team_id) | (MLBGames.away_team_id == team_id)),
            ).order_by(MLBGames.date.desc()).limit(1)
        )
        last_date = r.scalar_one_or_none()
        if not last_date:
            return 3  # Season opener / first game available
        gap = (_et_day(game.date) - _et_day(last_date)).days
        return max(gap, 1)
