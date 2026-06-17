#!/usr/bin/env python3
"""Run MLB backtest for all years 2021-2026 in sequence."""
import asyncio, os
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.handicapping.mlb.mlb_engine import backtest_season

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        async with AsyncSession(engine) as session:
            r = await backtest_season(session, year)
            if "error" in r:
                print(f"{year}: Error - {r['error']}")
            else:
                rl = r["run_line"]; ou = r["over_under"]; ml = r["moneyline"]
                print(f"{year}: {r['total_games']}g RL={rl['pct']*100:.1f}% OU={ou['pct']*100:.1f}% ML={ml['pct']*100:.1f}%")
    await engine.dispose()
    print("ALL DONE")

asyncio.run(run())
