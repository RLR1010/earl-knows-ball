"""Comprehensive audit: for EVERY player with an espn_id, fetch ESPN and compare name.
Finds ALL players whose espn_id points to the WRONG athlete (name mismatch vs our player name).
This catches the Harden->3967(DerrickBrown), Nash->2799(LouWilliams), KG->2214(RonaldDupree) class.
READ-ONLY audit. Output /tmp/espn_audit.csv. NO writes."""
import psycopg2, json, urllib.request, time
from app.db_urls import PSYCOPG2_DATABASE_URL
UA={'User-Agent':'Mozilla/5.0','Accept':'application/json'}

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); cur=conn.cursor()
cur.execute("SELECT id, name, espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 ORDER BY id")
players=cur.fetchall()
print(f"auditing {len(players)} players' espn_id against ESPN...")

mismatches=[]
for pid,name,eid in players:
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{eid}?lang=en&region=us",headers=UA),timeout=12) as r:
            d=json.load(r)
        en=d.get('fullName','')
    except Exception as ex:
        en=f"ERR"
    # simple last-name + first-initial containment check
    our=name.lower().replace('.','')
    espn=en.lower().replace('.','')
    our_last=our.split()[-1] if our.split() else ''
    ok = (our_last and our_last in espn) or (our in espn) or (espn in our)
    if not ok:
        mismatches.append((pid,name,eid,en))
    time.sleep(0.15)

print(f"\nMISMATCHES (espn_id -> wrong athlete): {len(mismatches)}")
for pid,n,e,en in mismatches:
    print(f"  pid={pid} {n} espn={e} -> ESPN={en}")
# save
with open('/tmp/espn_audit.csv','w') as f:
    for pid,n,e,en in mismatches:
        f.write(f"{pid}\t{n}\t{e}\t{en}\n")
conn.close()
