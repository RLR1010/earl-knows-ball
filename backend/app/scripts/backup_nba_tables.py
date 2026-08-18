import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL

TABLES = [
    "players",
    "player_game_stats",
    "player_season_stats",
    "cumulative_game_stats",
    "team_rolling_stats",
    "team_splits",
    "betting_lines_consolidated",
]

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

# 1) Drop the badly-named public tables from the previous run
for t in TABLES:
    cur.execute(f'DROP TABLE IF EXISTS public."nba.bak_{t}_pre_rebuild"')

# 2) Create clean backups in the nba schema
print("=== Creating clean backups in nba schema ===")
for t in TABLES:
    bak = f"bak_{t}_pre_rebuild"
    cur.execute(f'DROP TABLE IF EXISTS nba."{bak}"')
    cur.execute(f'CREATE TABLE nba."{bak}" AS TABLE nba."{t}"')
    cur.execute(f'SELECT count(*) FROM nba."{bak}"')
    n = cur.fetchone()[0]
    print(f"  nba.{t} -> nba.{bak}: {n} rows")

cur.close(); conn.close()

# 3) Verify
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL); cur = conn.cursor()
print("\n=== Verify ===")
all_ok = True
for t in TABLES:
    bak = f"bak_{t}_pre_rebuild"
    cur.execute(f'SELECT count(*) FROM nba."{t}"'); a = cur.fetchone()[0]
    cur.execute(f'SELECT count(*) FROM nba."{bak}"'); b = cur.fetchone()[0]
    ok = 'OK' if a==b else 'MISMATCH'
    if a!=b: all_ok=False
    print(f"  {t}: src={a} | backup={b} {ok}")
print("\nALL BACKUPS MATCH:", all_ok)
cur.close(); conn.close()
