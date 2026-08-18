"""Final metadata cleanup: fix espn_id column for players who have NO pgs athlete-id to derive from.
These are roster/bench players (incl. CP3, Kidd, Butler) whose espn_id column is polluted but whose
pgs rows carry NULL/zero athlete id. Verify each against ESPN by name and correct.
Uses ESPN site search by full name (name -> athlete id). WRITES only where a confident match found.
Dry-run first (--commit to write)."""
import psycopg2, json, urllib.request, urllib.parse, re, sys, time
from app.db_urls import PSYCOPG2_DATABASE_URL
UA={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120','Accept':'application/json'}
COMMIT = '--commit' in sys.argv

conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); cur=conn.cursor()
# players needing espn_id fix: have espn_id but NO positive athlete-id in their own pgs rows
cur.execute("""
  SELECT DISTINCT p.id, p.name, p.espn_id FROM nba.players p
  WHERE p.espn_id IS NOT NULL AND p.espn_id>0
    AND NOT EXISTS (SELECT 1 FROM nba.player_game_stats pgs WHERE pgs.player_id=p.id AND pgs.nba_player_id=p.espn_id)
    AND NOT EXISTS (SELECT 1 FROM nba.player_game_stats pgs WHERE pgs.player_id=p.id AND pgs.nba_player_id>0 AND pgs.nba_player_id=p.espn_id)
  ORDER BY p.name
""")
players=cur.fetchall()
print(f"{len(players)} players need espn_id resolution via ESPN name lookup")

def search_espn(name):
    q=urllib.parse.quote(name)
    for base in [f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/athletes?search={q}",
                 f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes?search={q}"]:
        try:
            with urllib.request.urlopen(urllib.request.Request(base,headers=UA),timeout=12) as r:
                d=json.load(r)
            athletes=(d.get('athletes') or d.get('results') or [])
            if athletes:
                a=athletes[0]
                return a.get('id'), a.get('displayName') or a.get('fullName')
        except Exception: pass
    return None,None

fixed=0; unsure=[]
for pid,name,cur_espn in players:
    eid,dn=search_espn(name)
    if eid:
        eid=int(eid)
        if eid!=cur_espn and COMMIT:
            cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s",(eid,pid))
        fixed+=1
        flag='' if eid==cur_espn else f" -> {eid} ({dn})"
        print(f"  {name}: {cur_espn}{flag}")
    else:
        unsure.append((pid,name,cur_espn))
        print(f"  {name}: NO match (keep {cur_espn})")
    time.sleep(0.2)

print(f"\nresolved: {fixed} | unsure/unchanged: {len(unsure)}")
if COMMIT:
    conn.commit(); print("COMMITTED")
else:
    conn.rollback(); print("dry-run (rollback) — rerun with --commit")
cur.close(); conn.close()
