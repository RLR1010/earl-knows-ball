"""SECOND CLEANUP PASS — delete residual pollution rows missed by count-dominance owner map.
These aids' TRUE owner is known from ESPN (verified live). Any row carrying one of these aids
under a DIFFERENT player is pollution -> delete; fix espn_id to the real athlete id.
Uses ESPN-verified owner, NOT row-count dominance (the failure mode).
DRY-RUN unless --commit."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
COMMIT='--commit' in sys.argv

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()

# aid -> (correct owner NAME, correct owner espn_id) verified via ESPN this session
# These are foreign ids found polluting real players' pgs. Owned by a DIFFERENT real player row.
VERIFIED = {
 6462:"Marcus Morris Sr", 2444:"JR Smith", 2528779:"Reggie Bullock Jr", 1721:"Roger Mason Jr",
 4017839:"Juancho Hernangomez", 2581084:"Johnny O'Bryant III", 2528096:"Mike James",
 6526:"Walker Russell Jr", 3240:"Marcus Williams", 4412182:"Isaiah Canaan", 4415554:"Corey Brewer",
 4895499:"Wayne Selden", 4610145:"Daryl Macon", 4432528:"Karim Mane", 5104156:"Armel Traore",
 # 3057304 is Vince Hunter's OWN real id (minority) — handled specially
}

def q1(s,p=None): cur.execute(s,p or []); return cur.fetchone()[0]

print("BEFORE pgs=%s" % q1("SELECT count(*) FROM nba.player_game_stats"))

# Find the CORRECT owner player row for each aid (player whose real espn_id == aid)
# Use espn_id column as the owner signal where it matches
owner_pid={}
for aid in VERIFIED:
    cur.execute("SELECT id FROM nba.players WHERE espn_id=%s", (aid,))
    row=cur.fetchone()
    owner_pid[aid]= row[0] if row else None

print("aid -> owner player row:")
for aid,nm in VERIFIED.items():
    print(f"   {aid} ({nm}) -> pid {owner_pid.get(aid)}")

# Collect rows to delete: rows with these aids whose player_id != owner
to_del=[]
log=[]
for aid in VERIFIED:
    cur.execute("""SELECT pgs.id, pgs.player_id, p.name FROM nba.player_game_stats pgs
        JOIN nba.players p ON p.id=pgs.player_id
        WHERE pgs.nba_player_id=%s""", (aid,))
    for rid,pid,nm in cur.fetchall():
        if owner_pid.get(aid)!=pid:
            to_del.append(rid)
            if len(log)<25: log.append((aid,nm,pid,owner_pid.get(aid)))

print(f"\nPHANTOM rows to DELETE (2nd pass): {len(to_del)}")
for aid,nm,pid,op in log:
    print(f"   aid={aid}({nm}) on pid={pid} -> owner pid={op} DELETE")

if COMMIT:
    for i in range(0,len(to_del),500):
        cur.execute("DELETE FROM nba.player_game_stats WHERE id=ANY(%s)", (to_del[i:i+500],))
    # Fix espn_id column where it's wrong (e.g. Vince Hunter 1384: 4065732->3057304)
    # 1384: real id = 3057304 (Vince Shamar Hunter)
    cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=1384 AND nba_player_id=3057304")
    if cur.fetchone()[0]>0:
        cur.execute("UPDATE nba.players SET espn_id=3057304 WHERE id=1384")
        print("fixed Vince Hunter(1384) espn_id -> 3057304")
    conn.commit()
    print(f"COMMITTED. pgs now=%s" % q1("SELECT count(*) FROM nba.player_game_stats"))
else:
    conn.rollback()
    print("DRY RUN — rollback. rerun with --commit")
cur.close(); conn.close()
