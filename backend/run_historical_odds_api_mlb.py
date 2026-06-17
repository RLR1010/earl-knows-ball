#!/usr/bin/env python3
"""
Run historical MLB betting lines from The Odds API paid tier.
Runs directly against the database, calling the ingestion function.

Usage:
    python3 run_historical_odds_api_mlb.py
    python3 run_historical_odds_api_mlb.py 2025-01-01 2025-06-01
    python3 run_historical_odds_api_mlb.py 2020-06-30 2026-06-05
"""
import asyncio
import logging
import os
import sys
from datetime import date, datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)

# Add app dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.environ.setdefault("APP_ENV", "production")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from app.core.config import settings
from app.ingestion.mlb_betting_lines import ingest_historical_odds_api_mlb_lines


async def main():
    api_key = settings.odds_api_key or os.environ.get("ODDS_API_KEY", "")

    if not api_key:
        print("ERROR: No ODDS_API_KEY found")
        sys.exit(1)

    start_str = sys.argv[1] if len(sys.argv) > 1 else "2020-06-30"
    end_str = sys.argv[2] if len(sys.argv) > 2 else datetime.now().strftime("%Y-%m-%d")

    start_date = date.fromisoformat(start_str)
    end_date = date.fromisoformat(end_str)

    print(f"=== Historical MLB Odds API (paid tier) ===")
    print(f"Date range: {start_date} to {end_date}")
    print(f"API key: {api_key[:8]}...{api_key[-4:]}")
    print(f"Database URL: {settings.database_url[:40]}...")
    print()

    # Create engine and session
    engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=4)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        result = await ingest_historical_odds_api_mlb_lines(
            db=db,
            api_key=api_key,
            start_date=start_date,
            end_date=end_date,
            source_name="the_odds_api_historical",
            markets="totals",
        )

    print()
    print("=" * 60)
    print("RESULT:")
    for key, value in result.items():
        if isinstance(value, dict):
            print(f"  {key}:")
            for k, v in value.items():
                print(f"    {k}: {v}")
        else:
            print(f"  {key}: {value}")
    print("=" * 60)

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
