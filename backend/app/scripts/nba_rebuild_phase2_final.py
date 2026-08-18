"""Phase 2 FINAL: Build the authoritative canonical decision table.
Uses ESPN-VERIFIED owner map (from phase-1c, this session) as ground truth for contested ids.
For each player_id:
  - Determine its athlete id(s) from pgs.
  - If an athlete id belongs to a DIFFERENT verified player (ESPN-confirmed), this row is a PHANTOM ->
    target = the real owner's player_id (merge into it).
  - Else the row is a real owner; its espn_id = its own verified/modal id.
Outputs reviewable CSV. NO DB writes."""

import psycopg2, json, re, csv
from app.db_urls import PSYCOPG2_DATABASE_URL

# ESPN-VERIFIED: athlete_id -> canonical player fullName (who REALLY owns that id).
# From our live ESPN verification this session. The REAL player keeps the row;
# anyone else who has this id in pgs is a phantom.
ESPN_OWNER_NAME = {
    3442:"DeAndre Jordan", 3448:"Brook Lopez", 3593:"Bojan Bogdanovic", 6430:"Jimmy Butler",
    6450:"Kawhi Leonard", 6583:"Anthony Davis", 2488653:"Mason Plumlee", 2528588:"Doug McDermott",
    2993874:"Kyle Anderson", 2999409:"Willy Hernangomez", 2999547:"Gary Harris", 3133628:"Myles Turner",
    3134916:"Jordan McLaughlin", 3135045:"Grayson Allen", 3136193:"Devin Booker", 3155526:"Dillon Brooks",
    4065732:"De'Andre Hunter", 4066457:"Austin Reaves", 4278402:"Jordan Goodwin", 4279147:"Lucas Williamson",
    4395625:"RJ Barrett", 4397136:"Saddiq Bey", 4397449:"Elijah Harkless", 4397450:"Darius Brown II",
    4397475:"AJ Green", 4431671:"Jaden McDaniels", 4432452:"Alex Antetokounmpo", 4432646:"Kennedy Chandler",
    4433133:"Jahmir Young", 4433246:"Patrick Baldwin Jr.", 4433268:"Trey Alexander", 4592857:"Isaiah Crawford",
    4594268:"Anthony Edwards", 4683774:"Bronny James", 4683834:"KJ Simpson", 4712849:"Anthony Black",
    4713010:"David Jones Garcia", 4845363:"Cam Christie", 4873138:"Ace Bailey", 4896372:"Walter Clayton Jr.",
    4897262:"Will Richard", 4900671:"Ajay Mitchell", 5061568:"Carter Bryant", 5105623:"Kel'el Ware",
    5105806:"Jett Howard", 5107199:"Daniss Jenkins", 5239561:"Jase Richardson",
    # Additional verified real-owner ids we know from the split data (own dominant, confirmed):
    # (these are the REAL owner ids for our own players)
    2393:"Shaun Livingston",2438:"Amare Stoudemire",2866:"John Lucas III",3191:"Corey Brewer",
    3194:"Wilson Chandler",3438:"George Hill",3447:"Robin Lopez",3988:"Danny Green",4239:"Evan Turner",
    4270:"Trevor Booker",4702352:"Larry Sanders",6428:"MarShon Brooks",6443:"Reggie Jackson",
    6591:"Maurice Harkless",6605:"Meyers Leonard",2490589:"Isaiah Canaan",2594922:"Otto Porter Jr.",
    2566741:"KJ McDaniels",2528393:"Tarik Black",3134881:"Stanley Johnson",2581190:"Josh Richardson",
    3907387:"Jonathon Simmons",2982340:"Justin Anderson",2528386:"Joe Young",3136477:"Isaiah Whitehead",
    3059316:"Wayne Selden",2566748:"Marshall Plumlee",3037789:"Bogdan Bogdanovic",3057187:"Sterling Brown",
    2779:"Brandon Paul",4431714:"Jamil Wilson",
}

def norm(n):
    n = (n or '').lower().replace(".","").replace("'","").replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u").replace("ñ","n").replace("ć","c").replace("š","s").replace("đ","d").replace("ž","z")
    return re.sub(r"\s+"," ", n).strip()

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

