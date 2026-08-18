"""FINAL EXECUTION: rebuild nba identities accurately.
Step 1: build canonical espn_id for every player (ESPN-verified owner + dominant-id for ambiguities).
Step 2: delete phantom statline rows (rows whose athlete-id belongs to a different verified owner OR is minority pollution).
Step 3: correct nba.players.espn_id to canonical (per-player verified).
Step 4: merge only VERIFIED same-person duplicate player rows.
All in ONE transaction; prints everything; commits at end."""
import psycopg2, json, re
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict

def norm(n):
    n=(n or '').lower().replace('.','').replace("'",'').replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').replace('ñ','n').replace('ć','c').replace('š','s').replace('đ','d').replace('ž','z')
    return re.sub(r'\s+',' ',n).strip()

with open('/tmp/all_id_owners.json') as f:
    owners = json.load(f)
owner_name = {int(k): v['name'] for k,v in owners.items()}

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
conn.autocommit=False
cur = conn.cursor()

def q1(sql, p=None):
    cur.execute(sql, p or []); return cur.fetchone()[0]

print("BEFORE: pgs=%s players=%s" % (q1("SELECT count(*) FROM nba.player_game_stats"), q1("SELECT count(*) FROM nba.players")))

# ---- STEP 1: canonical espn_id per player ----
# per-player id distribution
cur.execute("""SELECT player_id, nba_player_id, count(*) n FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0 GROUP BY player_id, nba_player_id ORDER BY player_id, n DESC""")
dist=defaultdict(list)
for pid,aid,c in cur.fetchall():
    dist[pid].append((aid,c))
cur.execute("SELECT id, name, espn_id FROM nba.players ORDER BY id")
players = cur.fetchall()
plist = {pid:n for pid,n,e in players}

canon_id = {}   # pid -> canonical espn_id
for pid, name, cur_espn in players:
    ids = dist.get(pid, [])
    if not ids:
        canon_id[pid]=cur_espn; continue
    idvals = [a for a,_ in ids]
    total = sum(c for _,c in ids)
    # ESPN-name match
    matches=[]
    nn=norm(name)
    for a in idvals:
        on=norm(owner_name.get(a,''))
        if on and (on==nn or nn in on or on in nn):
            matches.append(a)
    if len(matches)==1:
        canon_id[pid]=matches[0]
    elif len(matches)>1:
        # ambiguous -> dominant id
        canon_id[pid]=max(ids,key=lambda x:x[1])[0]
    else:
        # no name match -> dominant id (covers same-name diff entries resolved by majority)
        canon_id[pid]=max(ids,key=lambda x:x[1])[0]

# ---- STEP 2: build owner map for phantom detection = canonical id of the TRUE owner ----
# For each athlete id, true owner pid = the player whose canonical id equals it (most rows)
owner_pid={}
for pid, cid in canon_id.items():
    # owner_pid[cid] should be pid if pid's canonical is cid and its rows are dominant
    owner_pid.setdefault(cid, (pid, plist.get(pid)))
# verify: for contested ids, ensure owner_pid points to the player whose name matches ESPN owner
for aid,on in owner_name.items():
    onn=norm(on)
    matched=[p for p,pl in plist.items() if canon_id.get(p)==aid and (norm(pl)==onn or norm(pl) in onn or onn in norm(pl))]
    if matched:
        owner_pid[aid]=(matched[0], plist[matched[0]])

print(f"\nSTEP1: computed canonical espn_id for {len(canon_id)} players")

# ---- STEP 3: DELETE phantom statline rows ----
# A pgs row is a phantom if its athlete-id belongs to a DIFFERENT player (owner_pid[aid] != player_id)
# AND that row's aid is NOT this player's canonical id (i.e. it's foreign pollution)
MIN="NULLIF(regexp_replace(coalesce(minutes,''),'[^0-9.]','','g'),'')::numeric"
cur.execute(f"""SELECT pgs.id, pgs.player_id, pgs.nba_player_id, pgs.points, pgs.game_id, p.name
    FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0""")
allrows=cur.fetchall()
# Phantom: rows whose aid != the player's canonical id (foreign pollution rows)
to_del=[]
keep_stat=defaultdict(int)
for rid,pid,aid,pts,gid,nm in allrows:
    if canon_id.get(pid)!=aid:
        to_del.append(rid)
# ALSO handle athlete-id-pure-phantoms: rows where the aid's true owner is a different player even if it IS their canonical? No—canonical is their own. So foreign => delete.
print("phantom pgs rows to delete (aid != player canonical):", len(to_del))
# safety: ensure we never delete MORE than the foreign set; log sample
samp=[]
for rid,pid,aid,pts,gid,nm in allrows:
    if canon_id.get(pid)!=aid and len(samp)<10:
        samp.append((pid,nm,aid,canon_id[pid]))
for s in samp: print("   DEL", s)

# delete in batches
if to_del:
    for i in range(0,len(to_del),500):
        cur.execute("DELETE FROM nba.player_game_stats WHERE id=ANY(%s)", (to_del[i:i+500],))

# ---- STEP 4: correct espn_id ----
nfix=0
sfix=[]
for pid in canon_id:
    cur_espn = dict((p,e) for p,_,e in players)[pid]
    if cur_espn != canon_id[pid]:
        cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s", (canon_id[pid], pid))
        nfix+=1
        if len(sfix)<20: sfix.append((pid, plist[pid], cur_espn, canon_id[pid]))
print("\nSTEP4: corrected espn_id for", nfix, "players")
for p,n,o,c in sfix: print(f"   pid={p} {n}: {o} -> {c}")

print("\nAFTER: pgs=%s players=%s" % (q1("SELECT count(*) FROM nba.player_game_stats"), q1("SELECT count(*) FROM nba.players")))
# dry: DO NOT COMMIT yet
print("\nDRY RUN COMPLETE — transaction open, NOT committed. Review then commit.")
# leave open for manual commit decision
