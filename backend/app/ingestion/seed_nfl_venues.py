"""
Seed nfl.venues with known NFL stadium data (lat, lon, roof_type, surface_type).

This covers all current NFL stadiums plus historical/international venues that
appear in the nfl.games table.

Usage:
    docker exec earl-knows-football-api-1 python -m app.ingestion.seed_nfl_venues
"""
import asyncio
import logging

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("earl.seed_nfl_venues")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# (venue_name, city, state, country, lat, lng, roof_type, surface_type, capacity)
# roof_type: Dome, Retractable, Open, Retractable_Roof (MLB-style), null
VENUE_DATA = [
    # Active NFL stadiums
    ("AT&T Stadium", "Arlington", "TX", "USA", 32.7473, -97.0945, "Retractable", "Artificial", 80000),
    ("Acrisure Stadium", "Pittsburgh", "PA", "USA", 40.4468, -80.0158, "Open", "Grass", 68400),
    ("Allegiant Stadium", "Las Vegas", "NV", "USA", 36.0905, -115.1834, "Dome", "Artificial", 65000),
    ("Bank of America Stadium", "Charlotte", "NC", "USA", 35.2258, -80.8529, "Open", "Artificial", 75523),
    ("Caesars Superdome", "New Orleans", "LA", "USA", 29.9509, -90.0814, "Dome", "Artificial", 73208),
    ("Empower Field at Mile High", "Denver", "CO", "USA", 39.7439, -105.0201, "Open", "Grass", 76125),
    ("EverBank Stadium", "Jacksonville", "FL", "USA", 30.3239, -81.6374, "Open", "Artificial", 67814),
    ("Ford Field", "Detroit", "MI", "USA", 42.3400, -83.0458, "Dome", "Artificial", 65000),
    ("GEHA Field at Arrowhead Stadium", "Kansas City", "MO", "USA", 39.0489, -94.4839, "Open", "Grass", 76416),
    ("Gillette Stadium", "Foxborough", "MA", "USA", 42.0909, -71.2643, "Open", "Artificial", 66829),
    ("Hard Rock Stadium", "Miami Gardens", "FL", "USA", 25.9580, -80.2389, "Open", "Grass", 65326),
    ("Highmark Stadium", "Orchard Park", "NY", "USA", 42.7769, -78.7874, "Open", "Artificial", 71608),
    ("Huntington Bank Field", "Cleveland", "OH", "USA", 41.5061, -81.6995, "Open", "Grass", 67895),
    ("Lambeau Field", "Green Bay", "WI", "USA", 44.5013, -88.0622, "Open", "Grass", 81441),
    ("Levi's Stadium", "Santa Clara", "CA", "USA", 37.4033, -121.9696, "Open", "Grass", 68500),
    ("Lincoln Financial Field", "Philadelphia", "PA", "USA", 39.9008, -75.1674, "Open", "Grass", 69596),
    ("Los Angeles Memorial Coliseum", "Los Angeles", "CA", "USA", 34.0141, -118.2877, "Open", "Grass", 77433),
    ("Lucas Oil Stadium", "Indianapolis", "IN", "USA", 39.7601, -86.1639, "Retractable", "Artificial", 67000),
    ("Lumen Field", "Seattle", "WA", "USA", 47.5953, -122.3317, "Open", "Artificial", 68740),
    ("M&T Bank Stadium", "Baltimore", "MD", "USA", 39.2779, -76.6226, "Open", "Grass", 71008),
    ("Mercedes-Benz Stadium", "Atlanta", "GA", "USA", 33.7550, -84.4009, "Retractable", "Artificial", 71000),
    ("MetLife Stadium", "East Rutherford", "NJ", "USA", 40.8135, -74.0744, "Open", "Artificial", 82500),
    ("NRG Stadium", "Houston", "TX", "USA", 29.6847, -95.4107, "Retractable", "Artificial", 72000),
    ("Nissan Stadium", "Nashville", "TN", "USA", 36.1665, -86.7714, "Open", "Grass", 69143),
    ("Northwest Stadium", "Landover", "MD", "USA", 38.9076, -76.8650, "Open", "Grass", 62000),
    ("Paycor Stadium", "Cincinnati", "OH", "USA", 39.0954, -84.5160, "Open", "Artificial", 65515),
    ("Raymond James Stadium", "Tampa", "FL", "USA", 27.9759, -82.5033, "Open", "Grass", 65618),
    ("SoFi Stadium", "Inglewood", "CA", "USA", 33.9534, -118.3388, "Open", "Artificial", 70240),
    ("Soldier Field", "Chicago", "IL", "USA", 41.8625, -87.6166, "Open", "Grass", 61500),
    ("State Farm Stadium", "Glendale", "AZ", "USA", 33.5276, -112.2624, "Retractable", "Grass", 63400),
    ("U.S. Bank Stadium", "Minneapolis", "MN", "USA", 44.9738, -93.2575, "Dome", "Artificial", 66860),
    ("Georgia Dome", "Atlanta", "GA", "USA", 33.7578, -84.4011, "Dome", "Artificial", 71250),
    ("Highmark Stadium (Old)", "Orchard Park", "NY", "USA", 42.7737, -78.7872, "Open", "Artificial", 71608),

    # International venues
    ("Allianz Arena", "Munich", "BY", "Germany", 48.2188, 11.6247, "Open", "Grass", 75000),
    ("Corinthians Arena", "Sao Paulo", "SP", "Brazil", 23.5453, -46.4744, "Open", "Grass", 49205),
    ("Croke Park", "Dublin", "D", "Ireland", 53.3604, -6.2519, "Open", "Grass", 82300),
    ("Estadio Azteca", "Mexico City", "CDMX", "Mexico", 19.3029, -99.1504, "Open", "Grass", 87523),
    ("Estadio Banorte", "Monterrey", "NL", "Mexico", 25.6726, -100.2939, "Open", "Grass", 53500),
    ("Frankfurt Stadium", "Frankfurt", "HE", "Germany", 50.0687, 8.6455, "Open", "Grass", 58000),
    ("Maracanã Stadium", "Rio de Janeiro", "RJ", "Brazil", 22.9121, -43.2300, "Open", "Grass", 78838),
    ("Melbourne Cricket Ground", "Melbourne", "VIC", "Australia", 37.8199, 144.9834, "Open", "Grass", 100024),
    ("Olympic Stadium Berlin", "Berlin", "BE", "Germany", 52.5147, 13.2395, "Open", "Grass", 74475),
    ("Santiago Bernabéu", "Madrid", "MD", "Spain", 40.4530, -3.6883, "Open", "Grass", 81044),
    ("Stade de France", "Saint-Denis", "IDF", "France", 48.9244, 2.3602, "Open", "Grass", 81338),
    ("Tottenham Hotspur Stadium", "London", "ENG", "United Kingdom", 51.6033, -0.0658, "Open", "Grass", 62850),
    ("Twickenham Stadium", "London", "ENG", "United Kingdom", 51.4555, -0.3366, "Open", "Grass", 82000),
    ("Wembley Stadium", "London", "ENG", "United Kingdom", 51.5559, -0.2796, "Open", "Grass", 90000),
    ("Dignity Health Sports Park", "Carson", "CA", "USA", 33.8643, -118.2611, "Open", "Grass", 27000),
    ("FC Bayern Munich Stadium", "Munich", "BY", "Germany", 48.2188, 11.6247, "Open", "Grass", 75000),
]


