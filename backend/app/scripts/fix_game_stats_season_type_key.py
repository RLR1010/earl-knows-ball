"""Fix nfl.game_stats: (1) unique key must include season_type so a PRESEASON row can no
longer overwrite a REGULAR-SEASON row that shares (season, week, team, opponent);
(2) repair the 2016 week-5 rows that were mislabeled 'PRE' by an earlier nflverse file vintage
(values were verified against nfl.games — every one is a real 2016 REG week-5 game);
(3) repair two orphan rows whose season was written as the 2-digit '17'.

Idempotent. Run: PYTHONPATH=. venv/bin/python app/scripts/fix_game_stats_season_type_key.py
"""
import asyncio
import asyncpg
from app.db_urls import PSYCOPG2_DATABASE_URL as D

NEW_KEY = "game_stats_season_type_week_team_opp_key"
OLD_KEY = "game_stats_season_week_team_abbr_opponent_abbr_key"


async def main():
    c = await asyncpg.connect(D)
    try:
        # 0. pre-flight: are there already two rows that would collide under the new key?
        dups = await c.fetch("""select season, season_type, week, team_abbr, opponent_abbr, count(*) n
            from nfl.game_stats group by 1,2,3,4,5 having count(*)>1""")
        print("rows colliding under NEW key before migration:", len(dups))
        for r in dups[:10]:
            print("   ", dict(r))

        # 1. repair mislabeled 2016 week-5 rows (verified: all match a real 2016 REG game)
        n1 = await c.execute("""update nfl.game_stats set season_type='REG'
            where season=2016 and season_type='PRE' and week=5""")
        print("relabeled 2016 wk5 PRE->REG:", n1)

        # 2. repair 2-digit-season orphans
        n2 = await c.execute("update nfl.game_stats set season=2017 where season=17")
        print("fixed season=17 -> 2017:", n2)

        # 3. swap the unique key to include season_type
        have = await c.fetchval("""select count(*) from pg_constraint
            where conrelid='nfl.game_stats'::regclass and conname=$1""", OLD_KEY)
        if have:
            await c.execute(f"alter table nfl.game_stats drop constraint {OLD_KEY}")
            print("dropped", OLD_KEY)
        exists = await c.fetchval("""select count(*) from pg_constraint
            where conrelid='nfl.game_stats'::regclass and conname=$1""", NEW_KEY)
        if not exists:
            await c.execute(f"""alter table nfl.game_stats
                add constraint {NEW_KEY} unique (season, season_type, week, team_abbr, opponent_abbr)""")
            print("added", NEW_KEY)
        else:
            print(NEW_KEY, "already present")

        # 4. report
        for r in await c.fetch("""select season_type, count(*) n from nfl.game_stats
            where season between 2016 and 2026 group by 1 order by 1"""):
            print("  ", dict(r))
        for r in await c.fetch("""select season, count(*) n from nfl.game_stats
            where season_type='REG' and season between 2016 and 2025 group by 1 order by 1"""):
            print("   REG", dict(r))
    finally:
        await c.close()


asyncio.run(main())
