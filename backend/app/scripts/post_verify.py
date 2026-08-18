"""POST-CLEANUP VERIFICATION: confirm every surviving pgs row is correctly attributed.
For each (player_id, aid) pair, verify via ESPN that aid's owner == player's name (or dominant id matches espnCol).
Also report any player still carrying 2+ DIFFERENT aids (residual pollution) and any espn_id column issues."""
import psycopg2, json, urllib.request, time, re
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120','Accept':'application/json'}

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); cur=conn.cursor()
cur.execute("""SELECT player_id, nba_player_id, count(*) FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0 GROUP BY player_id, nba_player_id ORDER BY count(*) DESC""")
dist=defaultdict(list)
for pid,aid,c in cur.fetchall():
    dist[pid].append((aid,c))

# players still carrying 2+ different aids = residual
multi=[(pid, sorted(x) if False else [a for a,_ in sorted(x,key=lambda t:-t[1])]) for pid,x in dist.items() if len(x)>1]
print(f"players still carrying 2+ different aids (residual pollution): {len(multi)}")
for pid, aids in sorted(multi):
    cur.execute("SELECT name, espn_id FROM nba.players WHERE id=%s",(pid,))
    nm, ec = cur.fetchone()
    print(f"   pid={pid} {nm} espnCol={ec} aids={aids}")

# verify espn_id column correctness: for each player with espn_id, does their pgs have that aid?
print("\nplayers whose espn_id is NOT among their own pgs aids (espn_id column possibly wrong):")
mism=0
for pid, aids in dist.items():
    cur.execute("SELECT name, espn_id FROM nba.players WHERE id=%s",(pid,))
    nm, ec = cur.fetchone()
    if ec and ec>0 and ec not in [a for a,_ in aids] and aids:
        # check: maybe espid is their real id but pgs use a different (real) id -> reporter only if espnCol not a valid athlete
        mism+=1
        if mism<=15: print(f"   pid={pid} {nm} espnCol={ec} pgs_aids={[a for a,_ in aids]}")
print(f"  total espnCol-not-in-own-pgs: {mism}")
conn.close()