async def seed():
    async with async_session() as db:
        seeded = 0
        existing = 0
        missing_in_db = []

        # Fetch existing venue names
        result = await db.execute(text("SELECT LOWER(name) FROM nfl.venues"))
        existing_names = {row[0] for row in result.fetchall()}

        for name, city, state, country, lat, lng, roof_type, surface, capacity in VENUE_DATA:
            if name.strip().lower() in existing_names:
                existing += 1
                continue

            await db.execute(
                text("""
                    INSERT INTO nfl.venues (name, city, state, country, latitude, longitude, roof_type, surface_type, capacity)
                    VALUES (:name, :city, :state, :country, :lat, :lng, :roof, :surface, :capacity)
                """),
                {
                    "name": name, "city": city, "state": state, "country": country,
                    "lat": lat, "lng": lng, "roof": roof_type, "surface": surface,
                    "capacity": capacity,
                },
            )
            seeded += 1

        await db.commit()
        logger.info(f"Inserted {seeded} new venues, {existing} already existed")

        # Check for venue names in nfl.games not yet in nfl.venues
        result = await db.execute(text("""
            SELECT DISTINCT g.venue AS venue_name
            FROM nfl.games g
            LEFT JOIN nfl.venues v ON LOWER(v.name) = LOWER(g.venue)
            WHERE g.venue IS NOT NULL AND v.id IS NULL
            ORDER BY g.venue
        """))
        missing = result.fetchall()
        if missing:
            logger.warning(f"Games reference {len(missing)} venues not in nfl.venues:")
            for row in missing:
                logger.warning(f"  \"{row[0]}\"")


async def link_game_venues():
    """Set nfl.games.venue_id based on venue name matching."""
    async with async_session() as db:
        result = await db.execute(text("""
            UPDATE nfl.games g
            SET venue_id = v.id
            FROM nfl.venues v
            WHERE LOWER(v.name) = LOWER(g.venue)
              AND g.venue_id IS NULL
        """))
        matched = result.rowcount

        await db.commit()

        # Check for games that still don't have a venue match
        result = await db.execute(text("""
            SELECT COUNT(*) FROM nfl.games WHERE venue_id IS NULL
        """))
        still_null = result.scalar()

        logger.info(f"Linked {matched} games to venues, {still_null} games still unmatched")


if __name__ == "__main__":
    asyncio.run(seed())
    asyncio.run(link_game_venues())  # separate session from seed
