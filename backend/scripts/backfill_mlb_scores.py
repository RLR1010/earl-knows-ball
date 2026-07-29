"""
Backfill MLB scores for historical seasons (2012-2019).

Re-loads season schedules from the MLB Stats API, which updates
home_score/away_score on existing games that were loaded pre-season
with NULL scores.
"""

import asyncio
import sys
import os

# Fix import path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import async_session
from app.ingestion.mlb_stats import (
    sync_teams,
    sync_seasons,
    load_games_for_season,
    MLB_TEAMS,
)


YEARS = [2012, 2013, 2014, 2015, 2016, 2017, 2018, 2019]


async def main():
    async with async_session() as db:
        print("Syncing teams and seasons...")
        team_map = await sync_teams(db)
        season_map = await sync_seasons(db)
        await db.commit()

        team_abbr_by_api_id = {api_id: abbr for api_id, abbr, _, _, _ in MLB_TEAMS}

        for year in YEARS:
            season_id = season_map.get(year)
            if not season_id:
                print(f"  WARNING: Season {year} not found, skipping")
                continue

            print(f"\nLoading games for {year}...")
            try:
                count = await load_games_for_season(
                    db, year, season_id, team_map, team_abbr_by_api_id
                )
                await db.commit()
                print(f"  {year}: {count} games processed")
            except Exception as e:
                await db.rollback()
                print(f"  {year}: ERROR — {e}")
                continue

    print("\nDone!")


if __name__ == "__main__":
    asyncio.run(main())
