"""Pre-flight v2: Correct owner map using ESPN-VERIFIED ownership for contested ids.
Count-based owner for clean ids + ESPN-confirmed real owner for the 47 contested groups.
This fixes the 'Austin Reaves deleted' bug: the REAL owner is kept, phantoms are deleted."""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

def norm(n):
    import re
    n=(n or '').lower().replace('.','').replace("'",'').replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').replace('ñ','n').replace('ć','c').replace('š','s').replace('đ','d').replace('ž','z')
    return re.sub(r'\s+',' ',n).strip()

# ESPN-VERIFIED real owner NAME per contested athlete id (from live API, this session)
ESPN_OWNER = {
 3442:"DeAndre Jordan",3448:"Brook Lopez",3593:"Bojan Bogdanovic",6430:"Jimmy Butler",6450:"Kawhi Leonard",
 6583:"Anthony Davis",2488653:"Mason Plumlee",2528588:"Doug McDermott",2993874:"Kyle Anderson",2999409:"Willy Hernangomez",
 2999547:"Gary Harris",3133628:"Myles Turner",3134916:"Jordan McLaughlin",3135045:"Grayson Allen",3136193:"Devin Booker",
 3155526:"Dillon Brooks",4065732:"De'Andre Hunter",4066457:"Austin Reaves",4278402:"Jordan Goodwin",4279147:"Lucas Williamson",
 4395625:"RJ Barrett",4397136:"Saddiq Bey",4397449:"Elijah Harkless",4397450:"Darius Brown II",4397475:"AJ Green",
 4431671:"Jaden McDaniels",4432452:"Alex Antetokounmpo",4432646:"Kennedy Chandler",4433133:"Jahmir Young",
 4433246:"Patrick Baldwin Jr.",4433268:"Trey Alexander",4592857:"Isaiah Crawford",4594268:"Anthony Edwards",
 4683774:"Bronny James",4683834:"KJ Simpson",4712849:"Anthony Black",4713010:"David Jones Garcia",4845363:"Cam Christie",
 4873138:"Ace Bailey",4896372:"Walter Clayton Jr.",4897262:"Will Richard",4900671:"Ajay Mitchell",5061568:"Carter Bryant",
 5105623:"Kel'el Ware",5105806:"Jett Howard",5107199:"Daniss Jenkins",5239561:"Jase Richardson",
 # real OWN ids for our own players where phase-1c confirmed (already-dominant, single-player -> covered by count). 
}

# Count-based owner for ALL athlete ids (correct for single-owner ids)
cur.execute("""SELECT aid, player_id FROM (
  SELECT pgs.nba_player_id AS aid, pgs.player_id, count(*) n,
         rank() OVER (PARTITION BY pgs.nba_player_id ORDER BY count(*) DESC, player_id) rk
  FROM nba.player_game_stats pgs WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
  GROUP BY pgs.nba_player_id, pgs.player_id) x WHERE rk=1""")
owner = {a:p for a,p in cur.fetchall()}

# Override contested ids with ESPN-verified owner: find the player_id whose name matches ESPN owner
cur.execute("SELECT id, name, nba_id, active FROM nba.players")
players = cur.fetchall()
# Deterministic: among same-name players, prefer the one with nba_id (canonical), then active, then lower id
import collections
by_norm = collections.defaultdict(list)
for pid, nm, nba, act in players:
    by_norm[norm(nm)].append((pid, nm, nba, act))

def pick_canonical(name):
    cands = by_norm.get(norm(name)) or by_norm.get(norm(name).replace('aj ','a.j. '))
    if not cands:
        # containment scan
        for nn, cl in by_norm.items():
            if norm(name) in nn or nn in norm(name):
                cands = cl; break
    if not cands:
        return None
    # canonical = first with nba_id, else first active, else lowest id (prefer REAL, not auto-created no-nba_id row)
    with_nba = [c for c in cands if c[2]]
    if with_nba: return with_nba[0][0]
    active = [c for c in cands if c[3] in (1, True)]
    if active: return min(c[0] for c in active)
    return min(c[0] for c in cands)

overridden = 0
for aid, owner_name in ESPN_OWNER.items():
    target = pick_canonical(owner_name)
    if target is not None and owner.get(aid) != target:
        if aid in (4397475, 4066457, 6450, 6583, 3136193, 3133628, 4594268):
            print(f"  override aid={aid}: count-owner={owner.get(aid)} -> ESPN-owner={target} ({owner_name})")
        overridden += 1
        owner[aid] = target

print(f"overridden {overridden} contested ids with ESPN-verified owner")

# Now compute phantom deletes with corrected owner
MIN = "NULLIF(regexp_replace(coalesce(minutes,''),'[^0-9.]','','g'),'')::numeric"
cur.execute(f"""
  SELECT pgs.id, pgs.player_id, p.name, pgs.nba_player_id, pgs.game_id, pgs.points, {MIN}
  FROM nba.player_game_stats pgs JOIN nba.players p ON p.id=pgs.player_id
  JOIN (SELECT game_id, team_id, points, {MIN} AS mins, rebounds_total, assists, nba_player_id AS aid
        FROM nba.player_game_stats WHERE nba_player_id IS NOT NULL AND nba_player_id>0
        GROUP BY game_id, team_id, points, {MIN}, rebounds_total, assists, nba_player_id
        HAVING count(*)>1) d
    ON pgs.game_id=d.game_id AND pgs.points=d.points AND pgs.rebounds_total=d.rebounds_total
     AND pgs.assists=d.assists AND pgs.nba_player_id=d.aid AND {MIN}=d.mins
""")
rows = cur.fetchall()
to_del = [r for r in rows if owner.get(r[3]) != r[1]]
print(f"total candidate dup rows: {len(rows)} | would DELETE (corrected owner): {len(to_del)}")

print("\n=== Verification deletes for known groups (should now DELETE only phantoms, KEEP real players) ===")
wanted = {6450,4066457,4397475,3448,6583,4594268,3133628,4397136}
shown=set()
for rid,pid,name,aid,gid,pts,mins in to_del:
    if aid in wanted and (aid,pid,gid) not in shown:
        shown.add((aid,pid,gid))
        print(f"  DEL pgs={rid} pid={pid} ({name}) aid={aid} owner={owner.get(aid)} game={gid} pts={pts}")

print("\n=== Confirm real players are KEPT (not deleted) ===")
# (pid, expected_name, aid) — the real owner should equal this pid
kept_check = [(1737,'Austin Reaves',4066457),(812,'Kawhi Leonard',6450),(1874,'AJ Green',4397475),(887,'Anthony Davis',6583),(1623,'Anthony Edwards',4594268),(1127,'Myles Turner',3133628),(1624,'Saddiq Bey',4397136),(619,'Brook Lopez',3448)]
for pid, nm, aid in kept_check:
    ok = 'OK-KEPT' if owner.get(aid)==pid else 'CHECK!'
    print(f"   owner[{aid}] = {owner.get(aid)} ({nm}) -> {ok} {'' if owner.get(aid)==pid else '(should be ' + str(pid) + ')'}")
conn.close()
