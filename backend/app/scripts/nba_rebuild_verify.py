"""Pre-flight verification: show exactly which rows would be deleted for known verification cases,
confirming the phantom-delete logic is correct before executing."""
import psycopg2
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import defaultdict
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()

# true owner map
cur.execute("""
  SELECT aid, player_id FROM (
    SELECT pgs.nba_player_id AS aid, pgs.player_id, count(*) n,
           rank() OVER (PARTITION BY pgs.nba_player_id ORDER BY count(*) DESC, player_id) rk
    FROM nba.player_game_stats pgs
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY pgs.nba_player_id, pgs.player_id) x WHERE rk=1
""")
owner = {aid: pid for aid, pid in cur.fetchall()}

MIN = "NULLIF(regexp_replace(coalesce(minutes,''),'[^0-9.]','','g'),'')::numeric"
cur.execute(f"""
  SELECT pgs.id, pgs.player_id, p.name, pgs.nba_player_id, pgs.game_id, pgs.points, {MIN}
  FROM nba.player_game_stats pgs
  JOIN nba.players p ON p.id=pgs.player_id
  JOIN (
    SELECT game_id, team_id, points, {MIN} AS mins, rebounds_total, assists, nba_player_id AS aid
    FROM nba.player_game_stats
    WHERE nba_player_id IS NOT NULL AND nba_player_id>0
    GROUP BY game_id, team_id, points, {MIN}, rebounds_total, assists, nba_player_id
    HAVING count(*)>1
  ) d ON pgs.game_id=d.game_id AND pgs.points=d.points AND pgs.rebounds_total=d.rebounds_total
     AND pgs.assists=d.assists AND pgs.nba_player_id=d.aid AND {MIN}=d.mins
""")
rows = cur.fetchall()
print(f"total candidate dup rows: {len(rows)}")
to_del = [r for r in rows if owner.get(r[3]) != r[1]]
print(f"would DELETE: {len(to_del)}")
print()
# Show only known verification groups
wanted_aids = {6450, 4066457, 4397475, 3448, 6492, 3469, 6449, 6583, 4594268}
print("=== Verification: deletes for known corruption groups ===")
shown = set()
for rid, pid, name, aid, gid, pts, mins in to_del:
    if aid in wanted_aids and (aid,pid,gid) not in shown:
        shown.add((aid,pid,gid))
        print(f"  DEL pgs={rid} pid={pid} ({name}) aid={aid} (owner pid={owner[aid]}) game={gid} pts={pts} min={mins}")
conn.close()
