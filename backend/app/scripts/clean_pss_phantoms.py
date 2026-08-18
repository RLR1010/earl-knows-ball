"""Delete the phantom nba.player_season_stats rows for season 35 (2025-26).
A pss row is a PHANTOM if the player has ZERO nba.player_game_stats rows for the SAME
(team, season). These are fabricated/retired-player statlines (Rob Edwards 28.8ppg,
Meyers Leonard 27.8ppg, MarShon Brooks, Trevor Booker, etc.) that corrupt star selection
in populate_team_rolling_stats (top-3 scorers = team stars) and cause the 2025 ATS collapse.
Dry-run unless --commit."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
COMMIT='--commit' in sys.argv
conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()

cur.execute("""
SELECT pss.player_id, p.name AS player_name, pss.team_id, pss.points_per_game, pss.games_played
FROM nba.player_season_stats pss
JOIN nba.players p ON p.id = pss.player_id
WHERE pss.season_id = 35
  AND NOT EXISTS (
    SELECT 1 FROM nba.player_game_stats pgs
    JOIN nba.games g ON g.id = pgs.game_id AND g.season_id = 35
    WHERE pgs.player_id = pss.player_id AND pgs.team_id = pss.team_id
  )
ORDER BY pss.points_per_game DESC
""")
phantoms = cur.fetchall()
print(f"PHANTOM pss rows for s35: {len(phantoms)}")
for pid,nm,t,ppg,gp in phantoms:
    print(f"   pid {pid} {nm:28s} team {t:3d} ppg {ppg:5.1f} gp {gp}")

cur.execute("SELECT count(*) FROM nba.player_season_stats WHERE season_id=35")
print("\ns35 pss before:", cur.fetchone()[0])

if COMMIT:
    cur.execute("""
        DELETE FROM nba.player_season_stats
        WHERE season_id = 35
          AND NOT EXISTS (
            SELECT 1 FROM nba.player_game_stats pgs
            JOIN nba.games g ON g.id = pgs.game_id AND g.season_id = 35
            WHERE pgs.player_id = nba.player_season_stats.player_id
              AND pgs.team_id = nba.player_season_stats.team_id
          )
    """)
    conn.commit()
    cur.execute("SELECT count(*) FROM nba.player_season_stats WHERE season_id=35")
    print(f"COMMITTED. s35 pss after: {cur.fetchone()[0]} (deleted {len(phantoms)} phantoms)")
else:
    conn.rollback()
    print("\nDRY RUN — rollback. rerun --commit to delete.")
cur.close(); conn.close()
