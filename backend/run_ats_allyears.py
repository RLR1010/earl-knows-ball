#!/usr/bin/env python3
"""Run ATS all_years backtest for 2021-2026 and save results to JSON."""
import os, asyncio, logging
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from app.handicapping.mlb.mlb_xgb_model_ats import run_all_years

async def run():
    await run_all_years(
        feature_sets=["full"],
        test_years=[2021, 2022, 2023, 2024, 2025, 2026],
        train_from=2016,
    )

asyncio.run(run())
