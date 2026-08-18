"""Phase 2: Resolve canonical espn_id for EVERY player, accuracy-first.
Strategy per player:
  1. If player has exactly ONE distinct pgs athlete id -> that IS their real espn_id (unpolluted).
  2. If player has MULTIPLE pgs athlete ids (split) -> the polluted ones came from phantom merges.
     Determine the CORRECT id by: the id that does NOT belong to another player we can identify.
     For split players, pick the id that is ALSO the player's own distinct id (minority often correct)
     OR resolve via ESPN name lookup. Fall back to ESPN API name->id.
Produces a reviewable mapping CSV at /tmp/nba_player_espn_map.csv (NO writes to DB)."""

import psycopg2, json, urllib.request, time, re, csv
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import Counter

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120', 'Accept': 'application/json'}

def norm(n):
    n = (n or '').lower().replace(".","").replace("'","").replace("á","a").replace("é","e").replace("í","i").replace("ó","o").replace("ú","u").replace("ñ","n").replace("ć","c").replace("Č","c").replace("š","s").replace("đ","d").replace("ž","z")
    return re.sub(r"\s+"," ", n).strip()

def espn_search(name):
    """Try ESPN athlete by known id patterns; we can't enumerate, but we CAN verify specific ids.
    For name-based resolution we rely on the player's own pgs id + ESPN verification of that id."""
    return None

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

# All players with pgs athlete-id distribution (s26-35)
cur.execute("""
  WITH per AS (
    SELECT pgs.player_id, pgs.nba_player_id, count(*) n
    FROM nba.player_game_stats pgs JOIN nba.games g ON g.id=pgs.game_id
    WHERE g.season_id BETWEEN 26 AND 35 AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id)
  SELECT p.player_id, p2.name,
         json_agg(json_build_object('id', p.nba_player_id, 'n', p.n) ORDER BY p.n DESC),
         sum(p.n) AS total
  FROM per p JOIN nba.players p2 ON p2.id=p.player_id
  GROUP BY p.player_id, p2.name
  ORDER BY p.player_id
""")
players = cur.fetchall()

# Load all player names keyed by athlete id so we can detect which id belongs to whom
cur.execute("""SELECT p.id, p.name, p.nba_id FROM nba.players p""")
all_players = cur.fetchall()
# athlete id -> set of player ids that reference it (from pgs), built below
athlete_to_pids = {}
for pid, name, dist, total in players:
    for d in dist:
        athlete_to_pids.setdefault(d['id'], set()).add(pid)

# For each player pick canonical espn_id
rows_out = []
print("Resolving canonical espn_id for", len(players), "players...")
for pid, name, dist, total in players:
    dist = sorted(dist, key=lambda d: -d['n'])
    candidates = [d['id'] for d in dist]
    if len(candidates) == 1:
        canon = candidates[0]
        method = "only-id"
    else:
        # split. The correct id is the one NOT shared by a MORE-certain other player,
        # i.e. the id whose set of referencing pids is smallest (least pollution) OR unique to this player.
        # Prefer the id referenced by the fewest players (most specific to this one).
        least_shared = min(candidates, key=lambda a: len(athlete_to_pids.get(a, [])))
        # If an id is unique to this player, it's certainly theirs.
        canon = least_shared
        method = "split-least-shared"
        # But if the majority id is ALSO unique to this player, majority is fine (no phantom pollution)
        if len(athlete_to_pids.get(candidates[0], [])) == 1:
            canon = candidates[0]
            method = "split-majority-unique"
    rows_out.append((pid, name, canon, method, json.dumps(dist), total))

# Write reviewable CSV
with open('/tmp/nba_player_espn_map.csv','w',newline='') as f:
    w = csv.writer(f)
    w.writerow(["player_id","name","canon_espn_id","method","id_distribution","total_rows"])
    for r in rows_out:
        w.writerow(r)
print("Wrote /tmp/nba_player_espn_map.csv with", len(rows_out), "rows")

# Summarize the split players and their chosen canon
print("\n=== SPLIT player decisions (the accuracy-critical ones) ===")
for pid, name, canon, method, dist, total in rows_out:
    if method.startswith("split"):
        print(f"  pid={pid} {name}: canon={canon} [{method}] dist={dist}")
cur.close(); conn.close()
