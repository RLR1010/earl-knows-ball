#!/usr/bin/env python3
"""Run the standalone XGBoost OU backtest for 2026 and compare features."""
import os, sys, asyncio, logging, json
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from app.handicapping.mlb.mlb_xgb_model_ou import run_backtest, load_data, build_features
from sqlalchemy.ext.asyncio import create_async_engine

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    df, pitcher_df = await load_data(engine)
    feats = build_features(df, pitcher_df)
    await engine.dispose()
    
    result = await run_backtest(df, feats, test_year=2026, train_years=[2021, 2022, 2023, 2024, 2025])
    if "error" in result:
        print(f"Error: {result}")
    else:
        ou = result.get("ou", {})
        print(f"Standalone OU: {ou.get('correct')}-{ou.get('incorrect')}-{ou.get('pushes')} ({ou.get('pct', 0)}%)")
        print(f"Total games: {ou.get('total', 0)}")
        
        # Compare features for a sample 2026 game
        te = feats[feats["year"] == 2026].sort_values(["month", "game_date"]).head(1)
        if len(te) > 0:
            row = te.iloc[0]
            print(f"\nSample 2026 game features (from build_features):")
            print(f"  game_id: {row['game_id']}")
            print(f"  h_pitcher_era_l5: {row.get('h_pitcher_era_l5', 'N/A')}")
            print(f"  a_pitcher_era_l5: {row.get('a_pitcher_era_l5', 'N/A')}")
            print(f"  h_bullpen_ip_l5: {row.get('h_bullpen_ip_l5', 'N/A')}")
            print(f"  a_bullpen_ip_l5: {row.get('a_bullpen_ip_l5', 'N/A')}")
            print(f"  implied_total: {row.get('implied_total', 'N/A')}")
            print(f"  ou_movement: {row.get('ou_movement', 'N/A')}")
            print(f"  park_factor: {row.get('park_factor', 'N/A')}")

asyncio.run(run())
