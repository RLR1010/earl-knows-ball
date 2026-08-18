"""Phase 1b: Understand the 336 'no resolvable athlete id' players + name-variant dupes."""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

print("=== 336 players w/o pgs athlete id: how many appear in pgs at all? ===")
cur.execute("""
  SELECT count(*) ,
         count(*) FILTER (WHERE pgs.player_id IS NOT NULL) AS in_pgs
  FROM nba.players p
  LEFT JOIN (SELECT DISTINCT player_id FROM nba.player_game_stats) pgs ON pgs.player_id=p.id
  WHERE p.id NOT IN (
    SELECT p2.id FROM nba.players p2
    JOIN nba.player_game_stats p2s ON p2s.player_id=p2.id
    WHERE p2s.nba_player_id IS NOT NULL AND p2s.nba_player_id>0)
""")
r = cur.fetchone()
print(f"   w/o resolvable athlete id: {r[0]} | of those IN pgs: {r[1]}")

print("\n=== Name-variant / exact-duplicate names in nba.players (same normalized name) ===")
import re
def norm(n):
    n = n.lower()
    n = n.replace('.','').replace("'",'')
    return re.sub(r'\s+',' ',n).strip()
cur.execute("SELECT id, name, espn_id, nba_id FROM nba.players ORDER BY id")
allp = cur.fetchall()
from collections import defaultdict
byname = defaultdict(list)
for pid, nm, espn, nba in allp:
    byname[norm(nm)].append((pid, nm, espn, nba))
print(f"   normalized names with >1 row: {sum(1 for v in byname.values() if len(v)>1)}")
for nm, v in byname.items():
    if len(v)>1:
        print(f"     '{nm}': " + "; ".join(f"pid={pid}({n}) espn={e} nba={nba}" for pid,n,e,nba in v))

cur.close(); conn.close()
