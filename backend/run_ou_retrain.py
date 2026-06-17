#!/usr/bin/env python3
"""Run OU backtests with bullpen fix (start at 0), save prod model."""
import os, asyncio, logging, pickle, json
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from app.handicapping.mlb.mlb_xgb_model_ou import load_data, build_features, run_backtest, train_model, FEATURES_TRAINING
from sqlalchemy.ext.asyncio import create_async_engine

test_years = [2021, 2022, 2023, 2024, 2025, 2026]

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()

    results = []
    for year in test_years:
        train_years = list(range(year - 5, year))
        print(f"\n--- OU {year} (train: {train_years[0]}-{train_years[-1]}) ---")
        result = await run_backtest(df, feats, test_year=year, train_years=train_years)
        if "error" not in result:
            results.append(result)
            ou = result.get("ou", {})
            print(f"  OU {year}: {ou.get('correct')}-{ou.get('incorrect')}-{ou.get('pushes')} ({ou.get('pct', '?')}%)")

    with open("/app/data/mlb_ou_backtest_results.json", "w") as f:
        json.dump({"results": results}, f, indent=2, default=str)
    print("\nSaved ou results")

    print("\nTraining OU prod model on 2021-2025 (bullpen starts at 0)...")
    model = await train_model(2026, [2021, 2022, 2023, 2024, 2025])
    with open("/app/data/mlb_ou_model_prod.pkl", "wb") as f:
        pickle.dump({"model": model, "features": FEATURES_TRAINING, "train_years": [2021, 2022, 2023, 2024, 2025]}, f)
    print("Saved ou prod model")
    print("\nDone!")

asyncio.run(run())
