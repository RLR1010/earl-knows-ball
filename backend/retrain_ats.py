#!/usr/bin/env python3
"""Retrain MLB ATS model and save to prod path, then run backtests for 2021-2026."""
import os, asyncio, logging, pickle, sys
from pathlib import Path

os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from app.handicapping.mlb.mlb_xgb_model_ats import train_model, run_single
from sqlalchemy.ext.asyncio import create_async_engine

PROD_PATH = Path("/app/data/mlb_margin_model_prod.pkl")

async def run():
    # Train on 2021-2025 and save to prod path
    train_years = [2021, 2022, 2023, 2024, 2025]
    print(f"\n{'='*60}")
    print(f"Training ATS model on {train_years}...")
    print(f"{'='*60}")
    model = await train_model(2026, train_years, feature_set="full")
    
    with open(PROD_PATH, "wb") as f:
        pickle.dump({"model": model, "features": None, "train_years": train_years}, f)
    print(f"  Saved to {PROD_PATH}")
    
    # Run backtests for each year 2021-2026
    print(f"\n{'='*60}")
    print(f"Running backtests 2021-2026...")
    print(f"{'='*60}")
    for year in [2021, 2022, 2023, 2024, 2025, 2026]:
        print(f"\n--- {year} ---")
        await run_single(test_year=year, feature_set="full")

    print(f"\nDone!")

asyncio.run(run())
