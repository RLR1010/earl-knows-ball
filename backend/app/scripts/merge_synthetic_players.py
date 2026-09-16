#!/usr/bin/env python
"""Repoint stats rows from synthetic-id players (nflverse_id like 'BLA097235') to their unique
real-gsis twin (matched exact by lower(name)+position). Fixes remaining double-counted
2026 rookies without deleting any player rows.

Usage: venv/bin/python app/scripts/merge_synthetic_players.py [--apply]
"""
import asyncio, sys, asyncpg
DSN = "postgresql://earl:goY-4oLs6tGtZlYsX8xx0LSbFbsmX801KSr3O9wcXB2ivmBuPCL12w@localhost:5432/earl_knows_football"

MAP_SQL = r"""
  select s.id syn_id, t.id real_id
  from nfl.players s
  join nfl.players t on lower(t.name)=lower(s.name) and t.position=s.position
       and t.nflverse_id ~ '^00-[0-9]+$'
  where s.nflverse_id ~ '^[A-Z]{3}[0-9]+$'
  group by s.id, t.id
  having (select count(*) from nfl.players t2 where lower(t2.name)=lower(s.name)
            and t2.position=s.position and t2.nflverse_id ~ '^00-[0-9]+$')=1
"""


async def main(apply=False):
    c = await asyncpg.connect(DSN)
    pairs = await c.fetch(MAP_SQL)
    print(f"syn->real map pairs: {len(pairs):,}")
    await c.execute("create temp table syn_map(syn_id int, real_id int)")
    await c.executemany("insert into syn_map values ($1,$2)", [(r["syn_id"], r["real_id"]) for r in pairs])
    print("pws rows on mapped syns:", await c.fetchval("select count(*) from nfl.player_weekly_stats w join syn_map m on m.syn_id=w.player_id"))
    print("  conflicts (real already has the game):", await c.fetchval(
        "select count(*) from nfl.player_weekly_stats w join syn_map m on m.syn_id=w.player_id where exists (select 1 from nfl.player_weekly_stats x where x.player_id=m.real_id and x.game_id=w.game_id)"))
    print("splits rows on mapped syns:", await c.fetchval("select count(*) from nfl.player_splits w join syn_map m on m.syn_id=w.player_id"))
    if apply:
        async with c.transaction():
            await c.execute("""delete from nfl.player_weekly_stats w using syn_map m
                where w.player_id=m.syn_id and exists (select 1 from nfl.player_weekly_stats x
                  where x.player_id=m.real_id and x.game_id=w.game_id)""")
            await c.execute("update nfl.player_weekly_stats w set player_id=m.real_id from syn_map m where w.player_id=m.syn_id")
            await c.execute("""delete from nfl.player_splits w using syn_map m
                where w.player_id=m.syn_id and exists (select 1 from nfl.player_splits x
                  where x.player_id=m.real_id and x.split_type=w.split_type and x.season_id=w.season_id)""")
            await c.execute("update nfl.player_splits w set player_id=m.real_id from syn_map m where w.player_id=m.syn_id")
            print("APPLIED: repointed synthetic->real")
    else:
        print("(dry-run)")
    await c.close()


if __name__ == "__main__":
    asyncio.run(main("--apply" in sys.argv))
