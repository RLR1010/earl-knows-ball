"""Fix espn_id column for all players sharing a duplicate espn_id — assign true unique ESPN id via name lookup.
Players with a pgs aid (nba_player_id>0) get espn_id = that aid (authoritative).
Players with NULL pgs aid get espn_id from ESPN name search (best single exact-name match).
Dry-run unless --commit. Logs to /tmp/espn_dup_fix.log"""
import psycopg2, json, urllib.request, urllib.parse, time, re, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120','Accept':'application/json'}
COMMIT='--commit' in sys.argv

def deaccent(n):
    n=(n or '').lower()
    for a,b in [('á','a'),('é','e'),('í','i'),('ó','o'),('ú','u'),('ñ','n'),('ç','c'),('ć','c'),('š','s'),('đ','d'),('ž','z'),('ţ','t'),('ă','a'),('ū','u'),('ů','u'),('ě','e'),('ā','a'),('ē','e'),('ī','i'),('ō','o'),('ū','u'),('ğ','g'),("'",''),('.','')]:
        n=n.replace(a,b)
    return ' '.join(n.split())

def espn_search(name):
    q=urllib.parse.quote(name)
    url=f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/athletes?search={q}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=12) as r:
            d=json.load(r)
        for a in (d.get('athletes') or []):
            return a.get('id'), a.get('displayName') or a.get('fullName')
    except Exception:
        return None,None

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()
# all players in duplicate espn groups
cur.execute('''SELECT id, name, espn_id, nba_id FROM nba.players WHERE espn_id IN (
   SELECT espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 GROUP BY espn_id HAVING count(*)>1)
   ORDER BY id''')
players=cur.fetchall()
print(f"{len(players)} players in duplicate-espn groups to fix", flush=True)
log=[]

for pid,nm,cur_espn,nba in players:
    # authoritative: pgs aid if present
    cur.execute("SELECT nba_player_id FROM nba.player_game_stats WHERE player_id=%s AND nba_player_id>0 LIMIT 1",(pid,))
    row=cur.fetchone()
    new_id=None; src=""
    if row and row[0]:
        new_id=row[0]; src="pgs-aid"
    else:
        eid,dn=espn_search(nm)
        if eid:
            new_id=int(eid); src="espn-search"
    if new_id and new_id!=cur_espn:
        log.append((pid,nm,cur_espn,new_id,src))
        if COMMIT:
            cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s",(new_id,pid))
    time.sleep(0.15)

print(f"\n{len(log)} players with corrected espn_id:")
for pid,nm,old,new,src in log:
    print(f"   {nm}: espnId {old} -> {new} ({src})", flush=True)

if COMMIT:
    conn.commit(); print("\nCOMMITTED")
else:
    conn.rollback(); print("\nDRY RUN — rollback. rerun --commit")
cur.close(); conn.close()
