"""
Fetch weather forecasts for upcoming NFL games from the National Weather Service API.

NWS API is free, no API key required, and provides 7-day hourly forecasts
for any US location. International venues are skipped.

Unlike MLB, NFL doesn't need wind_direction — just temperature, wind_speed,
and weather_condition for the games table. Forecasts are saved to
nfl.weather_forecasts for posterity.

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.nfl_weather_forecast
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.nfl_weather_forecast")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


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
            logger.debug(f"NWS points API returned {resp.status_code} for {lat},{lng}")
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

    Returns: {temperature, wind_speed, short_forecast}
    """
    try:
        resp = await client.get(forecast_url, headers=NWS_HEADERS)
        if resp.status_code != 200:
            logger.debug(f"NWS hourly forecast returned {resp.status_code}")
            return None
        data = resp.json()
    except Exception as e:
        logger.error(f"Error fetching hourly forecast: {e}")
        return None

    periods = data.get("properties", {}).get("periods", [])
    if not periods:
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
        return None

    wind_speed_str = best.get("windSpeed", "0 mph")
    try:
        wind_speed = int(wind_speed_str.split()[0])
    except (ValueError, IndexError):
        wind_speed = 0

    return {
        "temperature": best.get("temperature"),
        "wind_speed": wind_speed,
        "weather_condition": best.get("shortForecast", ""),
    }


# ── DB updates ────────────────────────────────────────────────────────────

async def update_game_weather(
    game_id: int,
    temperature: int | None,
    wind_speed: int | None,
    weather_condition: str | None,
    db,
) -> bool:
    """Update nfl.games weather columns for a given game."""
    if temperature is None and wind_speed is None and weather_condition is None:
        return False

    try:
        await db.execute(
            text("""
                UPDATE nfl.games SET
                    temperature = :temp,
                    wind_speed = :ws,
                    weather_condition = :wcond
                WHERE id = :gid
                  AND status = 'SCHEDULED'
            """),
            {
                "gid": game_id,
                "temp": temperature,
                "ws": wind_speed,
                "wcond": weather_condition,
            },
        )
        return True
    except Exception as e:
        logger.error(f"Failed to update game {game_id} weather: {e}")
        return False


async def save_forecast_record(db, game_id: int, forecast_data: dict):
    """Save a record to nfl.weather_forecasts."""
    try:
        await db.execute(
            text("""
                INSERT INTO nfl.weather_forecasts
                    (game_id, forecast_observed_at, temperature, wind_speed,
                     weather_condition, source)
                VALUES
                    (:gid, NOW(), :temp, :ws, :wcond, 'nws')
            """),
            {
                "gid": game_id,
                "temp": forecast_data.get("temperature"),
                "ws": forecast_data.get("wind_speed"),
                "wcond": forecast_data.get("weather_condition"),
            },
        )
    except Exception as e:
        logger.error(f"Failed to save forecast for game {game_id}: {e}")


# ── Main logic ────────────────────────────────────────────────────────────

FORECAST_LOOKAHEAD_DAYS = 7

INDOOR_ROOF_TYPES = {"dome", "retractable", "closed"}


async def main(force_refresh: bool = False):
    """
    Fetch NWS weather forecasts for upcoming NFL games and update nfl.games.

    Args:
        force_refresh: If True, re-fetch even if games already have weather data.
                       Otherwise only fills NULL weather fields.
    """
    async with async_session() as db:
        # Use Central time for "today" / "tomorrow" boundaries
        ct_tz = ZoneInfo("America/Chicago")
        now_ct = datetime.now(ct_tz)
        start_of_today = now_ct.replace(hour=0, minute=0, second=0, microsecond=0)
        lookahead = start_of_today + timedelta(days=FORECAST_LOOKAHEAD_DAYS)

        now_utc = start_of_today.astimezone(timezone.utc)
        lookahead_utc = lookahead.astimezone(timezone.utc)

        if force_refresh:
            weather_filter = ""
        else:
            weather_filter = "AND (g.temperature IS NULL OR g.weather_condition IS NULL)"

                # Only US venues — NWS doesn't cover international
        country_filter = "AND (v.country IS NULL OR v.country = 'USA' OR v.country = 'US')"
        result = await db.execute(
            text(f"""
                SELECT g.id, g.date, v.name, v.latitude, v.longitude, v.roof_type
                FROM nfl.games g
                JOIN nfl.venues v ON v.id = g.venue_id
                WHERE g.date >= :now_utc
                  AND g.date < :lookahead_utc
                  AND g.status = 'SCHEDULED'
                  AND v.latitude IS NOT NULL
                  AND v.longitude IS NOT NULL
                  {country_filter}
                  {weather_filter}
                ORDER BY g.date
            """),
            {"now_utc": now_utc, "lookahead_utc": lookahead_utc},
        )
        games = result.fetchall()

        if not games:
            logger.info("No upcoming NFL games need weather forecasts")
            return

        logger.info(f"Found {len(games)} NFL games needing weather forecasts")

        limits = httpx.Limits(max_keepalive_connections=5, max_connections=5)
        async with httpx.AsyncClient(limits=limits, timeout=30.0) as client:
            updated = 0
            errors = 0
            indoor_skipped = 0

            for g in games:
                game_id = g[0]
                game_time = g[1]
                venue_name = g[2]
                lat = g[3]
                lng = g[4]
                roof_type = g[5]

                # Skip fully indoor stadiums
                is_indoor = False
                if roof_type:
                    rt_lower = roof_type.lower()
                    if any(t in rt_lower for t in INDOOR_ROOF_TYPES):
                        # For dome/retractable — can still get temperature from NWS
                        # but indoor temp is less useful. Still worth getting.
                        pass

                logger.info(f"Fetching forecast for {venue_name} (game {game_id}) at {game_time.isoformat()}")

                forecast_url = await get_forecast_url(client, lat, lng)
                if not forecast_url:
                    errors += 1
                    continue

                forecast = await get_hourly_forecast(client, forecast_url, game_time, tolerance_hours=3)
                if not forecast:
                    errors += 1
                    continue

                did_update = await update_game_weather(
                    game_id,
                    forecast.get("temperature"),
                    forecast.get("wind_speed"),
                    forecast.get("weather_condition"),
                    db,
                )

                await save_forecast_record(db, game_id, forecast)

                if did_update:
                    updated += 1

            await db.commit()

        logger.info(f"Done. Updated {updated}/{len(games)} NFL games, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
