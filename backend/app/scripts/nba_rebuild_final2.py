"""FINAL exec v2 — owner-based phantom deletion + espn_id correction + verified merges.
Phantom pgs row = the athlete-id's TRUE owner (by ESPN) is a DIFFERENT player than the row's player_id.
This correctly deletes Vince-Hunter's DeAndre rows (owner=DeAndre) AND keeps entities consistent.
DRY RUN (no commit) — review then commit."""
import psycopg2, json, re, sys
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

# per-player id distribution
cur.execute("""SELECT player_id, nba_player_id, count(*) n FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0 GROUP BY player_id, nba_player_id ORDER BY player_id, n DESC""")
dist=defaultdict(list)
for pid,aid,c in cur.fetchall(): dist[pid].append((aid,c))
cur.execute("SELECT id, name, espn_id FROM nba.players ORDER BY id")
players=cur.fetchall()
name_of={pid:n for pid,n,e in players}

# canonical id per player: ESPN-name match; else dominant
canon={}
for pid,n,e in players:
    ids=dist.get(pid,[])
    if not ids: canon[pid]=e; continue
    vals=[a for a,_ in ids]; nn=norm(n)
    m=[a for a in vals if norm(owner_name.get(a,'')) and (norm(owner_name[a])==nn or nn in norm(owner_name[a]) or norm(owner_name[a]) in nn)]
    canon[pid]= m[0] if len(m)==1 else max(ids,key=lambda x:x[1])[0]

# TRUE owner of each athlete id = player whose canonical==aid and name best matches ESPN owner; else dominant pid
# Build aid->owner_pid by awarding to the player who has the MOST rows with that aid AND whose canonical==aid
aid_rows=defaultdict(list)
for pid,ids in dist.items():
    for aid,c in ids:
        aid_rows[aid].append((pid,c))
owner_pid={}
for aid,plist in aid_rows.items():
    # prefer a player whose canonical_id==aid (they own it); among those pick most rows
    owners_here=[p for p,c in plist if canon.get(p)==aid]
    candidates = owners_here if owners_here else [p for p,c in plist]
    # tie-break: name-match to ESPN owner (primary) then row count
    rows_map={p:c for p,c in plist}
    def score(p): return (norm(name_of.get(p,''))==norm(owner_name.get(aid,'')), rows_map.get(p,0))
    owner_pid[aid]=max(candidates, key=score)

print("BEFORE pgs=%s players=%s"%(q1("SELECT count(*) FROM nba.player_game_stats"),q1("SELECT count(*) FROM nba.players")))

# DELETE phantom rows (owner of aid != row player_id)
cur.execute("SELECT pgs.id, player_id, nba_player_id, points, game_id, name FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id WHERE pgs.nba_player_id>0")
rows=cur.fetchall()
to_del=[]
samp=[]
for rid,pid,aid,pts,gid,nm in rows:
    if owner_pid.get(aid)!=pid:
        to_del.append(rid)
        if len(samp)<15: samp.append((pid,nm,aid,owner_pid.get(aid),pts,gid))
print("PHANTOM pgs rows to DELETE:", len(to_del))
for s in samp: print("   DEL", s)

print("\nSample KEPT (owner matches):")
kept=0
for rid,pid,aid,pts,gid,nm in rows:
    if owner_pid.get(aid)==pid:
        kept+=1
        if kept<=12: print(f"   KEEP {pid} {nm} aid={aid} owner={owner_pid.get(aid)} game={gid}")
print("  ... total kept:", kept)

# espn_id corrections
nfix=0; sfix=[]
for pid,n,e in players:
    if e!=canon.get(pid):
        nfix+=1
        if len(sfix)<20: sfix.append((pid,n,e,canon.get(pid)))
print("\nplayers needing espn_id fix:", nfix)
for p,n,o,c in sfix: print(f"   {p} {n}: {o}->{c}")

print("\nDRY RUN — NOT COMMITTED. pgs would go %s->%s" % (q1("SELECT count(*) FROM nba.player_game_stats"), q1("SELECT count(*) FROM nba.player_game_stats")-len(to_del)))
conn.rollback()  # discard dry run
cur.close(); conn.close()
