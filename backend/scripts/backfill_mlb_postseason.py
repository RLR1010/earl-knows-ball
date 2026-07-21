#!/usr/bin/env python3
"""Backfill MLB postseason games for 2024 and 2025 seasons."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("PYTHONPATH", os.path.join(os.path.dirname(__file__), ".."))

# Import models first to establish the proper SQLAlchemy metadata order
from app.models.mlb import MLBSeason, MLBTeam  # noqa: E402
from app.models.chat import ChatMessage, ChatConversation  # noqa: E402, F401
from app.models.base import Base  # noqa: E402

from app.database import async_session  # noqa: E402
from app.ingestion.mlb_stats import load_games_for_season  # noqa: E402
from sqlalchemy import select  # noqa: E402


async def main():
    years = [2024, 2025]

    async with async_session() as db:
        for year in years:
            print(f"\n=== Loading MLB games (incl. postseason) for {year} ===")

            r = await db.execute(select(MLBSeason).where(MLBSeason.year == year))
            season = r.scalar_one_or_none()
            if not season:
                print(f"Season {year} not found, skipping")
                continue

            r = await db.execute(select(MLBTeam))
            teams = r.scalars().all()
            team_map = {t.mlb_team_id: t.id for t in teams}
            team_abbr = {t.mlb_team_id: t.abbreviation for t in teams if t.abbreviation}

            count = await load_games_for_season(db, year, season.id, team_map, team_abbr)
            print(f"  Loaded {count} new games for {year}")

            await db.commit()
            print(f"  Committed {year}")

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
