"""Comprehensive espn_id audit — CONCURRENT (thread pool) for speed, live output.
For every player with espn_id>0, fetch ESPN, compare the athlete's fullName to our player name.
Lists all where espn_id points to the WRONG athlete. READ-ONLY. NO writes."""
import psycopg2, json, urllib.request, urllib.parse, time, sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.db_urls import PSYCOPG2_DATABASE_URL
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120','Accept':'application/json'}

def fetch(eid):
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{eid}?lang=en&region=us",headers=UA),timeout=10) as r:
            d=json.load(r)
        return eid, d.get('fullName','')
    except Exception as ex:
        return eid, f"ERR:{type(ex).__name__}"

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); cur=conn.cursor()
cur.execute("SELECT id, name, espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 ORDER BY id")
players=cur.fetchall()
print(f"auditing {len(players)} players concurrently...", flush=True)

# fetch all in a thread pool
results={}
with ThreadPoolExecutor(max_workers=12) as ex:
    futs={ex.submit(fetch, eid): (pid,name,eid) for pid,name,eid in players}
    for fut in as_completed(futs):
        eid, en = fut.result()
        pid,name,oid = futs[fut]
        results[eid]=(name, en)

def norm(n):
    n=(n or '').lower().replace('.','').replace("'",'')
    return n
mismatches=[]
for pid,name,oid in players:
    en = results.get(oid, ('',''))[1]
    if en.startswith('ERR'):
        continue  # skip fetch errors (can retry)
    our=norm(name); espn=norm(en)
    our_last=our.split()[-1] if our.split() else ''
    ok = (our_last and our_last in espn) or (our in espn) or (espn in our)
    if not ok:
        mismatches.append((pid,name,oid,en))

print(f"\nMISMATCHES (espn_id -> wrong athlete): {len(mismatches)}", flush=True)
for pid,n,e,en in mismatches:
    print(f"  pid={pid} {n} espn={e} -> ESPN={en}", flush=True)
with open('/tmp/espn_audit.csv','w') as f:
    for pid,n,e,en in mismatches:
        f.write(f"{pid}\t{n}\t{e}\t{en}\n")
print("\ncount by filename saved.", flush=True)
conn.close()
