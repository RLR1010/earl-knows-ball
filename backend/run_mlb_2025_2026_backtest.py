#!/usr/bin/env python3
"""Clear MLB game_predictions for completed 2025 & 2026 games, then backtest both years."""
import os, asyncio, logging

# Let .env DATABASE_URL (async+asyncpg) remain for module-level engine init
# We'll override after imports for db_training.py which needs sync DSN

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from app.handicapping.mlb.mlb_engine import backtest_season

# Now override for db_training.py's psycopg2 connection
os.environ["DATABASE_URL"] = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

DATABASE_URL_ASYNC = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"

async def run():
    engine = create_async_engine(DATABASE_URL_ASYNC, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    for year in [2025, 2026]:
        # ── Clear existing game_predictions for completed games this year ──
        async with session_factory() as session:
            result = await session.execute(
                text("""
                    DELETE FROM mlb.game_predictions
                    WHERE source = 'api'
                      AND game_id IN (
                        SELECT g.id FROM mlb.games g
                        JOIN mlb.seasons s ON s.id = g.season_id
                        WHERE s.year = :year AND g.status = 'FINAL'
                    )
                """),
                {"year": year},
            )
            deleted = result.rowcount
            await session.commit()
            print(f"[{year}] Cleared {deleted} existing predictions for completed games")

        # ── Run the backtest ──
        async with session_factory() as session:
            r = await backtest_season(session, year, resume=False)

        if "error" in r:
            print(f"[{year}] Error: {r['error']}")
        else:
            rl = r["run_line"]; ou = r["over_under"]; ml = r["moneyline"]
            print(f"[{year}] Results:")
            print(f"       RL: {rl['w']}-{rl['l']}-{rl['push']} ({rl['pct']}%)")
            print(f"       OU: {ou['w']}-{ou['l']}-{ou['push']} ({ou['pct']}%)")
            print(f"       ML: {ml['w']}-{ml['l']} ({ml['pct']}%)")

    await engine.dispose()
    print("\nALL DONE ✅")

asyncio.run(run())
