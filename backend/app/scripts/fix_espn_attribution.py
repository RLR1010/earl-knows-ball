#!/usr/bin/env python
"""Fix ESPN misattribution: a player P carries espn_id E, but the nflverse crosswalk maps E to a
different gsis owned by another player Q. All pws rows come from the ESPN ingest, so P's rows
actually belong to Q. Move rows + espn_id P->Q, then delete P.

Usage: venv/bin/python app/scripts/fix_espn_attribution.py [--apply]
"""
import asyncio, sys, io
import httpx, pandas as pd, asyncpg
DSN = "postgresql://earl:goY-4oLs6tGtZlYsX8xx0LSbFbsmX801KSr3O9wcXB2ivmBuPCL12w@localhost:5432/earl_knows_football"
URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet"


def crosswalk():
    with httpx.Client(timeout=180.0, follow_redirects=True) as cl:
        r = cl.get(URL); r.raise_for_status()
    df = pd.read_parquet(io.BytesIO(r.content))[["espn_id", "gsis_id"]]
    df = df[df["espn_id"].notna() & df["gsis_id"].notna()]
    df["espn_id"] = df["espn_id"].apply(lambda x: int(float(x)))
    return {int(e): str(g) for e, g in zip(df["espn_id"], df["gsis_id"])}


async def main(apply=False):
    cw = crosswalk()
    c = await asyncpg.connect(DSN)
    rows = await c.fetch("select id,name,nflverse_id,espn_id from nfl.players where espn_id is not null")
    plan, ambiguous, notarget = [], [], 0
    for p in rows:
        g = cw.get(int(p["espn_id"]))
        if not g or g == p["nflverse_id"]:
            continue
        q = await c.fetchrow("select id,espn_id from nfl.players where nflverse_id=$1 and id<>$2", g, p["id"])
        if not q:
            notarget += 1; continue
        if q["espn_id"] is not None and int(q["espn_id"]) != int(p["espn_id"]):
            ambiguous.append((p["id"], p["name"], q["id"])); continue
        plan.append((p["id"], q["id"], int(p["espn_id"]), p["name"]))
    print(f"espn misattribution: fixable={len(plan)}  ambiguous(target has other espn)={len(ambiguous)}  no-target={notarget}")
    await c.execute("create temp table fix_map(p_id int, q_id int, espn_id bigint)")
    await c.executemany("insert into fix_map values ($1,$2,$3)", [(x[0], x[1], x[2]) for x in plan])
    pid = [x[0] for x in plan]
    print("pws rows to move:", await c.fetchval("select count(*) from nfl.player_weekly_stats where player_id=any($1::int[])", pid))
    print("  conflicts with target:", await c.fetchval("""select count(*) from nfl.player_weekly_stats w join fix_map m on m.p_id=w.player_id
        where exists (select 1 from nfl.player_weekly_stats x where x.player_id=m.q_id and x.game_id=w.game_id)"""))
    print("splits rows to move:", await c.fetchval("select count(*) from nfl.player_splits where player_id=any($1::int[])", pid))
    print("sample:", [ (x[0],x[3],x[1]) for x in plan[:6] ])
    if apply:
        async with c.transaction():
            for tbl, key in [("player_weekly_stats", ["game_id"]), ("player_splits", ["split_type", "season_id"])]:
                k0 = key[0]
                await c.execute(f"""delete from nfl.{tbl} w using fix_map m where w.player_id=m.p_id and exists (
                    select 1 from nfl.{tbl} x where x.player_id=m.q_id and x.{k0}=w.{k0}""" +
                    "".join(f" and x.{k}=w.{k}" for k in key[1:]) + ")")
                await c.execute(f"""delete from nfl.{tbl} where id in (select id from (
                    select w.id, row_number() over (partition by m.q_id,{',w.'.join(key)} order by w.id) rn
                    from nfl.{tbl} w join fix_map m on m.p_id=w.player_id) z where rn>1)""")
                await c.execute(f"update nfl.{tbl} w set player_id=m.q_id from fix_map m where w.player_id=m.p_id")
            for t in ["depth_charts", "injuries", "transactions", "dfs_salaries"]:
                await c.execute(f"update nfl.{t} d set player_id=m.q_id from fix_map m where d.player_id=m.p_id")
            await c.execute("update nfl.players set espn_id=null where id in (select p_id from fix_map)")
            await c.execute("update nfl.players p set espn_id=m.espn_id from fix_map m where p.id=m.q_id and p.espn_id is null")
            await c.execute("delete from nfl.players where id in (select p_id from fix_map)")
            print(f"APPLIED: moved {len(plan)} misattributed players")
    else:
        print("(dry-run)")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
