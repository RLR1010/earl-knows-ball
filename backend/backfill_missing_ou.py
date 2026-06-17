#!/usr/bin/env python3
"""Run historical odds API backfill for the dates missing OAK data."""
import os, asyncio, logging
os.environ["DATABASE_URL"] = "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football"
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.ingestion.mlb_betting_lines import ingest_historical_odds_api_mlb_lines
from datetime import date

async def run():
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with AsyncSession(engine) as db:
        import os as _os
        api_key = _os.environ.get("ODDS_API_KEY", "")
        result = await ingest_historical_odds_api_mlb_lines(
            db=db,
            api_key=api_key,
            start_date=date(2026, 3, 27),
            end_date=date(2026, 6, 7),
        )
        print(f"Result: {result}")
    await engine.dispose()

asyncio.run(run())
