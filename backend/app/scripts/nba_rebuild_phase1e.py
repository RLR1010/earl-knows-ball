import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

print("=== Stray (wrong-id) pgs rows across the 66 split players ===")
# modal per player via mode() in a CTE, then compare
cur.execute("""
  WITH per AS (
    SELECT pgs.player_id, pgs.nba_player_id, count(*) n
    FROM nba.player_game_stats pgs JOIN nba.games g ON g.id=pgs.game_id
    WHERE g.season_id BETWEEN 26 AND 35 AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id),
  modal AS (
    SELECT player_id, mode() WITHIN GROUP (ORDER BY nba_player_id) AS modal_id, sum(n) AS total_rows
    FROM per GROUP BY player_id)
  SELECT
    (SELECT sum(n) FROM per p WHERE p.nba_player_id <> (SELECT modal_id FROM modal m WHERE m.player_id=p.player_id)) AS stray_rows,
    (SELECT count(*) FROM modal WHERE total_rows > 0) AS players
""")
r = cur.fetchone()
print(f"  stray rows (id != modal): {r[0]} | players: {r[1]}")

print("\n=== List the 66 split players (pid, name, ids, counts) ===")
cur.execute("""
  WITH per AS (
    SELECT pgs.player_id, pgs.nba_player_id, count(*) n
    FROM nba.player_game_stats pgs JOIN nba.games g ON g.id=pgs.game_id
    WHERE g.season_id BETWEEN 26 AND 35 AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id)
  SELECT p.player_id, p2.name,
         string_agg(p.nba_player_id::text || ':' || p.n, ', ' ORDER BY p.n DESC) AS ids_and_counts
  FROM per p JOIN nba.players p2 ON p2.id=p.player_id
  GROUP BY p.player_id, p2.name
  HAVING count(*) > 1
  ORDER BY p.player_id
""")
rows = cur.fetchall()
print(f"  split players: {len(rows)}")
for r in rows:
    print(f"    pid={r[0]} {r[1]}: {r[2]}")
cur.close(); conn.close()
