"""Fix: convert compass-cardinal wind_direction values in mlb.games to proper
in/out/l_to_r/r_to_l using each park's home_plate_orientation.

Background: a weather backfill copied weather_forecasts.wind_direction_cardinal
(e.g. 'SW') directly into mlb.games.wind_direction. But the model's
data_loader wind feature is:
    wind_calculated = wind_speed * {'out': +1, 'in': -1}.get(wind_direction, 0)
so a cardinal string maps to 0 and the wind is silently ignored. The forecast
task (mlb_weather_forecast.py) already computes proper in/out via
calculate_wind_effect(cardinal->degrees, home_plate_orientation); we reuse the
same function to correct the rows.

No writes to live data beyond the offending wind_direction values.
"""
import sys
sys.path.insert(0, str("/home/rich/.openclaw/workspace/earl-knows-football/backend"))
import asyncpg
from app.database import database_url

DB_URL = database_url.replace("+asyncpg", "")


def quiet_main():
    import asyncio

    URL = DB_URL

    async def run():
        conn = await asyncpg.connect(URL)
        import re
        from app.ingestion.mlb_weather_forecast import calculate_wind_effect, cardinal_to_degrees

        pat = re.compile(
            r"^(N|S|E|W|NE|NW|SE|SW|NNE|NNW|SSE|SSW|ENE|ESE|WNW|WSW)$"
        )
        rows = await conn.fetch(
            """
            SELECT g.id, g.wind_direction AS wdir, v.home_plate_orientation AS orient,
                   g.wind_speed
            FROM mlb.games g
            JOIN mlb.venues v ON v.mlb_venue_id = g.venue_id
            WHERE g.wind_direction ~ '^(N|S|E|W|NE|NW|SE|SW|NNE|NNW|SSE|SSW|ENE|ESE|WNW|WSW)$'
            """
        )
        fixed = 0
        skipped = 0
        for r in rows:
            wdir = r["wdir"]
            orient = r["orient"]
            if not orient:
                skipped += 1
                print(f"  gid {r['id']}: park lacks orientation, leaving {wdir!r}", flush=True)
                continue
            deg = cardinal_to_degrees(wdir)
            effect = calculate_wind_effect(deg, orient)
            if effect is None:
                skipped += 1
                print(f"  gid {r['id']}: could not compute effect for {wdir!r} ({orient}), leaving", flush=True)
                continue
            if effect != wdir:
                await conn.execute(
                    "UPDATE mlb.games SET wind_direction=$1 WHERE id=$2",
                    effect, r["id"],
                )
                print(f"  gid {r['id']}: {wdir!r} -> {effect!r} (orientation {orient}, wind {r['wind_speed']}mph)",
                      flush=True)
                fixed += 1
            else:
                print(f"  gid {r['id']}: {wdir!r} already correct (no change)", flush=True)
        print(f"\nDONE: fixed={fixed} skipped={skipped} total={len(rows)}", flush=True)
        await conn.close()

    asyncio.run(run())


if __name__ == "__main__":
    quiet_main()
