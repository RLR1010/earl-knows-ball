"""ROOT-CAUSE FIX (commit): eliminate ALL duplicate espn_id values.
For each player in a dup group: espn_id = their own pgs nba_player_id (authoritative), else NULL.
Then add a UNIQUE index on nba.players(espn_id) to permanently prevent espn_cache collapse.
Dry-run unless --commit."""
import psycopg2, sys
from app.db_urls import PSYCOPG2_DATABASE_URL
from collections import Counter
COMMIT='--commit' in sys.argv
conn=psycopg2.connect(PSYCOPG2_DATABASE_URL); conn.autocommit=False
cur=conn.cursor()
def q1(s,p=None): cur.execute(s,p or []); return cur.fetchone()[0]

print("BEFORE dup espn groups:", q1("""SELECT count(*) FROM (SELECT espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 GROUP BY espn_id HAVING count(*)>1) x"""))

# For EVERY player (not just dup groups): set espn_id = pgs_aid if shared/wrong, else NULL if no aid & not the real owner
# Strategy: keep espn_id where it's UNIQUE (likely correct owner). For shared ids, assign the owner's pgs_aid;
# for members without pgs aid, NULL them.
cur.execute("""SELECT p.id, p.name, p.espn_id,
    (SELECT pgs.nba_player_id FROM nba.player_game_stats pgs WHERE pgs.player_id=p.id AND pgs.nba_player_id>0 LIMIT 1) AS aid
    FROM nba.players p WHERE p.espn_id IS NOT NULL AND p.espn_id>0""")
rows=cur.fetchall()

# count current espn_id multiplicity
cnt=Counter(r[2] for r in rows)
changes=[]
for pid,nm,e,aid in rows:
    if cnt[e]==1:
        continue  # unique espn_id -> keep (correct owner)
    # shared espn_id -> this group needs fixing
    # assign pgs aid if present (each player's own real id), else NULL
    new = aid  # may be None
    if new != e:
        changes.append((pid,nm,e,new))

print(f"{len(changes)} shared-espn players to fix (ids -> own pgs aid or NULL)")
for pid,nm,old,new in changes[:25]:
    print(f"   {nm}: espn {old} -> {new}")

# also: players whose espn_id is UNIQUE but does NOT match their pgs aid (espn_id wrong, pgs correct) -> fix to pgs aid
fixm=[]
for pid,nm,e,aid in rows:
    if cnt[e]==1 and aid and e!=aid:
        # espn_id unique but doesn't match their own pgs aid -> espn_id is wrong metadata
        fixm.append((pid,nm,e,aid))
print(f"\n+ {len(fixm)} unique-espn players whose espn_id != their own pgs aid (fix to aid)")

allfix = changes + fixm
if COMMIT:
    for pid,nm,old,new in allfix:
        if new:
            cur.execute("UPDATE nba.players SET espn_id=%s WHERE id=%s",(new,pid))
        else:
            cur.execute("UPDATE nba.players SET espn_id=NULL WHERE id=%s",(pid,))
    # unique index
    cur.execute("DROP INDEX IF EXISTS nba.idx_players_espn_id_unique")
    cur.execute("CREATE UNIQUE INDEX idx_players_espn_id_unique ON nba.players(espn_id) WHERE espn_id IS NOT NULL")
    conn.commit()
    print("\nCOMMITTED. final dup groups:", q1("""SELECT count(*) FROM (SELECT espn_id FROM nba.players WHERE espn_id IS NOT NULL AND espn_id>0 GROUP BY espn_id HAVING count(*)>1) x"""))
    print("UNIQUE index created on espn_id.")
else:
    conn.rollback()
    print("\nDRY RUN — rollback. rerun --commit")
cur.close(); conn.close()
