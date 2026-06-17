#!/usr/bin/env python3
"""Run MLB 2026 backtest (no timeout)."""
import os, asyncio, logging
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.handicapping.mlb.mlb_engine import backtest_season

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with AsyncSession(engine) as session:
        r = await backtest_season(session, 2026, resume=False)
        if "error" in r:
            print(f"Error: {r}")
        else:
            rl = r["run_line"]; ou = r["over_under"]; ml = r["moneyline"]
            total = r["total_games"]
            rl_line = f"RL:  {rl['correct']}-{rl['incorrect']}-{rl['pushes']} ({rl['pct']*100:.1f}%)"
            ou_line = f"OU:  {ou['correct']}-{ou['incorrect']}-{ou['pushes']} ({ou['pct']*100:.1f}%)"
            ml_line = f"ML:  {ml['correct']}-{ml['incorrect']} ({ml['pct']*100:.1f}%)"
            print(f"Games: {total}")
            print(rl_line)
            print(ou_line)
            print(ml_line)
    await engine.dispose()

asyncio.run(run())
