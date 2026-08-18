"""Resolve the 7 ambiguous-name players by matching their pgs game window to ESPN career window."""
import psycopg2, json, urllib.request, time
from app.db_urls import PSYCOPG2_DATABASE_URL
UA={'User-Agent':'Mozilla/5.0','Accept':'application/json'}
conn = psycopg2.connect(PSYCOPG2_DATABASE_URL); cur = conn.cursor()

AMBI = {
 21:[1051,2528096], 469:[3039,3240], 561:[3191,4415554], 996:[2490589,4412182],
 1252:[3059316,4895499], 1384:[3057304], 1472:[4066243,4610145],
}
for pid, ids in AMBI.items():
    cur.execute("SELECT name FROM nba.players WHERE id=%s",(pid,)); nm=cur.fetchone()[0]
    cur.execute("""SELECT min(g.date)::date, max(g.date)::date, count(*) FROM nba.player_game_stats pgs
        JOIN nba.games g ON g.id=pgs.game_id WHERE pgs.player_id=%s AND pgs.nba_player_id>0""",(pid,))
    dmin,dmax,n = cur.fetchone()
    print(f"\npid={pid} {nm}: OUR pgs window {dmin}..{dmax} ({n} rows, all ids)")
    for i in ids:
        url=f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/athletes/{i}?lang=en&region=us"
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=12) as r:
                d=json.load(r)
            print(f"   espn {i} {d.get('fullName')}: active={d.get('active')}")
        except Exception as e:
            print(f"   espn {i}: ERR {e}")
    time.sleep(0.3)
conn.close()
