#!/usr/bin/env python
"""Merge ESPN stub players (espn_id, no nflverse_id) into their canonical player using the
nflverse espn_id->gsis_id crosswalk (authoritative, no name guessing).

Repoints nfl.player_weekly_stats, nfl.player_splits, nfl.depth_charts, then deletes the stub.
Usage: venv/bin/python app/scripts/merge_espn_stub_players.py [--apply]
"""
import asyncio, sys, io
import httpx, pandas as pd, asyncpg

DSN = "postgresql://earl:goY-4oLs6tGtZlYsX8xx0LSbFbsmX801KSr3O9wcXB2ivmBuPCL12w@localhost:5432/earl_knows_football"
URL = "https://github.com/nflverse/nflverse-data/releases/download/players/players.parquet"


def crosswalk():
    with httpx.Client(timeout=180.0, follow_redirects=True) as cl:
        r = cl.get(URL); r.raise_for_status()
    df = pd.read_parquet(io.BytesIO(r.content))[["espn_id", "gsis_id", "display_name"]]
    df = df[df["espn_id"].notna() & df["gsis_id"].notna()]
    df["espn_id"] = df["espn_id"].apply(lambda x: int(float(x)))
    return {int(e): str(g) for e, g in zip(df["espn_id"], df["gsis_id"])}


async def main(apply=False):
    cw = crosswalk()
    print(f"crosswalk entries: {len(cw):,}")
    c = await asyncpg.connect(DSN)
    stubs = await c.fetch("select id, name, espn_id from nfl.players where nflverse_id is null and espn_id is not null")
    plan = []
    for s in stubs:
        gsis = cw.get(int(s["espn_id"]))
        if not gsis:
            continue
        canon = await c.fetchval("select id from nfl.players where nflverse_id=$1 and id<>$2", gsis, s["id"])
        if canon:
            plan.append((s["id"], canon, int(s["espn_id"]), s["name"]))
    print(f"stubs: {len(stubs):,} | resolvable merges: {len(plan):,} | unresolved: {len(stubs)-len(plan):,}")

    await c.execute("create temp table merge_map(old_id int, canon_id int, espn_id bigint)")
    await c.executemany("insert into merge_map values ($1,$2,$3)", [(p[0], p[1], p[2]) for p in plan])

    async def conflicts(tbl, key):
        kc = ",".join(key)
        a = await c.fetchval(f"""select count(*) from nfl.{tbl} w join merge_map m on m.old_id=w.player_id
             where w.{key[0]} is not null and exists (select 1 from nfl.{tbl} x
               where x.player_id=m.canon_id and x.{key[0]}=w.{key[0]}""" +
            "".join(f" and x.{k}=w.{k}" for k in key[1:]) + ")")
        b = await c.fetchval(f"""select coalesce(sum(n-1),0) from (select count(*) n from nfl.{tbl} w
             join merge_map m on m.old_id=w.player_id group by m.canon_id,{',w.'.join(key)} having count(*)>1) z""")
        return a, b

    for tbl, key in [("player_weekly_stats", ["game_id"]), ("player_splits", ["split_type", "season_id"])]:
        ca, cb = await conflicts(tbl, key)
        print(f"  {tbl}: cross-conflict rows={ca:,}  stub-stub dupes={cb:,}")

    if apply:
        async with c.transaction():
            for tbl, key in [("player_weekly_stats", ["game_id"]), ("player_splits", ["split_type", "season_id"])]:
                kc = ",".join(key)
                await c.execute(f"""delete from nfl.{tbl} w using merge_map m
                    where w.player_id=m.old_id and w.{key[0]} is not null and exists (
                        select 1 from nfl.{tbl} x where x.player_id=m.canon_id and x.{key[0]}=w.{key[0]}""" +
                    "".join(f" and x.{k}=w.{k}" for k in key[1:]) + ")")
                await c.execute(f"""delete from nfl.{tbl} where id in (
                    select id from (select w.id, row_number() over (partition by m.canon_id,{',w.'.join(key)} order by w.id) rn
                      from nfl.{tbl} w join merge_map m on m.old_id=w.player_id) z where rn>1)""")
                await c.execute(f"""update nfl.{tbl} w set player_id=m.canon_id from merge_map m where w.player_id=m.old_id""")
            await c.execute("""update nfl.depth_charts d set player_id=m.canon_id from merge_map m where d.player_id=m.old_id""")
            await c.execute("""delete from nfl.depth_charts where id in (
                select id from (select id, row_number() over (partition by player_id,team_id,position,slot order by id) rn
                  from nfl.depth_charts) z where rn>1)""")
            # free stub espn_id (unique) then adopt on canon
            await c.execute("update nfl.players set espn_id=null where id in (select old_id from merge_map)")
            await c.execute("""update nfl.players p set espn_id=m.espn_id
                 from merge_map m where p.id=m.canon_id and p.espn_id is null""")
            await c.execute("delete from nfl.players where id in (select old_id from merge_map)")
            print(f"APPLIED: merged {len(plan):,} stubs into canonical players")
    else:
        print("(dry-run — re-run with --apply to commit)")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
