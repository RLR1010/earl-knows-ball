"""RECOVERY + correct conversion for pgs.ip baseball-notation rows.

The previous normalize script used a buggy formula (frac_int/3 where frac_int was
the *hundreds* value, e.g. 10, 20) producing whole+3.333 or whole+6.667. This corrupts
the 2579 rows. We recover the TRUE decimal innings deterministically:

  For a row whose stored value came from baseball X.Y (Y in {1,2}):
    buggy_new = whole + Y*10/3
  So:
    if frac(new) ≈ .3-ish  (Y=1): whole = floor(new) - 3  → correct = whole + 1/3
    if frac(new) ≈ .7-ish  (Y=2): whole = floor(new) - 6  → correct = whole + 2/3

WE ONLY TOUCH ROWS WHOSE CURRENT ip matches the buggy-output signature
(frac ≈ .3333 or .6667) so we don't disturb any genuine decimal rows.
Recovery is deterministic from the buggy value alone.
"""
import asyncio
import math
import sqlalchemy as sa
from app.database import async_session


async def main() -> None:
    async with async_session() as db:
        # candidate rows: those whose current ip has the buggy residual (~.333 or ~.667)
        sel = sa.text("""
            SELECT id, ip FROM mlb.pitcher_game_stats
            WHERE ip IS NOT NULL
              AND ( round((ip - floor(ip)) * 1000)::int IN (333, 334, 666, 667) )
        """)
        rows = (await db.execute(sel)).fetchall()
        print(f"candidate rows matching buggy-output signature: {len(rows)}")

        upd = sa.text("UPDATE mlb.pitcher_game_stats SET ip = :new_ip WHERE id = :id")
        fixed = 0
        for r in rows:
            cur = float(r[1])
            frac = round((cur - math.floor(cur)) * 1000)
            # determine Y and whole
            if frac in (333, 334):   # Y=1: buggy = whole + 3.333
                whole = math.floor(cur) - 3
                correct = round(whole + 1 / 3.0, 3)
                y = 1
            elif frac in (666, 667):  # Y=2: buggy = whole + 6.667
                whole = math.floor(cur) - 6
                correct = round(whole + 2 / 3.0, 3)
                y = 2
            else:
                print(f"  SKIP id={r[0]} cur={cur} frac={frac} (not buggy sig)")
                continue
            # sanity: recovered whole must be >= 0 and correct must be < 15 (max ~9 IP)
            if whole < 0 or correct < 0 or correct > 15:
                print(f"  !! IMPLAUSIBLE id={r[0]} cur={cur} -> whole={whole} correct={correct}; SKIP")
                continue
            await db.execute(upd, {"new_ip": correct, "id": r[0]})
            fixed += 1
            if fixed <= 5 or fixed % 500 == 0:
                print(f"  [{fixed}] id={r[0]} cur={cur} -> whole={whole} Y={y} correct={correct}")
        await db.commit()
        print(f"recovered+converted {fixed} rows -> committed")

        # verify: no .1/.2 baseball rows and no .333/.667 leftovers from the buggy 2579
        rem = (await db.execute(sa.text("""
            SELECT
              count(*) FILTER (WHERE round((ip-floor(ip))*100)::int IN (10,20)) AS ballbase_left,
              count(*) FILTER (WHERE round((ip-floor(ip))*1000)::int IN (333,334,666,667)) AS residual_left
            FROM mlb.pitcher_game_stats WHERE ip IS NOT NULL
        """))).fetchone()
        print(f"after: baseball_left={rem[0]} residual_left={rem[1]}")


asyncio.run(main())
