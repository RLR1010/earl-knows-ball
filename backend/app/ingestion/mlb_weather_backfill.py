"""
Backfill weather data for MLB games using the schedule endpoint.

Uses statsapi.mlb.com/api/v1/schedule with hydrate=weather, which
returns all games for a season with weather data inline (much faster
than per-game API calls).

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.mlb_weather_backfill
"""
import asyncio
import aiohttp
import logging
import re

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.mlb_weather_backfill")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"

DIR_PATTERNS = [
    ("out", re.compile(r"out", re.I)),
    ("in", re.compile(r"\bin\b", re.I)),
    ("l_to_r", re.compile(r"l\s+to\s+r", re.I)),
    ("r_to_l", re.compile(r"r\s+to\s+l", re.I)),
]


def parse_wind_direction(wind_str: str | None) -> str | None:
    if not wind_str:
        return None
    parts = wind_str.split(",", 1)
    direction_part = parts[1].strip() if len(parts) > 1 else parts[0].strip()
    if not direction_part:
        return None
    direction_lower = direction_part.lower()
    if "out" in direction_lower:
        return "out"
    if "in" in direction_lower and "out" not in direction_lower:
        return "in"
    return None


def parse_wind_speed(wind_str: str | None) -> int | None:
    if not wind_str:
        return None
    speed_part = wind_str.split(",")[0].strip()
    digits = "".join(c for c in speed_part if c.isdigit() or c == ".")
    try:
        return int(float(digits))
    except (ValueError, IndexError):
        return None


async def backfill_season(year: int) -> int:
    """Fetch weather data for all games in a season via the schedule endpoint."""
    async with aiohttp.ClientSession() as session:
        params = {
            "sportId": 1,
            "season": year,
            "gameTypes": "R",
            "hydrate": "weather",
        }
        logger.info(f"Fetching {year} schedule...")
        async with session.get(SCHEDULE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                logger.warning(f"  {year}: HTTP {resp.status}")
                return 0
            data = await resp.json()

    updates = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            game_pk = game.get("gamePk")
            weather_data = game.get("weather", {}) or {}
            temp_str = weather_data.get("temp", "")
            wind_str = weather_data.get("wind", "")
            condition = weather_data.get("condition")

            if not temp_str and not wind_str and not condition:
                continue  # No weather data for this game

            temp = int(temp_str) if temp_str and temp_str.strip().lstrip("-").isdigit() else None
            wind_speed = parse_wind_speed(wind_str)
            wind_dir = parse_wind_direction(wind_str)

            updates.append({
                "pk": game_pk,
                "temp": temp,
                "wind": wind_speed,
                "cond": condition,
                "wdir": wind_dir,
            })

    if not updates:
        logger.info(f"  {year}: no weather data found")
        return 0

    # Batch update in DB
    async with async_session() as db:
        updated = 0
        for u in updates:
            r = await db.execute(
                text("""UPDATE mlb.games SET
                    temperature = COALESCE(:temp, temperature),
                    wind_speed = COALESCE(:wind, wind_speed),
                    weather_condition = COALESCE(:cond, weather_condition),
                    wind_direction = COALESCE(:wdir, wind_direction)
                    WHERE mlb_game_id = :pk
                      AND (temperature IS NULL OR wind_speed IS NULL)"""),
                u,
            )
            if r.rowcount > 0:
                updated += 1
        await db.commit()
        logger.info(f"  {year}: updated {updated}/{len(updates)} games")
        return updated


async def main():
    total = 0
    # Backfill 2021-2026 (our training/test window)
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        n = await backfill_season(year)
        total += n
        await asyncio.sleep(0.5)
    logger.info(f"Done. Total updated: {total}")


if __name__ == "__main__":
    asyncio.run(main())
