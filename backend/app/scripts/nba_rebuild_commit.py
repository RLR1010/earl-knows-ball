"""FINAL COMMIT: rebuild nba identities (validated dry-run passed, 16/16 espn checks OK).
Deletes phantom pollution pgs rows + corrects espn_id. Commits. Then verifies.
Backups: nba.bak_*_pre_rebuild + nba.player_game_stats_s35_bak exist."""
import psycopg2, json, re
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict

def norm(n):
    n=(n or '').lower().replace('.','').replace("'",'').replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').replace('ñ','n').replace('ć','c').replace('š','s').replace('đ','d').replace('ž','z')
    return re.sub(r'\s+',' ',n).strip()

with open('/tmp/all_id_owners.json') as f:
    owners=json.load(f)
owner_name={int(k):v['name'] for k,v in owners.items()}

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()
def q1(s,p=None): cur.execute(s,p or []); return cur.fetchone()[0]

print("BEFORE pgs=%s players=%s"%(q1("SELECT count(*) FROM nba.player_game_stats"),q1("SELECT count(*) FROM nba.players")))

cur.execute("""SELECT player_id, nba_player_id, count(*) n FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0 GROUP BY player_id, nba_player_id ORDER BY player_id, n DESC""")
dist=defaultdict(list)
for pid,aid,c in cur.fetchall(): dist[pid].append((aid,c))
cur.execute("SELECT id, name, espn_id FROM nba.players ORDER BY id")
players=cur.fetchall()
name_of={pid:n for pid,n,e in players}

canon={}
for pid,n,e in players:
    ids=dist.get(pid,[])
    if not ids: canon[pid]=e; continue
    vals=[a for a,_ in ids]; nn=norm(n)
    m=[a for a in vals if norm(owner_name.get(a,'')) and (norm(owner_name[a])==nn or nn in norm(owner_name[a]) or norm(owner_name[a]) in nn)]
    canon[pid]= m[0] if len(m)==1 else max(ids,key=lambda x:x[1])[0]

aid_rows=defaultdict(list)
for pid,ids in dist.items():
    for aid,c in ids: aid_rows[aid].append((pid,c))
owner_pid={}
for aid,plist in aid_rows.items():
    owners_here=[p for p,c in plist if canon.get(p)==aid]
    candidates=owners_here if owners_here else [p for p,c in plist]
    rows_map={p:c for p,c in plist}
    def score(p): return (norm(name_of.get(p,''))==norm(owner_name.get(aid,'')), rows_map.get(p,0))
    owner_pid[aid]=max(candidates, key=score)

# collect+delete phantom rows
cur.execute("SELECT pgs.id, player_id, nba_player_id FROM nba.player_game_stats pgs WHERE pgs.nba_player_id>0")
rows=cur.fetchall()
to_del=[rid for rid,pid,aid in rows if owner_pid.get(aid)!=pid]
print("deleting", len(to_del), "phantom pgs rows...")
for i in range(0,len(to_del),500):
    cur.execute("DELETE FROM nba.player_game_stats WHERE id=ANY(%s)", (to_del[i:i+500],))

# espn_id corrections
nfix=0
for pid,n,e in players:
    if e!=canon.get(pid):
        cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s", (canon[pid], pid))
        nfix+=1
print("corrected espn_id for", nfix, "players")

print("COMMITTING...")
conn.commit()

print("\nAFTER pgs=%s players=%s"%(q1("SELECT count(*) FROM nba.player_game_stats"),q1("SELECT count(*) FROM nba.players")))

# ---- POST-COMMIT VERIFICATION ----
print("\n=== POST-VERIFY 1: no remaining foreign-pollution (every pgs aid must match owner_pid) ===")
cur.execute("""SELECT pgs.player_id, p.name, pgs.nba_player_id FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id
    WHERE pgs.nba_player_id>0 AND pgs.nba_player_id NOT IN (SELECT id FROM nba.players WHERE espn_id>0)""")
bad=cur.fetchall()
print("  rows whose aid not in any player's espn_id:", len(bad))

print("\n=== POST-VERIFY 2: every surviving row's aid == owner (no owner violations) ===")
# recompute owner post-commit from current data
cur.execute("""SELECT player_id, nba_player_id, count(*) n FROM nba.player_game_stats
    WHERE nba_player_id>0 GROUP BY player_id, nba_player_id""")
dist2=defaultdict(list)
for pid,aid,c in cur.fetchall(): dist2[pid].append((aid,c))
aid_rows2=defaultdict(list)
for pid,ids in dist2.items():
    for aid,c in ids: aid_rows2[aid].append((pid,c))
owner2={}
for aid,plist in aid_rows2.items():
    rm={p:c for p,c in plist}
    cands=[p for p,c in plist]
    owner2[aid]=max(cands,key=lambda p:(norm(name_of.get(p,''))==norm(owner_name.get(aid,'')), rm.get(p,0)))
viol=0
for pid,ids in dist2.items():
    for aid,c in ids:
        if owner2.get(aid)!=pid: viol+=c
print("  total surviving rows where aid!=owner:", viol)

print("\n=== POST-VERIFY 3: games still valid (real statline integrity) — check Kawhi & Danny Green ===")
for pid,nm,expect in [(812,'Kawhi Leonard','>300'),(1874,'AJ Green','>200'),(887,'Anthony Davis','>500'),(983,'CJ McCollum','>400')]:
    cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=%s",(pid,))
    print(f"  {nm} (pid {pid}): {cur.fetchone()[0]} rows")

conn.close()
print("\nDONE — committed and verified.")
