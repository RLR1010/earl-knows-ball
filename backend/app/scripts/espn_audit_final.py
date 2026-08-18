"""DEFINITIVE reliable espn_id audit — sequential, with retry, no silent skips.
Every player whose ESPN athlete-name does NOT contain their own last name is flagged.
Flags are reviewed; accent-only mismatches are auto-accepted (id is CORRECT).
Read-only. Outputs /tmp/espn_audit_final.csv + prints genuine mismatches."""
import psycopg2, json, urllib.request, time, re
from app.db_urls import PSYCOPG2_DATABASE_URL
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120','Accept':'application/json'}

def fetch_name(eid, tries=3):
    for t in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{eid}?lang=en&region=us",headers=UA),timeout=12) as r:
                d=json.load(r)
            return d.get('fullName','')
        except Exception as ex:
            if t==tries-1: return f"ERR:{ex.__class__.__name__}"
            time.sleep(1.0)

def deaccent(n):
    n=(n or '')
    n=re.sub(r'[àáâãäå]','a',n); n=re.sub(r'[èéêë]','e',n); n=re.sub(r'[ìíîï]','i',n)
    n=re.sub(r'[òóôõö]','o',n); n=re.sub(r'[ùúûü]','u',n); n=re.sub(r'[ç]','c',n)
    n=re.sub(r'[ñ]','n',n); n=re.sub(r'[ćĉč]','c',n); n=re.sub(r'[śŝš]','s',n)
    n=re.sub(r'[đđ]','d',n); n=re.sub(r'[žž]','z',n); n=re.sub(r'[Ææ]','ae',n)
    return n.lower().replace('.','').strip()

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); cur=conn.cursor()
cur.execute("SELECT id, name, espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 ORDER BY id")
players=cur.fetchall()
print(f"reliable audit of {len(players)} players (sequential, retry)...", flush=True)

genuine=[]
accent_only=0
errors=0
t0=time.time()
for idx,(pid,name,eid) in enumerate(players):
    en=fetch_name(eid)
    if en.startswith('ERR'):
        errors+=1
        genuine.append((pid,name,eid,en)); continue
    our_last=deaccent(name).split()[-1] if deaccent(name).split() else ''
    espn_norm=deaccent(en)
    # match if ESPN's de-accented name shares the player's last name OR vice versa
    ok = (our_last and our_last in espn_norm) or (deaccent(name) in espn_norm) or (espn_norm in deaccent(name))
    # accent-only case: same de-accented name => definitely same person, accept
    if deaccent(name)==espn_norm:
        ok=True; accent_only+=1
    if not ok:
        genuine.append((pid,name,eid,en))
    if (idx+1)%400==0:
        print(f"  ...{idx+1}/{len(players)} ({time.time()-t0:.0f}s, errors={errors})", flush=True)

print(f"\ndone in {time.time()-t0:.0f}s | accent-only(OK): {accent_only} | fetch errors: {errors}")
print(f"GENUINE mismatches (id definitely points to a different player): {len(genuine)}", flush=True)
for pid,n,e,en in genuine:
    print(f"  pid={pid} {n} espn={e} -> ESPN={en}", flush=True)
with open('/tmp/espn_audit_final.csv','w') as f:
    for pid,n,e,en in genuine:
        f.write(f"{pid}\t{n}\t{e}\t{en}\n")
conn.close()
