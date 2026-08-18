"""Remove phantom player row 1200 (Willy Hernangómez/Juancho collision identity).
pid 2382 = the REAL Willy Hernangomez (espn 2999409, 171 rows). pid 1200 is the corrupted
collision identity whose surviving rows are Juancho's Denver-era games (2016-19) + DNP noise,
all with aid=None/0 (unattributable). Delete 1200's rows across all referencing tables + the row.
Dry-run unless --commit."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
COMMIT='--commit' in sys.argv
conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()
def q(p=None): cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=1200"); return cur.fetchone()[0]

print("pid 1200 before:", {t: cur.execute(f"SELECT count(*) FROM nba.{t} WHERE player_id=1200") or cur.fetchone()[0] for t in ['player_game_stats','player_splits','player_season_stats','dfs_salaries']})
print("pid 2382 (real Willy) pgs:", cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=2382") or cur.fetchone()[0])

if COMMIT:
    for t in ['player_game_stats','player_splits','player_season_stats','dfs_salaries']:
        cur.execute(f"DELETE FROM nba.{t} WHERE player_id=1200")
    cur.execute("DELETE FROM nba.players WHERE id=1200")
    conn.commit()
    print("COMMITTED: removed pid 1200 + all its rows.")
    print("pid 2382 (real Willy) pgs now:", cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=2382") or cur.fetchone()[0] if False else None)
    cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=2382"); print("  =", cur.fetchone()[0])
else:
    conn.rollback()
    print("DRY RUN — rollback. rerun --commit")
cur.close(); conn.close()
