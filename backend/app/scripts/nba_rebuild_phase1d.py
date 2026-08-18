"""Quantify: how clean is each player's pgs.nba_player_id? 
For accuracy, a player should have ONE dominant athlete id. Measure split distributions."""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

print("=== Per-player pgs athlete-id cleanliness (s26-35) ===")
cur.execute("""
  WITH per AS (
    SELECT pgs.player_id, pgs.nba_player_id, count(*) n
    FROM nba.player_game_stats pgs JOIN nba.games g ON g.id=pgs.game_id
    WHERE g.season_id BETWEEN 26 AND 35 AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id),
  agg AS (
    SELECT player_id, sum(n) AS total,
           max(n) AS top_n, sum(n)-max(n) AS rest
    FROM per GROUP BY player_id)
  SELECT
    count(*) AS players_with_id,
    count(*) FILTER (WHERE rest=0) AS ONE_clean_id,
    count(*) FILTER (WHERE rest>0) AS split_multiple_ids,
    count(*) FILTER (WHERE top_n*2 < total) AS no_majority,
    round(avg(rest) FILTER (WHERE rest>0)::numeric,1) AS avg_stray_rows_per_split_player
  FROM agg
""")
r = cur.fetchone()
print(f"  players with athlete id: {r[0]}")
print(f"  CLEAN (single id): {r[1]}")
print(f"  SPLIT (>=2 ids, some errors): {r[2]}")
print(f"  no-majority (most problematic): {r[3]}")
print(f"  avg stray rows per split player: {r[4]}")

print("\n=== Total stray (wrong-id) pgs rows that need correcting ===")
cur.execute("""
  WITH per AS (
    SELECT pgs.player_id, pgs.nba_player_id, count(*) n,
           mode() WITHIN GROUP (ORDER BY pgs.nba_player_id) OVER (PARTITION BY pgs.player_id) AS modal
    FROM nba.player_game_stats pgs JOIN nba.games g ON g.id=pgs.game_id
    WHERE g.season_id BETWEEN 26 AND 35 AND pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.player_id, pgs.nba_player_id)
  SELECT sum(n) FILTER (WHERE nba_player_id <> modal) AS stray_rows,
         count(DISTINCT player_id) FILTER (WHERE nba_player_id <> modal) AS affected_players
  FROM per
""")
r = cur.fetchone()
print(f"  stray pgs rows where athlete id != player's modal: {r[0]} across {r[1]} players")
cur.close(); conn.close()
