"""
Fetch weather forecasts for upcoming MLB games from the National Weather Service API.

NWS API is free, no API key required, and provides 7-day hourly forecasts
for any US location. It covers all current MLB venues.

The MLB API only provides weather for games within ~1 day of game time.
This module fills the gap by getting forecast data for games further out.

Wind direction calculation:
- NWS gives wind_direction in cardinal degrees (0-360, where 0=N, 90=E)
- We compare wind direction against the venue's home_plate_orientation to
  determine the effect: in (blowing toward plate), out (blowing toward outfield),
  l_to_r (blowing from left field to right), r_to_l (right to left)

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.mlb_weather_forecast
"""
import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.mlb_weather_forecast")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


# ── Cardinal helpers ──────────────────────────────────────────────────────

CARDINAL_DEGREES = {
    "N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
    "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
    "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
    "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5,
}

DEGREES_TO_CARDINAL = {v: k for k, v in CARDINAL_DEGREES.items()}


def _normalize_angle(deg: float) -> float:
    """Normalize an angle to [0, 360)."""
    return deg % 360


def degrees_to_cardinal(deg: float | None) -> str | None:
    """Convert wind direction degrees to cardinal direction string."""
    if deg is None:
        return None
    deg = _normalize_angle(deg)
    closest = min(DEGREES_TO_CARDINAL.keys(), key=lambda x: min(abs(x - deg), 360 - abs(x - deg)))
    return DEGREES_TO_CARDINAL[closest]


def cardinal_to_degrees(cardinal: str | None) -> float | None:
    """Convert cardinal direction string to degrees."""
    if cardinal is None:
        return None
    return CARDINAL_DEGREES.get(cardinal.upper())


def calculate_wind_effect(
    wind_from_degrees: float | None,
    home_plate_orientation: str | None,
) -> str | None:
    """
    Calculate the wind effect relative to home plate (in/out/l_to_r/r_to_l).

    NWS wind direction = direction wind is COMING FROM (meteorological convention).
    So wind from 0 (N) means wind blows from north to south.

    home_plate_orientation = direction home plate FACES (toward center field).

    Logic: We compute where the wind is blowing TO (opposite of meteorological),
    then compare that vector to the home plate direction vector.
    """
    if wind_from_degrees is None or home_plate_orientation is None:
        return None

    hp_deg = cardinal_to_degrees(home_plate_orientation)
    if hp_deg is None:
        return None

    wind_from = _normalize_angle(wind_from_degrees)
    hp = _normalize_angle(hp_deg)

    # Direction wind is blowing TOWARD (opposite of meteorological "from")
    wind_to = _normalize_angle(wind_from + 180)

    # How far off is wind_to from hp?
    # Use the smaller angle
    angle_diff = _normalize_angle(abs(wind_to - hp))
    if angle_diff > 180:
        angle_diff = 360 - angle_diff

    # Thresholds
    # < 45° off → roughly same direction as HP faces → outfield → "out"
    # > 135° off → roughly opposite direction → toward plate → "in"
    # In between → perpendicular → l_to_r or r_to_l
    threshold = 45

    if angle_diff <= threshold:
        return "out"
    elif angle_diff >= 180 - threshold:
        return "in"
    else:
        # Perpendicular: determine which side the wind is coming FROM
        # Standing at plate facing center field:
        #   wind coming from right side → "r_to_l"
        #   wind coming from left side  → "l_to_r"
        #
        # Cross product sign: (wind_to × hp) using differences in the
        # normalized circle determines left vs right
        if _normalize_angle(wind_to - hp) <= 180:
            # wind_to is counter-clockwise from hp
            # (wind hitting from the right field side)
            return "r_to_l"
        else:
            return "l_to_r"


# ── NWS API helpers ───────────────────────────────────────────────────────

NWS_HEADERS = {
    "User-Agent": "(earl-knows-ball, rich@earl.com)",
    "Accept": "application/geo+json",
}


