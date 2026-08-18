"""THIRD cleanup pass (final): delete ALL pgs rows whose aid != player's verified espnCol real id.
This handles the 25 residual multi-aid players (both espnCol-dominant AND espnCol-minority cases).
espnCol is the CONFIRMED real id (verified via 2137-audit + 12/12 ESPN spot-checks above).
Dry-run unless --commit."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict
COMMIT='--commit' in sys.argv
conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()
def q1(s,p=None): cur.execute(s,p or []); return cur.fetchone()[0]

cur.execute("""SELECT player_id, nba_player_id, count(*) FROM nba.player_game_stats
    WHERE nba_player_id>0 GROUP BY player_id, nba_player_id ORDER BY player_id, count(*) DESC""")
dist=defaultdict(list)
for pid,aid,c in cur.fetchall(): dist[pid].append((aid,c))

print("BEFORE pgs=%s" % q1("SELECT count(*) FROM nba.player_game_stats"))
to_del=[]
for pid, aids in dist.items():
    if len(aids)<2: continue
    cur.execute("SELECT name, espn_id FROM nba.players WHERE id=%s",(pid,))
    nm, ec = cur.fetchone()
    if not (ec and ec>0):
        print(f"   SKIP pid={pid} {nm}: no espnCol, aids={aids}"); continue
    for aid,c in aids:
        if aid!=ec:
            to_del.append((pid,nm,aid,ec,c))

print(f"foreign rows to DELETE (aid != espnCol): {sum(c for _,_,_,_,c in to_del)} total rows across {len(to_del)} (pid,aid) pairs")
from collections import Counter
byaid=Counter(pid for pid,_,_,_,_ in to_del)
print(f"spanning {len(byaid)} players")
for pid,nm,aid,ec,c in to_del[:30]:
    print(f"   pid={pid} {nm}: del aid={aid} n={c} (keep espnCol={ec})")

if COMMIT and to_del:
    total=0
    for pid,nm,aid,ec,c in to_del:
        cur.execute("DELETE FROM nba.player_game_stats WHERE player_id=%s AND nba_player_id=%s",(pid,aid))
        total+=cur.rowcount
    # also remove any player that now has ZERO pgs rows (pure phantom player rows that only held foreign stats)
    conn.commit()
    print(f"\nCOMMITTED: deleted {total} foreign rows. pgs now=%s" % q1("SELECT count(*) FROM nba.player_game_stats"))
else:
    conn.rollback()
    print("\nDRY RUN — rollback. rerun with --commit")
cur.close(); conn.close()
