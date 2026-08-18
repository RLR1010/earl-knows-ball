"""Normalize legacy baseball-notation pgs.ip rows (.1/.2) to decimal innings.

pgs.ip must be DECIMAL INNINGS (6.333 = 6 1/3 IP) everywhere, matching what the
ingest writers (boxscore_ingest.py, mlb_pitcher_stats.py) and cumulative_stats.py
already assume. A minority of legacy rows (seasons 7-14) stored ESPN's raw baseball
notation where "6.1" = 6 1/3 and "6.2" = 6 2/3. Convert them:

    new_ip = FLOOR(ip) + frac_int / 3.0     (frac_int = 1 or 2)

Idempotent — after conversion these rows become .333/.667 decimals, so re-running
matches zero rows. Backs up nothing (values are deterministic from the stored ip).
"""
import asyncio
import sqlalchemy as sa
from app.database import async_session


async def main() -> None:
    async with async_session() as db:
        # rows whose fraction is .1 or .2 (baseball notation)
        sel = sa.text("""
            SELECT id, ip FROM mlb.pitcher_game_stats
            WHERE round((ip - floor(ip)) * 100)::int IN (10, 20)
              AND ip IS NOT NULL
        """)
        rows = (await db.execute(sel)).fetchall()
        print(f"baseball-notation rows to convert: {len(rows)}")

        upd = sa.text("UPDATE mlb.pitcher_game_stats SET ip = :new_ip WHERE id = :id")
        n = 0
        for r in rows:
            old = float(r[1])
            whole = int(old)
            frac_int = round((old - whole) * 100)  # 1 or 2
            new_ip = round(whole + frac_int / 3.0, 3)
            await db.execute(upd, {"new_ip": new_ip, "id": r[0]})
            n += 1
            if n <= 5 or n % 500 == 0:
                print(f"  [{n}] id={r[0]} ip {old} -> {new_ip}")
        await db.commit()
        print(f"converted {n} rows -> committed")

        # verify
        rem = (await db.execute(sa.text("""
            SELECT count(*) FROM mlb.pitcher_game_stats
            WHERE round((ip - floor(ip)) * 100)::int IN (10, 20) AND ip IS NOT NULL
        """))).fetchone()[0]
        print(f"remaining baseball-notation rows after: {rem}")


asyncio.run(main())