async def get_forecast_url(client: httpx.AsyncClient, lat: float, lng: float) -> str | None:
    """Get the NWS forecast hourly URL for a lat/lng point."""
    url = f"https://api.weather.gov/points/{lat:.4f},{lng:.4f}"
    try:
        resp = await client.get(url, headers=NWS_HEADERS)
        if resp.status_code != 200:
            logger.warning(f"NWS points API returned {resp.status_code} for {lat},{lng}")
            return None
        data = resp.json()
        return data.get("properties", {}).get("forecastHourly")
    except Exception as e:
        logger.error(f"Error fetching NWS forecast URL for {lat},{lng}: {e}")
        return None


async def get_hourly_forecast(
    client: httpx.AsyncClient,
    forecast_url: str,
    game_time: datetime,
    tolerance_hours: int = 3,
) -> dict | None:
    """
    Fetch hourly forecast and find the period closest to game_time.
    Returns: {temperature, wind_speed, wind_direction (degrees), short_forecast}
    """
    try:
        resp = await client.get(forecast_url, headers=NWS_HEADERS)
        if resp.status_code != 200:
            logger.warning(f"NWS hourly forecast returned {resp.status_code}")
            return None
        data = resp.json()
    except Exception as e:
        logger.error(f"Error fetching hourly forecast: {e}")
        return None

    periods = data.get("properties", {}).get("periods", [])
    if not periods:
        logger.warning("No periods in NWS hourly forecast")
        return None

    best = None
    best_diff = float("inf")

    for p in periods:
        p_time_str = p.get("startTime")
        if not p_time_str:
            continue
        try:
            p_time = datetime.fromisoformat(p_time_str.replace("Z", "+00:00"))
        except ValueError:
            continue

        diff = abs((p_time - game_time).total_seconds())
        if diff < best_diff:
            best_diff = diff
            best = p

    if best is None or best_diff > tolerance_hours * 3600:
        logger.info(f"No hourly period within {tolerance_hours}h of game time")
        return None

    wind_speed_str = best.get("windSpeed", "0 mph")
    try:
        wind_speed_kts = int(wind_speed_str.split()[0])
    except (ValueError, IndexError):
        wind_speed_kts = 0

    # NWS wind direction is a cardinal string (e.g. "ENE" = 67.5°),
    # direction wind is coming FROM (meteorological convention)
    wind_dir_raw = best.get("windDirection")
    wind_deg = cardinal_to_degrees(wind_dir_raw) if wind_dir_raw else None

    short_forecast = best.get("shortForecast", "")
    temperature = best.get("temperature")

    return {
        "temperature": temperature,
        "wind_speed": int(wind_speed_kts),  # mph (NWS gives mph for US)
        "wind_direction_degrees": wind_deg,
        "weather_condition": short_forecast,
    }


# ── Feature extraction → game update ──────────────────────────────────────