cur.execute("""
  SELECT p.id, p.name, p.nba_id, p.active,
         json_agg(json_build_object('id', pgs.nba_player_id,'n',cnt) ORDER BY cnt DESC)
  FROM nba.players p
  LEFT JOIN (SELECT player_id, nba_player_id, count(*) cnt
             FROM nba.player_game_stats WHERE nba_player_id IS NOT NULL AND nba_player_id>0
             GROUP BY player_id, nba_player_id) pgs ON pgs.player_id=p.id
  GROUP BY p.id, p.name, p.nba_id, p.active
  ORDER BY p.id
""")
players = cur.fetchall()

decisions = []  # (player_id, name, canon_espn, action, target_pid_or_name, note)
for pid, name, nba_id, active, dist in players:
    dist = dist or []
    ids = [d['id'] for d in dist]
    if not ids:
        # no athlete id at all
        decisions.append((pid, name, None, "no-id", None, f"nba_id={nba_id}, active={active}"))
        continue
    # Are any of this player's ids owned by a DIFFERENT verified player (by name)?
    real_hits = []
    for d in dist:
        owner = ESPN_OWNER_NAME.get(d['id'])
        if owner and norm(owner) != norm(name):
            real_hits.append((d['id'], owner, d['n'], dist[0]['id']))
    if real_hits:
        # This player has a phantom id belonging to someone else.
        # The phantom id is the LEAST representative (minority usually). If a phantom id is present
        # AND there's another id, decide by which id is this player's own.
        # Rule: player keeps the id that ISN'T definitively another verified player's (unless that's their only id).
        alt_ids = [d['id'] for d in dist if not (ESPN_OWNER_NAME.get(d['id']) and norm(ESPN_OWNER_NAME.get(d['id'])) != norm(name))]
        if len(alt_ids) == 1:
            canon = alt_ids[0]
            decisions.append((pid, name, canon, "keep", None, f"own-id {canon}; phantom {real_hits}"))
        elif len(alt_ids) > 1:
            canon = max(dist, key=lambda d: d['n'] if d['id'] in alt_ids else 0)['id']
            decisions.append((pid, name, canon, "keep-multi", None, f"own-candidates {alt_ids}; phantom {real_hits}"))
        else:
            # ALL ids belong to other verified players -> this is a pure phantom, merge to owner
            owner_pid = None
            oname = real_hits[0][1]
            for pid2, n2, nb2, ac2, d2 in players:
                if pid2 != pid and norm(n2) == norm(oname):
                    owner_pid = pid2; break
            decisions.append((pid, name, real_hits[0][0], "MERGE", owner_pid, f"all ids belong to {oname} (pid {owner_pid})"))
    else:
        # All this player's ids are either unlisted or its own -> keep with dominant id
        canon = dist[0]['id']
        decisions.append((pid, name, canon, "keep", None, f"dominant {canon}"))

# Write CSV
with open('/tmp/nba_player_canon_map.csv','w',newline='') as f:
    w = csv.writer(f)
    w.writerow(["player_id","name","canon_espn_id","action","merge_to_pid","note"])
    for r in decisions:
        w.writerow([r[0],r[1],r[2],r[3],r[4],r[5]])

# Print the import decisions: MERGE + no-id + keep-with-phantom
print("=== MERGE decisions (phantoms to fold into real owner) ===")
for r in decisions:
    if r[3]=="MERGE":
        print(f"  MERGE pid={r[0]} ({r[1]}) -> pid={r[4]} espn={r[2]} | {r[5]}")
print(f"\nTotal: {len(decisions)} players | merges: {sum(1 for r in decisions if r[3]=='MERGE')} | keep: {sum(1 for r in decisions if r[3].startswith('keep'))} | no-id: {sum(1 for r in decisions if r[3]=='no-id')}")

print("\n=== keep-with-phantom note (real owner, but also has a stray foreign id) ===")
for r in decisions:
    if r[3].startswith("keep") and "phantom" in r[5]:
        print(f"  pid={r[0]} ({r[1]}) canon={r[2]}: {r[5]}")

print("\n=== no-id players ===")
for r in decisions:
    if r[3]=="no-id":
        print(f"  pid={r[0]} ({r[1]}) nba={r[2]} | {r[5]}")
cur.close(); conn.close()
