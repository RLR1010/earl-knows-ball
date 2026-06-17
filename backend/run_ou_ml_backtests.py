#!/usr/bin/env python3
"""Run OU and ML backtests with 5yr windows, save prod models."""
import os, asyncio, logging, pickle, json
from pathlib import Path

os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

test_years = [2021, 2022, 2023, 2024, 2025, 2026]

async def run():
    from sqlalchemy.ext.asyncio import create_async_engine

    # ── OU ──
    print("\n" + "="*60)
    print("MLB OU: backtests + prod model")
    print("="*60)
    
    from app.handicapping.mlb.mlb_xgb_model_ou import load_data, build_features, run_backtest, train_model, FEATURES_TRAINING
    
    engine = create_async_engine(os.environ["DATABASE_URL"])
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()

    ou_results = []
    for year in test_years:
        train_years = list(range(year - 5, year))
        print(f"\n--- OU {year} (train: {train_years[0]}-{train_years[-1]}) ---")
        result = await run_backtest(df, feats, test_year=year, train_years=train_years)
        if "error" not in result:
            ou_results.append(result)
            ou_pct = result.get("ou", {}).get("pct", "?")
            print(f"  OU {year}: {ou_pct}%")

    with open("/app/data/mlb_ou_backtest_results.json", "w") as f:
        json.dump({"results": ou_results}, f, indent=2, default=str)
    print("  Saved ou results")

    print("\nTraining OU prod model on 2021-2025...")
    ou_model = await train_model(2026, [2021, 2022, 2023, 2024, 2025])
    with open("/app/data/mlb_ou_model_prod.pkl", "wb") as f:
        pickle.dump({"model": ou_model, "features": FEATURES_TRAINING, "train_years": [2021, 2022, 2023, 2024, 2025]}, f)
    print("  Saved ou prod model")

    # ── ML ──
    print("\n" + "="*60)
    print("MLB ML: backtests + prod model")
    print("="*60)

    from app.handicapping.mlb.mlb_xgb_model_ml import load_data as ml_load, build_features as ml_build, run_backtest as ml_bt, train_model as ml_train

    engine = create_async_engine(os.environ["DATABASE_URL"])
    df_ml, pitcher_df_ml = await ml_load(engine)
    feats_ml = ml_build(df_ml, pitcher_df_ml)
    await engine.dispose()

    ml_results = []
    for year in test_years:
        train_years = list(range(year - 5, year))
        print(f"\n--- ML {year} (train: {train_years[0]}-{train_years[-1]}) ---")
        result = await ml_bt(df_ml, feats_ml, test_year=year, train_years=train_years)
        if "error" not in result:
            ml_results.append(result)
            ml_pct = result.get("ml", {}).get("pct", "?")
            print(f"  ML {year}: {ml_pct}%")

    with open("/app/data/mlb_ml_backtest_results.json", "w") as f:
        json.dump({"results": ml_results}, f, indent=2, default=str)
    print("  Saved ml results")

    print("\nTraining ML prod model on 2021-2025...")
    ml_model = await ml_train(2026, [2021, 2022, 2023, 2024, 2025])
    with open("/app/data/mlb_ml_residual_model_prod.pkl", "wb") as f:
        pickle.dump({"model": ml_model, "train_years": [2021, 2022, 2023, 2024, 2025]}, f)
    print("  Saved ml prod model")
    
    print("\nDone!")

asyncio.run(run())
