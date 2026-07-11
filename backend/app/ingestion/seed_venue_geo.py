"""
Seed latitude, longitude, and home plate orientation for known MLB venues.

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.seed_venue_geo
"""
import asyncio
import logging

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.seed_venue_geo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Known MLB venue data: (venue_name, lat, lng, home_plate_orientation)
# Orientation = direction home plate faces (toward center field), cardinal value
# Sources: ballparks.com, baseball-almanac.com, wikipedia, Google Maps analysis
VENUE_DATA = [
    # Active MLB stadiums
    ("Angel Stadium", 33.8003, -117.8827, "WSW"),
    ("Oriole Park at Camden Yards", 39.2839, -76.6217, "ENE"),
    ("Fenway Park", 42.3467, -71.0972, "ENE"),
    ("Rate Field", 41.8299, -87.6338, "ENE"),
    ("Progressive Field", 41.4959, -81.6854, "NE"),
    ("Kauffman Stadium", 39.0517, -94.4803, "WNW"),
    ("Oakland Coliseum", 37.7516, -122.2005, "WSW"),
    ("Tropicana Field", 27.7683, -82.6534, "SE"),
    ("Rogers Centre", 43.6414, -79.3894, "NNE"),
    ("Chase Field", 33.4457, -112.0668, "NNE"),
    ("Wrigley Field", 41.9484, -87.6553, "NE"),
    ("Coors Field", 39.7559, -104.9942, "NNW"),
    ("Dodger Stadium", 34.0739, -118.2400, "SSW"),
    ("PNC Park", 40.4468, -80.0058, "ENE"),
    ("American Family Field", 43.0283, -87.9712, "ENE"),
    ("T-Mobile Park", 47.5914, -122.3323, "NNE"),
    ("Daikin Park", 29.7570, -95.3554, "ENE"),
    ("Comerica Park", 42.3390, -83.0485, "NNE"),
    ("Oracle Park", 37.7786, -122.3893, "WNW"),
    ("Sutter Health Park", 38.5807, -121.5134, "WSW"),
    ("Great American Ball Park", 39.0977, -84.5070, "WNW"),
    ("Petco Park", 32.7077, -117.1571, "WNW"),
    ("Citizens Bank Park", 39.9059, -75.1665, "NE"),
    ("Busch Stadium", 38.6226, -90.1928, "WSW"),
    ("Citi Field", 40.7571, -73.8458, "ENE"),
    ("Nationals Park", 38.8730, -77.0075, "WSW"),
    ("Target Field", 44.9817, -93.2783, "E"),
    ("Yankee Stadium", 40.8296, -73.9262, "ENE"),
    ("loanDepot park", 25.7781, -80.2197, "SSE"),
    ("Truist Park", 33.8908, -84.4684, "WNW"),
    ("Globe Life Field", 32.7516, -97.0835, "WNW"),

    # Spring training / special event venues
    ("Ballpark of the Palm Beaches", 26.7437, -80.1186, "NE"),
    ("BayCare Ballpark", 27.9970, -82.7541, "ENE"),
    ("CACTI Park of the Palm Beaches", 26.7437, -80.1186, "NE"),
    ("CoolToday Park", 27.0664, -82.1792, "WSW"),
    ("Dunkin' Park", 41.7003, -72.7389, "NE"),
    ("George M. Steinbrenner Field", 27.9970, -82.7541, "ENE"),
    ("Jungle Jim's Stadium", 25.3108, -80.2744, "NE"),
    ("Las Vegas Ballpark", 36.1061, -115.1764, "NE"),
    ("Muncy Bank Ballpark", 41.2373, -77.0229, "NE"),
    ("Sahlen Field", 42.8820, -78.8782, "ENE"),
    ("TD Ballpark", 43.7190, -79.4637, "ENE"),
    ("Field of Dreams", 42.5377, -91.0888, "NE"),
    ("Tokyo Dome", 35.7056, 139.7518, "N"),
    ("Gocheok Sky Dome", 37.4982, 126.8670, "NNE"),
    ("Estadio Alfredo Harp Helú", 19.3772, -99.1759, "WNW"),
    ("Estadio de Beisbol", 25.6734, -100.6484, "NNE"),
    ("London Stadium", 51.5385, -0.0166, "W"),
    ("Rickwood Field", 33.5260, -86.8142, "WNW"),
    ("Bristol Motor Speedway", 36.5160, -82.2568, "NW"),
]


async def seed():
    async with async_session() as db:
        seeded_geo = 0
        seeded_orientation = 0
        missing = []

        # Fetch existing venues
        result = await db.execute(text("SELECT id, name FROM mlb.venues"))
        venues = result.fetchall()
        venue_map = {row[1].strip().lower(): row[0] for row in venues}

        for name, lat, lng, orientation in VENUE_DATA:
            key = name.strip().lower()
            vid = venue_map.get(key)
            if vid is None:
                missing.append(name)
                continue

            await db.execute(
                text("""
                    UPDATE mlb.venues SET
                        latitude = :lat,
                        longitude = :lng,
                        home_plate_orientation = :orientation
                    WHERE id = :vid
                """),
                {"vid": vid, "lat": lat, "lng": lng, "orientation": orientation},
            )
            seeded_geo += 1
            seeded_orientation += 1

        await db.commit()

        logger.info(f"Updated geo + orientation for {seeded_geo} venues")
        if missing:
            logger.warning(f"No DB match for {len(missing)} venues: {', '.join(missing)}")

        # Check remaining
        result = await db.execute(text("SELECT id, name FROM mlb.venues WHERE latitude IS NULL"))
        still_null = result.fetchall()
        if still_null:
            logger.warning(f"{len(still_null)} venues still missing geo data:")
            for row in still_null:
                logger.warning(f"  id={row[0]}: {row[1]}")

if __name__ == "__main__":
    asyncio.run(seed())
