"""Phase 1: Analyze nba.players vs pgs.nba_player_id to derive canonical espn_id mapping.
Accuracy-first: this is READ-ONLY analysis; no writes."""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

print("=== A) For EVERY player_id in nba.players, what real athlete id does pgs say? ===")
cur.execute("""
  SELECT p.id AS player_id, p.name, p.espn_id AS table_espn,
         p.nba_id,
         mode() WITHIN GROUP (ORDER BY pgs.nba_player_id) AS modal_athlete,
         count(DISTINCT pgs.nba_player_id) FILTER (WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0) AS n_distinct_real_ids
  FROM nba.players p
  LEFT JOIN nba.player_game_stats pgs ON pgs.player_id=p.id
  GROUP BY p.id, p.name, p.espn_id, p.nba_id
  ORDER BY p.id
""")
players = cur.fetchall()
print(f"   total player rows: {len(players)}")
col_modal = [r[4] for r in players]
print(f"   players with a resolvable real athlete id: {sum(1 for m in col_modal if m)}")
print(f"   players with NO pgs athlete id (modal null/0): {sum(1 for m in col_modal if not m)}")

print("\n=== B) Players sharing the SAME real athlete id (the phantom groups) ===")
from collections import defaultdict
by_athlete = defaultdict(list)
for pid, name, tespn, nba, modal, ndist in players:
    if modal:
        by_athlete[modal].append((pid, name, nba, tespn))
groups = {a: v for a, v in by_athlete.items() if len(v) > 1}
print(f"   athlete ids with >1 player_id (phantom groups): {len(groups)}")
total_phantom_rows = sum(len(v)-1 for v in groups.values())
print(f"   total EXTRA player rows in phantom groups: {total_phantom_rows}")
n=0
for a, v in sorted(groups.items(), key=lambda kv:-len(kv[1])):
    names = ", ".join(f"{pid}:{nm}" for pid, nm, _, _ in v)
    print(f"     athlete {a}: {len(v)} players -> {names}")
    n+=1
    if n>=25: print("     ..."); break

print(f"\n=== C) How many players have NO real id AND no nba_id (true orphans)? ===")
orphans = [r for r in players if not r[4] and not r[3]]
print(f"   no athlete id + no nba_id: {len(orphans)}")
for pid, name, tespn, nba, modal, ndist in orphans[:15]:
    print(f"     pid={pid} name={name} table_espn={tespn} nba_id={nba}")

cur.close(); conn.close()
