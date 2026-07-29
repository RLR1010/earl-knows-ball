"""
Backfill missing MLB venue IDs.

For games that have a venue name but NULL venue_id (mostly 2012-2019):
- Match by name against existing mlb.venues
- Use known alias map for renamed/replaced venues
- For remaining unmatched venues, fetch venue ID from the game's API data
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import httpx
from sqlalchemy import select, text
from app.database import async_session
from app.models.mlb import MLBVenue


# Known venue name → mlb_venue_id (handles renames and historical names)
ALIASES = {
    "angel stadium of anaheim": 1,
    "us cellular field": 4,
    "guaranteed rate field": 4,
    "o.co coliseum": 10,
    "rangers ballpark in arlington": 13,
    "globe life park in arlington": 13,
    "turner field": 16,
    "miller park": 32,
    "safeco field": 680,
    "minute maid park": 2392,
    "at&t park": 2395,
    "marlins park": 4169,
    "suntrust park": 4705,
    "assured warranty field": 4169,
    "target field": 3312,
    "sahlen field": 2756,
    "td ameritrade park": 5365,
    "london stadium": 5381,
    "fort bragg field": 5010,
    "sydney cricket ground": 4589,
    "hiram bithorn stadium": 2535,
    "tokyo dome": 2397,
    "bb&t ballpark": 2738,
    "estadio de beisbol monterrey": 2701,
}


async def fetch_venue_from_mlb_game(client: httpx.AsyncClient, mlb_game_id: int) -> dict | None:
    """Fetch venue info from a game's live feed."""
    url = f"https://statsapi.mlb.com/api/v1.1/game/{mlb_game_id}/feed/live"
    try:
        resp = await client.get(url)
        if resp.status_code == 200:
            data = resp.json()
            v = data.get("gameData", {}).get("venue", {})
            if v.get("id"):
                return {"id": v["id"], "name": v.get("name")}
    except Exception:
        pass
    return None


async def main():
    async with async_session() as db:
        # 1. Get all unique venue names with null venue_id (and sample mlb_game_id)
        result = await db.execute(
            text("SELECT g.venue, MIN(g.mlb_game_id) AS sample_game_id "
                 "FROM mlb.games g "
                 "WHERE g.venue IS NOT NULL AND g.venue_id IS NULL "
                 "GROUP BY g.venue "
                 "ORDER BY g.venue")
        )
        rows = result.all()
        print(f"Found {len(rows)} unique venue names with null venue_id\n")

        # 2. Get existing venues in our DB (name → mlb_venue_id)
        existing_rows = await db.execute(select(MLBVenue.mlb_venue_id, MLBVenue.name))
        db_name_to_id = {row[1].strip().lower(): row[0] for row in existing_rows}

        created = 0
        matched_db = 0
        matched_alias = 0
        matched_api = 0
        skipped = 0

        async with httpx.AsyncClient(timeout=15) as client:
            for venue_name, sample_game_id in rows:
                vname_lower = venue_name.strip().lower()

                # Try: exact match in our DB
                if vname_lower in db_name_to_id:
                    mlb_venue_id = db_name_to_id[vname_lower]
                    await db.execute(
                        text("UPDATE mlb.games SET venue_id = :vid WHERE venue = :v AND venue_id IS NULL"),
                        {"vid": mlb_venue_id, "v": venue_name}
                    )
                    matched_db += 1
                    print(f"  DB:   '{venue_name}' → {mlb_venue_id}")
                    continue

                # Try: known alias
                if vname_lower in ALIASES:
                    mlb_venue_id = ALIASES[vname_lower]

                    # Ensure it exists in mlb.venues
                    check = await db.execute(
                        select(MLBVenue).where(MLBVenue.mlb_venue_id == mlb_venue_id)
                    )
                    if not check.scalar_one_or_none():
                        db.add(MLBVenue(mlb_venue_id=mlb_venue_id, name=venue_name, city="Unknown"))
                        created += 1
                        await db.flush()

                    await db.execute(
                        text("UPDATE mlb.games SET venue_id = :vid WHERE venue = :v AND venue_id IS NULL"),
                        {"vid": mlb_venue_id, "v": venue_name}
                    )
                    matched_alias += 1
                    print(f"  ALIAS: '{venue_name}' → {mlb_venue_id}")
                    continue

                # Try: fetch venue ID from a sample game's API data
                if sample_game_id:
                    api_venue = await fetch_venue_from_mlb_game(client, sample_game_id)
                    if api_venue:
                        vid = api_venue["id"]
                        api_name = api_venue.get("name", venue_name)

                        check = await db.execute(
                            select(MLBVenue).where(MLBVenue.mlb_venue_id == vid)
                        )
                        if not check.scalar_one_or_none():
                            db.add(MLBVenue(mlb_venue_id=vid, name=api_name, city="Unknown"))
                            created += 1
                            await db.flush()

                        await db.execute(
                            text("UPDATE mlb.games SET venue_id = :vid WHERE venue = :v AND venue_id IS NULL"),
                            {"vid": vid, "v": venue_name}
                        )
                        matched_api += 1
                        print(f"  API:  '{venue_name}' → {vid}")
                        continue

                print(f"  SKIP: '{venue_name}' — no match found")
                skipped += 1

        await db.commit()

        # Verify
        after = await db.execute(
            text("SELECT COUNT(*) FROM mlb.games WHERE venue IS NOT NULL AND venue_id IS NULL")
        )
        remaining = after.scalar_one()

        print(f"\nDone!")
        print(f"  Created venues:  {created}")
        print(f"  Matched by name: {matched_db}")
        print(f"  Matched by alias: {matched_alias}")
        print(f"  Matched by API:  {matched_api}")
        print(f"  Skipped:         {skipped}")
        print(f"  Remaining null:  {remaining}")


if __name__ == "__main__":
    asyncio.run(main())
