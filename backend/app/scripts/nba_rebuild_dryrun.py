"""Rebuild: DELETE phantom pgs rows + correct nba.players.espn_id.
Phantom row = exact-statline duplicate (same game, team, points, minutes, reb, ast)
that appears under TWO+ DIFFERENT player_ids carrying the SAME athlete_id.
Within each such group we keep the row whose player_id is the TRUE owner of that
athlete_id (the player whose dominant/real espn_id == that athlete id). Others are phantom copies.
Also computes corrected espn_id per player (their own dominant athlete id) and flags merges.

Mode: DRY_RUN only (no writes). Prints counts for verification."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

# 1) True owner of each athlete_id = the player whose pgs athlete_id matches and is most numerous
#    For each athlete id, find the player_id(s) whose OWN id distribution includes it.
cur.execute("""
  WITH pd AS (
    SELECT pgs.player_id, pgs.nba_player_id AS aid, count(*) n
    FROM nba.player_game_stats pgs
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id)
  SELECT aid, player_id, n FROM pd
""")
rows = cur.fetchall()
owner_n = defaultdict(dict)  # aid -> {player_id: n}
for aid, pid, n in rows:
    owner_n[aid][pid] = n
# dominant player (by count) owner
owner = {}
for aid, pmap in owner_n.items():
    owner[aid] = max(pmap, key=pmap.get)

# 2) Find phantom pgs rows (duplicate statlines under different player_ids)
MINUTES = "NULLIF(regexp_replace(coalesce(pgs.minutes,''),'[^0-9.]','','g'),'')::numeric"
cur.execute(f"""
  WITH dup AS (
    SELECT pgs.game_id, pgs.team_id, pgs.points AS pts, {MINUTES} AS mins,
           pgs.rebounds_total AS reb, pgs.assists AS ast, pgs.nba_player_id AS aid,
           count(*) n, count(DISTINCT pgs.player_id) np
    FROM nba.player_game_stats pgs
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.game_id, pgs.team_id, pgs.points, {MINUTES}, pgs.rebounds_total, pgs.assists, pgs.nba_player_id
    HAVING count(*)>1 AND count(DISTINCT pgs.player_id)>1)
  SELECT d.aid, d.game_id, pgs.id, pgs.player_id, p.name, pgs.points, {MINUTES}
  FROM dup d
  JOIN nba.player_game_stats pgs ON pgs.game_id=d.game_id AND pgs.team_id=d.team_id
     AND pgs.points=d.pts AND pgs.assists=d.ast AND pgs.rebounds_total=d.reb
     AND pgs.nba_player_id=d.aid
     AND NULLIF(regexp_replace(coalesce(pgs.minutes,''),'[^0-9.]','','g'),'')::numeric=d.mins
  JOIN nba.players p ON p.id=pgs.player_id
""")
rows = cur.fetchall()
print(f"Total pgs rows in duplicate groups (all candidates): {len(rows)}")

# Keep the TRUE-owner row; mark others
from collections import Counter
to_delete = []
kept = 0
for aid, game_id, rid, pid, name, pts, mins in rows:
    true_owner = owner.get(aid)
    if pid == true_owner:
        kept += 1
    else:
        to_delete.append((rid, pid, name, aid, game_id, pts, mins))
print(f"TRUE-OWNER rows kept: {kept}")
print(f"PHANTOM rows to DELETE: {len(to_delete)}")

# Distinct phantom rows (unique ids)
phantom_ids = set(r[0] for r in to_delete)
print(f"Distinct phantom pgs row ids to delete: {len(phantom_ids)}")

# 3) Preview phantom deletes with their true owners
print("\n=== Preview phantom deletes (first 30) ===")
from collections import defaultdict as dd
for rid, pid, name, aid, game_id, pts, mins in to_delete[:30]:
    to = owner.get(aid)
    print(f"  DEL pgs id={rid} player_id={pid} ({name}) aid={aid} -> true owner pid={to} game={game_id} pts={pts}")

# 4) Corrected espn_id per player (own dominant athlete id) + merge candidates
print("\n=== Corrected nba.players.espn_id (players whose current espn_id != true dominant) ===")
cur.execute("""SELECT id, name, espn_id, nba_id FROM nba.players ORDER BY id""")
players = cur.fetchall()
fixes = []
for pid, name, cur_espn, nba in players:
    pmap = owner_n.get(pid if False else None, {})
    # own ids
    own_ids = {a:n for a, pm in owner_n.items() if pid in pm for a, mm in [(a, pm[pid])]}
    if own_ids:
        true_id = max(own_ids, key=own_ids.get)
        if cur_espn != true_id:
            fixes.append((pid, name, cur_espn, true_id))
print(f"players needing espn_id correction: {len(fixes)}")
for pid, name, cur, new in fixes[:40]:
    print(f"  pid={pid} {name}: {cur} -> {new}")

conn.close()
print("\nDRY RUN COMPLETE (no writes).")