async def update_game_weather(
    game_id: int,
    temperature: int | None,
    wind_speed: int | None,
    wind_effect: str | None,
    weather_condition: str | None,
    db,
) -> bool:
    """Update the weather columns in mlb.games for a given game."""
    if temperature is None and wind_speed is None and wind_effect is None and weather_condition is None:
        return False

    try:
        await db.execute(
            text("""
                UPDATE mlb.games SET
                    temperature = :temp,
                    wind_speed = :ws,
                    wind_direction = :wdir,
                    weather_condition = :wcond
                WHERE id = :gid
                  AND status = 'SCHEDULED'
            """),
            {
                "gid": game_id,
                "temp": temperature,
                "ws": wind_speed,
                "wdir": wind_effect,
                "wcond": weather_condition,
            },
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update game {game_id} weather: {e}")
        return False


async def save_forecast_record(db, game_id: int, forecast_data: dict, wind_effect: str | None):
    """Save a record to mlb.weather_forecasts."""
    try:
        wind_cardinal = degrees_to_cardinal(forecast_data.get("wind_direction_degrees"))
        await db.execute(
            text("""
                INSERT INTO mlb.weather_forecasts
                    (game_id, forecast_observed_at, temperature, wind_speed,
                     wind_direction_cardinal, weather_condition, source)
                VALUES
                    (:gid, NOW(), :temp, :ws, :wcard, :wcond, 'nws')
            """),
            {
                "gid": game_id,
                "temp": forecast_data.get("temperature"),
                "ws": forecast_data.get("wind_speed"),
                "wcard": wind_cardinal,
                "wcond": forecast_data.get("weather_condition"),
            },
        )
    except Exception as e:
        logger.error(f"Failed to save forecast for game {game_id}: {e}")


# ── Main logic ────────────────────────────────────────────────────────────

FORECAST_LOOKAHEAD_DAYS = 2

INDOOR_ROOF_TYPES = {"indoor", "dome", "closed"}


async def main(force_refresh: bool = False):
    """
    Fetch NWS weather forecasts for upcoming MLB games and update the games table.

    Args:
        force_refresh: If True, re-fetch even if games already have weather data.
                       Otherwise only fills in games with NULL weather fields.
    """
    async with async_session() as db:
        # 1. Find upcoming games that need weather data
        # Use Central time for "today" / "tomorrow" boundaries
        ct_tz = ZoneInfo("America/Chicago")
        now_ct = datetime.now(ct_tz)
        start_of_today = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
        lookahead = start_of_today + timedelta(days=FORECAST_LOOKAHEAD_DAYS)

        # Convert to UTC for DB comparison
        now_utc = start_of_today.astimezone(timezone.utc)
        lookahead_utc = lookahead.astimezone(timezone.utc)

        if force_refresh:
            weather_condition = ""
        else:
            weather_condition = "AND (g.temperature IS NULL OR g.wind_direction IS NULL)"

        result = await db.execute(
            text(f"""
                SELECT g.id, g.date, g.venue_id,
                       v.name AS venue_name,
                       v.latitude, v.longitude,
                       v.home_plate_orientation,
                       g.roof_type
                FROM mlb.games g
                JOIN mlb.venues v ON v.mlb_venue_id = g.venue_id
                WHERE g.date >= :now_utc
                  AND g.date < :lookahead_utc
                  AND g.status = 'SCHEDULED'
                  AND v.latitude IS NOT NULL
                  AND v.longitude IS NOT NULL
                  {weather_condition}
                ORDER BY g.date
            """),
            {"now_utc": now_utc, "lookahead_utc": lookahead_utc},
        )
        games = result.fetchall()

        if not games:
            logger.info("No upcoming games need weather forecasts")
            return

        logger.info(f"Found {len(games)} games needing weather forecasts")

        # 2. Fetch forecasts using NWS API
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
        async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
            updated = 0
            forecast_errors = 0
            indoor_skipped = 0

            for g in games:
                game_id = g[0]
                game_time = g[1]
                venue_id = g[2]
                venue_name = g[3]
                lat = g[4]
                lng = g[5]
                orientation = g[6]
                roof_type = g[7]

                # Skip fully indoor stadiums — no wind effect needed,
                # but still fetch temperature if missing
                is_indoor = False
                if roof_type:
                    rt_lower = roof_type.lower()
                    if any(t in rt_lower for t in INDOOR_ROOF_TYPES) and "retractable" not in rt_lower:
                        is_indoor = True

                logger.info(f"Fetching forecast for {venue_name} (game {game_id}) at {game_time.isoformat()}")

                # Get NWS forecast URL for this venue
                forecast_url = await get_forecast_url(client, lat, lng)
                if not forecast_url:
                    forecast_errors += 1
                    continue

                # Get hourly forecast at game time
                forecast = await get_hourly_forecast(client, forecast_url, game_time, tolerance_hours=3)
                if not forecast:
                    forecast_errors += 1
                    continue

                            # NWS already returns wind speed in mph for US locations
                wind_speed = forecast.get("wind_speed", 0)

                # Calculate wind effect (indoor stadiums: no wind)
                wind_effect = None if is_indoor else calculate_wind_effect(
                    forecast.get("wind_direction_degrees"), orientation
                )

                # Update games table
                did_update = await update_game_weather(
                    game_id,
                    forecast.get("temperature"),
                    wind_speed,
                    wind_effect,
                    forecast.get("weather_condition"),
                    db,
                )

                # Save forecast record
                forecast["wind_speed"] = wind_speed
                await save_forecast_record(db, game_id, forecast, wind_effect)

                if did_update:
                    updated += 1

            await db.commit()

        logger.info(
            f"Done. Updated {updated}/{len(games)} games, "
            f"{forecast_errors} forecast errors, "
            f"{indoor_skipped} indoor skipped"
        )


# ── Entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    asyncio.run(main())
