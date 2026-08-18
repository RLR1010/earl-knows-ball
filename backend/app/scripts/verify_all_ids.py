"""Verify EVERY distinct athlete id referenced by the 81 multi-id players against ESPN.
Builds an authoritative id->owner map so each player's true espn_id is confirmed.
Outputs /tmp/all_id_owners.json + prints a reviewable table. NO DB writes."""
import psycopg2, json, urllib.request, time
from app.db_urls import PSYCOPG2_DATABASE_URL
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

UA = {'User-Agent':'Mozilla/5.0','Accept':'application/json'}

cur.execute("""SELECT pgs.player_id, p.name, array_agg(DISTINCT pgs.nba_player_id) 
    FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, p.name
    HAVING count(DISTINCT pgs.nba_player_id)>1 ORDER BY pgs.player_id""")
multi = cur.fetchall()
print(f"{len(multi)} multi-id players. Collecting all distinct ids...")
ids_all = set()
for pid, name, ids in multi:
    for i in ids: ids_all.add(i)
print(f"distinct athlete ids to verify: {len(ids_all)}")

owners = {}
for i in sorted(ids_all):
    url = f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{i}?lang=en&region=us"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=12) as r:
            d = json.load(r)
        owners[i] = {'name': d.get('fullName'), 'active': d.get('active')}
    except Exception as e:
        owners[i] = {'name': f'ERR:{e}', 'active': None}
    time.sleep(0.25)

print("\n=== id -> ESPN owner ===")
for i in sorted(owners):
    o = owners[i]
    print(f"  {i}: {o['name']} {'' if o['active'] is None else ('[ACTIVE]' if o['active'] else '[retired]')}")

with open('/tmp/all_id_owners.json','w') as f:
    json.dump({str(k):v for k,v in owners.items()}, f, indent=2)
print("\nSaved /tmp/all_id_owners.json")
cur.close(); conn.close()
