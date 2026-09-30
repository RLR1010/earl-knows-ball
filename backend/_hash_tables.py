import sys, json, hashlib
import psycopg2
from app.core import config

_dsn = config.settings.database_url.replace("postgresql+asyncpg", "postgresql").replace("postgresql+psycopg2", "postgresql")
TABLES = ["game_stats","team_rolling_stats","cumulative_game_stats","prior_team_stats",
          "team_badweather_stats","qb_badweather_stats","defensive_rolling_stats",
          "skill_rolling_stats","qb_rolling_stats","kicker_rolling_stats"]

def hash_table(cur, tbl, max_season=2025):
    # canonical content hash: sort row-json texts, independent of physical order
    q = f"""SELECT md5(string_agg(rj, E'\\n' ORDER BY rj)) AS h, count(*) AS n
            FROM (SELECT row_to_json(t)::text rj FROM (SELECT * FROM nfl.{tbl} WHERE season<=%s) t) s"""
    try:
        cur.execute(q, (max_season,))
        return cur.fetchone()
    except Exception as e:
        return ("ERR:"+str(e)[:60], -1)

out = {}
with psycopg2.connect(_dsn) as conn:
    with conn.cursor() as cur:
        for t in TABLES:
            h, n = hash_table(cur, t)
            out[t] = {"h": h, "n": n}
json.dump(out, open(sys.argv[1], "w"), indent=1)
for t, v in out.items():
    print(f"{t:28s} n={v['n']:>8} h={v['h']}")
