import asyncio, asyncpg

async def test():
    conn = await asyncpg.connect("postgresql://earl:earl@localhost:5432/earl_knows_football")
    r = await conn.fetchrow("SELECT count(*) FROM nfl.game_predictions")
    print(f"Total predictions: {r['count']}")
    r2 = await conn.fetch("SELECT source, count(*) FROM nfl.game_predictions GROUP BY source")
    for row in r2:
        print(f"  source={row['source']}: {row['count']}")
    # Check table schema
    r3 = await conn.fetch("""
        SELECT column_name, data_type, is_nullable 
        FROM information_schema.columns 
        WHERE table_schema='nfl' AND table_name='game_predictions'
        ORDER BY ordinal_position
    """)
    for row in r3:
        print(f"  {row['column_name']:30s} {row['data_type']:20s} nullable={row['is_nullable']}")
    await conn.close()

asyncio.run(test())
