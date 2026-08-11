"""
Historical weather backfill for past NFL games.

The normal forecast path (nfl_weather_forecast.py) only writes weather for
SCHEDULED games, so FINAL (past) games never got temperature/wind_speed, and
nothing writes games.surface at all. This script backfills both from free,
keyless sources:

  * temperature / wind_speed -> Open-Meteo Archive API.
      (https://archive-api.open-meteo.com/v1/archive) — provides genuine
      HISTORICAL hourly observations by lat/lng, unlike NWS which only keeps a
      short window. No API key required.
  * surface                    -> nfl.venues.surface_type (static venue prop).

Usage:
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/nfl_weather_backfill.py
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/nfl_weather_backfill.py --surface-only
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/nfl_weather_backfill.py --limit 50 --caught-up-from 2024
"""
import argparse
import asyncio
import logging
from datetime import datetime, timezone, timedelta

import httpx

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.nfl_weather_backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Open-Meteo is free without a key; keep a polite rate (NWS ~3/sec, Open-Meteo ok @ 1/sec).
OPENMETEO_BASE = "https://archive-api.open-meteo.com/v1/archive"
POLITE_DELAY_S = 0.35          # ~3 req/sec ceiling
HTTP_TIMEOUT = 30


async def backfill_surfaces(db) -> int:
    """Copy static playing surface from venues into nfl.games.surface."""
    res = await db.execute(text("""
        UPDATE nfl.games g
        SET surface = v.surface_type
        FROM nfl.venues v
        WHERE v.id = g.venue_id
          AND g.surface IS NULL
          AND v.surface_type IS NOT NULL
    """))
    return res.rowcount


async def fetch_open_meteo(client: httpx.AsyncClient, lat: float, lng: float,
                           game_date: str, tz_name: str, game_time_utc: datetime) -> dict | None:
    """Fetch hourly archive weather for a venue+date, return nearest-to-kick reading.

    Returns {temperature_f, wind_mph, time} or None on failure.
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    url = (
        f"{OPENMETEO_BASE}?latitude={lat}&longitude={lng}"
        f"&start_date={game_date}&end_date={game_date}"
        f"&hourly=temperature_2m,wind_speed_10m,weather_code"
        f"&timezone={tz_name}"
    )
    try:
        resp = await client.get(url, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            logger.debug(f"Open-Meteo {resp.status_code} for {lat},{lng} on {game_date}")
            return None
        data = resp.json()
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        winds = hourly.get("wind_speed_10m") or []
        if not times:
            return None

        # Find the hourly slot closest to kickoff (handles pre/post-midnight).
        target = game_time_utc.astimezone(tz)
        best_i = 0
        best_delta = float("inf")
        for i, ts in enumerate(times):
            try:
                # Open-Meteo returns LOCAL wall-clock time for the requested
                # timezone (naive). Attach the venue tz so it's comparable with
                # the (aware) kickoff time.
                slot = datetime.fromisoformat(ts).replace(tzinfo=tz)
            except Exception:
                continue
            d = abs((slot - target).total_seconds())
            if d < best_delta:
                best_delta, best_i = d, i

        temp_c = temps[best_i] if best_i < len(temps) else None
        wind_kmh = winds[best_i] if best_i < len(winds) else None

        temp_f = (temp_c * 9 / 5 + 32) if temp_c is not None else None
        wind_mph = (wind_kmh * 0.621371) if wind_kmh is not None else None
        return {
            "temperature": round(temp_f) if temp_f is not None else None,
            "wind_speed": round(wind_mph, 1) if wind_mph is not None else None,
            "time": times[best_i],
        }
    except Exception as e:
        logger.warning(f"Open-Meteo error for {lat},{lng}: {e}")
        return None


async def backfill_weather(db, caught_up_from: int | None = None, limit: int | None = None) -> int:
    """Fetch historical weather for FINAL games lacking temperature."""
    q = """
        SELECT g.id, g.date, v.latitude, v.longitude, s.tz_name
        FROM nfl.games g
        JOIN nfl.venues v ON v.id = g.venue_id
        LEFT JOIN (SELECT id, 'America/New_York' AS tz_name FROM nfl.seasons) s
          ON s.id = g.season_id
        WHERE g.status = 'FINAL'
          AND g.game_type = 'REG'
          AND v.latitude IS NOT NULL AND v.longitude IS NOT NULL
          AND (g.temperature IS NULL OR g.wind_speed IS NULL)
    """
    params = {}
    if caught_up_from is not None:
        q += " AND EXTRACT(YEAR FROM g.date) >= :cup"
        params["cup"] = caught_up_from
    q += " ORDER BY g.date DESC"
    if limit is not None:
        q += " LIMIT :lim"
        params["lim"] = limit

    games = (await db.execute(text(q), params)).fetchall()
    if not games:
        logger.info("No FINAL games need weather backfill")
        return 0

    logger.info(f"Backfilling weather for {len(games)} FINAL games")
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/New_York")  # all NFL games fall within ET..PT; ET keeps the date sane
    tz_name = "America/New_York"
    limit_hits = httpx.Limits(max_connections=3)
    updated = 0
    async with httpx.AsyncClient(limits=limit_hits, timeout=HTTP_TIMEOUT) as client:
        for i, g in enumerate(games):
            game_id, game_time_utc, lat, lng, *_ = g
            if game_time_utc.tzinfo is None:
                game_time_utc = game_time_utc.replace(tzinfo=timezone.utc)
            game_date = game_time_utc.astimezone(tz).date().isoformat()

            fc = await fetch_open_meteo(client, lat, lng, game_date, tz_name, game_time_utc)
            if fc and (fc["temperature"] is not None or fc["wind_speed"] is not None):
                await db.execute(text("""
                    UPDATE nfl.games
                    SET temperature = :temp, wind_speed = :ws,
                        weather_condition = :wcond
                    WHERE id = :gid
                """), {
                    "gid": game_id, "temp": fc["temperature"],
                    "ws": fc["wind_speed"],
                    "wcond": _code_label(None),
                })
                updated += 1
                logger.info(f"  [{i+1}/{len(games)}] game {game_id} -> {fc['temperature']}F, {fc['wind_speed']}mph")
            else:
                logger.debug(f"  [{i+1}/{len(games)}] game {game_id} no data")

            if (i + 1) % 50 == 0:
                await db.commit()

            await asyncio.sleep(POLITE_DELAY_S)

    await db.commit()
    return updated


def _code_label(code):
    # keep simple; conserve API field usage
    return "Historical"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--surface-only", action="store_true", help="Only backfill games.surface from venues")
    ap.add_argument("--limit", type=int, default=None, help="Max games to backfill (weather)")
    ap.add_argument("--caught-up-from", type=int, default=None,
                    help="Only games from this year onward (e.g. 2024)")
    args = ap.parse_args()

    async with async_session() as db:
        surf = await backfill_surfaces(db)
        await db.commit()
        logger.info(f"Surface backfill complete: {surf} games updated")

        if not args.surface_only:
            n = await backfill_weather(db, caught_up_from=args.caught_up_from, limit=args.limit)
            logger.info(f"Weather backfill complete: {n} games updated")


if __name__ == "__main__":
    asyncio.run(main())
