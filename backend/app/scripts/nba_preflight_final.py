"""Final pre-flight using the COMPLETE espn-verified owner map.
For each player, ESPN's owner-of-their-name determines the canonical espn_id.
Prints decisions for accuracy review. NO WRITES."""
import psycopg2, json, re
from app.db_urls import PSYCOPG2_DATABASE_URL

def norm(n):
    n=(n or '').lower().replace('.','').replace("'",'').replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').replace('ñ','n').replace('ć','c').replace('š','s').replace('đ','d').replace('ž','z')
    return re.sub(r'\s+',' ',n).strip()

with open('/tmp/all_id_owners.json') as f:
    owners = json.load(f)
owner_name = {int(k): v['name'] for k, v in owners.items()}

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()
cur.execute("""SELECT pgs.player_id, p.name, array_agg(DISTINCT pgs.nba_player_id ORDER BY pgs.nba_player_id)
    FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, p.name
    HAVING count(DISTINCT pgs.nba_player_id)>1 ORDER BY pgs.player_id""")
multi = cur.fetchall()

print(f"{len(multi)} multi-id players. For each: which id ESPN says belongs to THEIR name?")
print("="*90)
for pid, name, ids in multi:
    nn = norm(name)
    # among this player's ids, find the ESPN owner matching this player's name
    matches = [i for i in ids if norm(owner_name.get(i,''))==nn or nn in norm(owner_name.get(i,'')) or norm(owner_name.get(i,'')) in nn]
    ids_str = ", ".join(f"{i}({owner_name.get(i,'?')})" for i in ids)
    if len(matches)==1:
        print(f"  pid={pid} {name}: canonical={matches[0]} | ids:[{ids_str}]")
    elif len(matches)>1:
        print(f"  pid={pid} {name}: **AMBIGUOUS multiple matches {matches}** | ids:[{ids_str}]")
    else:
        # no id matches name -> pick the id whose owner name is closest
        # default to the id whose ESPN owner shares the LAST NAME
        last = nn.split()[-1] if nn.split() else ''
        lm = [i for i in ids if last and last in norm(owner_name.get(i,''))]
        pick = lm[0] if len(lm)==1 else (ids[0])
        print(f"  pid={pid} {name}: NO-exact; pick={pick}({owner_name.get(pick)}) | ids:[{ids_str}]")
cur.close(); conn.close()
