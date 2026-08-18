"""Phase 1c: For each phantom-group athlete id, fetch from ESPN to confirm the REAL player.
Accuracy-critical: we verify against the live ESPN API which name owns each athlete id."""
import psycopg2, json, urllib.request, time
from app.db_urls import PSYCOPG2_DATABASE_URL

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120', 'Accept': 'application/json'}

conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
cur = conn.cursor()
cur.execute("""
  WITH per AS (
    SELECT p.id AS pid, p.name, pgs.nba_player_id AS athlete
    FROM nba.players p JOIN nba.player_game_stats pgs ON pgs.player_id=p.id
    WHERE pgs.nba_player_id IS NOT NULL AND pgs.nba_player_id>0
    GROUP BY p.id, p.name, pgs.nba_player_id)
  SELECT athlete,
         array_agg(DISTINCT pid ORDER BY pid) AS pids,
         string_agg(DISTINCT name, ' / ') AS names
  FROM per
  GROUP BY athlete
  HAVING count(DISTINCT pid)>1
  ORDER BY athlete
""")
groups = cur.fetchall()
print(f"verifying {len(groups)} phantom groups against live ESPN API:\n")
for athlete, pids, names in groups:
    url = f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{athlete}?lang=en&region=us"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=15) as r:
            d = json.load(r)
        real = d.get('fullName','?')
        active = d.get('active')
        pos = (d.get('position') or {}).get('abbreviation') if isinstance(d.get('position'),dict) else '?'
        status = 'ACTIVE' if active else 'inactive/other'
        print(f"  espn_id={athlete}: ESPN says '{real}' ({status}, {pos})")
        print(f"      our rows: pids={list(pids)} names=[{names}]")
    except Exception as e:
        print(f"  espn_id={athlete}: FETCH ERR {e} | our rows: pids={list(pids)} names=[{names}]")
    time.sleep(0.4)
cur.close(); conn.close()
