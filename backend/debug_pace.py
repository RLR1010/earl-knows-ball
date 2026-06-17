import asyncio, httpx, csv, io, gzip

async def main():
    import asyncpg
    conn = await asyncpg.connect("postgresql://earl:earl@localhost:5432/earl_knows_football")
    
    rows = await conn.fetch("""
        SELECT g.id, g.week,
               ht.abbreviation AS home, at.abbreviation AS away
        FROM nfl.games g
        JOIN nfl.seasons s ON s.id = g.season_id
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at ON at.id = g.away_team_id
        WHERE s.year = 2024 AND g.game_type = 'REG'
        ORDER BY g.week
    """)
    
    lookup = {}
    for r in rows:
        gid = f"2024_{r['week']}_{r['away']}_{r['home']}"
        rev = f"2024_{r['week']}_{r['home']}_{r['away']}"
        lookup[gid] = r['id']
        lookup[rev] = r['id']
    
    print(f"DB has {len(rows)} REG games, built {len(lookup)} lookup keys")
    
    resp = httpx.get('https://github.com/nflverse/nflverse-data/releases/download/snap_counts/snap_counts_2024.csv.gz', follow_redirects=True, timeout=60)
    text = gzip.decompress(resp.content).decode()
    reader = csv.DictReader(io.StringIO(text))
    snap_ids = sorted(set(r['game_id'] for r in reader))
    
    matched = 0
    missed = 0
    missed_examples = []
    
    for gid in snap_ids:
        if gid in lookup:
            matched += 1
            _ = lookup[gid]
        else:
            missed += 1
            parts = gid.split('_')
            if len(missed_examples) < 10:
                missed_examples.append(f"{gid} (w{parts[1]}, {parts[3]} hosts {parts[2]})")
    
    print(f"Snap count game IDs: {len(snap_ids)}")
    print(f"Matched: {matched}")
    print(f"Missed:  {missed}")
    print()
    if missed_examples:
        print("Sample missed:")
        for m in missed_examples:
            print(f"  {m}")
    
    await conn.close()

asyncio.run(main())
