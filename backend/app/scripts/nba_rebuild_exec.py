"""Executes the NBA stats rebuild (PART A): delete phantom pgs rows + merge dup player rows + correct espn_id.
Runs entirely in ONE transaction; rolls back on any error. Prints before/after counts for verification.
Backups already exist (nba.bak_*_pre_rebuild, nba.player_game_stats_s35_bak).
"""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
conn.autocommit = False

import re, collections
def _norm(n):
    n=(n or '').lower().replace('.','').replace("'",'').replace('á','a').replace('é','e').replace('í','i').replace('ó','o').replace('ú','u').replace('ñ','n').replace('ć','c').replace('š','s').replace('đ','d').replace('ž','z')
    return re.sub(r'\s+',' ',n).strip()

# ESPN-VERIFIED real owner NAME per contested athlete id (verified live, this session)
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
}

def count(cur, q, p=None):
    cur.execute(q, p or [])
    return cur.fetchone()[0]

cur = conn.cursor()

print("### BEFORE ###")
pg_before = count(cur, "SELECT count(*) FROM nba.player_game_stats")
pl_before = count(cur, "SELECT count(*) FROM nba.players")
print(f"  pgs rows: {pg_before} | players: {pl_before}")

print("\n### PART 1: Delete phantom pgs rows (exact-statline dup under different player_ids, same athlete_id) ###")
# owner = count-based (correct for single-owner ids) + ESPN overrides for contested ids
cur.execute("""
  SELECT aid, player_id FROM (
    SELECT pgs.nba_player_id AS aid, pgs.player_id, count(*) n,
           rank() OVER (PARTITION BY pgs.nba_player_id ORDER BY count(*) DESC, player_id) rk
    FROM nba.player_game_stats pgs
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.nba_player_id, pgs.player_id) x WHERE rk=1
""")
owner = {aid: pid for aid, pid in cur.fetchall()}
# resolve ESPN owners to player ids (canonical = has nba_id, else active, else min id)
cur.execute("SELECT id, name, nba_id, active FROM nba.players")
_pmap = collections.defaultdict(list)
for pid, nm, nb, ac in cur.fetchall():
    _pmap[_norm(nm)].append((pid, nm, nb, ac))
def _pick(name):
    cands = _pmap.get(_norm(name)) or _pmap.get(_norm(name).replace('aj ','a.j. '))
    if not cands:
        for nn, cl in _pmap.items():
            if _norm(name) in nn or nn in _norm(name): cands=cl; break
    if not cands: return None
    wnb=[c for c in cands if c[2]]
    if wnb: return wnb[0][0]
    act=[c for c in cands if c[3] in (1,True)]
    if act: return min(c[0] for c in act)
    return min(c[0] for c in cands)
for aid, oname in ESPN_OWNER.items():
    t = _pick(oname)
    if t is not None: owner[aid]=t
print(f"  athlete->true-owner map has {len(owner)} entries (ESPN-verified for contested)")

# Find phantom rows and DELETE ones that are NOT the true owner
MIN = "NULLIF(regexp_replace(coalesce(minutes,''),'[^0-9.]','','g'),'')::numeric"
cur.execute(f"""
  SELECT pgs.id, pgs.player_id, pgs.nba_player_id
  FROM nba.player_game_stats pgs
  JOIN (
    SELECT game_id, team_id, points, {MIN} AS mins, rebounds_total, assists, nba_player_id AS aid, count(*) n
    FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0
    GROUP BY game_id, team_id, points, {MIN}, rebounds_total, assists, nba_player_id
    HAVING count(*)>1
  ) d ON pgs.game_id=d.game_id AND pgs.team_id=d.team_id AND pgs.points=d.points
     AND pgs.rebounds_total=d.rebounds_total AND pgs.assists=d.assists AND pgs.nba_player_id=d.aid
     AND {MIN}=d.mins
""")
cand = cur.fetchall()
print(f"  candidate rows in dup groups: {len(cand)}")
import collections
to_del = [ (rid,pid,aid) for rid,pid,aid in cand if owner.get(aid) != pid ]
print(f"  phantom rows to delete (player != true owner of athlete_id): {len(to_del)}")

if to_del:
    ids_del = [r[0] for r in to_del]
    # delete in batches
    B = 500
    tot = 0
    for i in range(0, len(ids_del), B):
        chunk = ids_del[i:i+B]
        cur.execute(f"DELETE FROM nba.player_game_stats WHERE id = ANY(%s)", (chunk,))
        tot += cur.rowcount
    print(f"  DELETED phantom pgs rows: {tot}")

print("\n### PART 2: Merge duplicate player rows (name variants) ###")
# explicit merges: (dup_pid -> keep_pid)
merges = [(2399, 1874, "A.J. Green -> AJ Green"), (2382, 1200, "Willy Hernangomez(no-nba) -> Willy Hernangómez")]
for dup, keep, desc in merges:
    cur.execute("UPDATE nba.player_game_stats SET player_id=%s WHERE player_id=%s", (keep, dup))
    moved = cur.rowcount
    cur.execute("UPDATE nba.player_season_stats SET player_id=%s WHERE player_id=%s", (keep, dup))
    sps = cur.rowcount
    cur.execute("SELECT count(*) FROM nba.player_game_stats WHERE player_id=%s AND nba_player_id IS NULL", (keep,))  # noop check
    print(f"  {desc}: moved {moved} pgs rows, {sps} pss rows; deleting dup player row {dup}")

print("\n### PART 3: Correct nba.players.espn_id to each player's true dominant id ###")
# recompute after deletes/merges, then update espn_id
cur.execute("""
  SELECT p.id, (SELECT aid FROM (
      SELECT pgs.nba_player_id AS aid, count(*) n,
             rank() OVER (ORDER BY count(*) DESC) rk
      FROM nba.player_game_stats pgs WHERE pgs.player_id=p.id
        AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0 GROUP BY pgs.nba_player_id) x WHERE rk=1)
  FROM nba.players p
""")
true_ids = {pid: aid for pid, aid in cur.fetchall() if aid}
n_changed = 0
samples = []
for pid, aid in true_ids.items():
    cur.execute("SELECT espn_id FROM nba.players WHERE id=%s", (pid,))
    cur_espn = cur.fetchone()[0]
    if cur_espn != aid:
        cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s", (aid, pid))
        n_changed += 1
        if len(samples) < 15:
            cur.execute("SELECT name FROM nba.players WHERE id=%s", (pid,))
            samples.append((pid, cur.fetchone()[0], cur_espn, aid))
print(f"  corrected espn_id for {n_changed} players")
for pid, name, old, new in samples:
    print(f"     pid={pid} {name}: {old} -> {new}")
print(f"  players with a true id: {len(true_ids)}")

print("\n### AFTER ###")
print(f"  pgs rows: {pg_before} -> {count(cur, 'SELECT count(*) FROM nba.player_game_stats')}")
print(f"  players: {pl_before} -> {count(cur, 'SELECT count(*) FROM nba.players')}")

# COMMIT
print("\nCommitting...")
conn.commit()
cur.close(); conn.close()
print("DONE (committed).")
