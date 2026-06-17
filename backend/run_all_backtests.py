#!/usr/bin/env python3
"""Run MLB backtests for 2021-2026 and save to game_predictions."""
import os, asyncio, logging, sys
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.handicapping.mlb.mlb_engine import backtest_season

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        print(f"\n{'='*60}")
        print(f"Backtesting {year}...")
        print(f"{'='*60}")
        async with AsyncSession(engine) as session:
            r = await backtest_season(session, year, resume=False)
            if "error" in r:
                print(f"  Error: {r}")
            else:
                ou = r["over_under"]
                rl = r["run_line"]
                ml = r["moneyline"]
                print(f"  {year}: RL={rl['pct']*100:.1f}%  OU={ou['pct']*100:.1f}%  ML={ml['pct']*100:.1f}%")
    await engine.dispose()
    print("\nDone!")

asyncio.run(run())
