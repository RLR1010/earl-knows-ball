"""Backfill nflverse player stats for 2016-2025 into player_weekly_stats.

Calls ingest_nflverse_stats(season_year=...) per season to fill the QB/player
weekly-stat gap (2021-2024 are incomplete). Idempotent (nflverse upsert by
player/season/week). Reports before/after coverage per season.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/backfill_nflverse_player_stats.py [start] [end]
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL
from app.database import async_session
from app.ingestion.nflverse import ingest_nflverse_stats

START = int(sys.argv[1]) if len(sys.argv) > 1 else 2016
END = int(sys.argv[2]) if len(sys.argv) > 2 else 2025

_sync_engine = sa.create_engine(SYNC_DATABASE_URL)


def coverage():
    """Per-season distinct QBs (pass_attempts>0) currently in player_weekly_stats."""
    with _sync_engine.connect() as c:
        r = c.execute(text('''
            SELECT s.year, count(DISTINCT pw.player_id)
            FROM nfl.player_weekly_stats pw
            JOIN nfl.seasons s ON s.id = pw.season_id
            WHERE pw.pass_attempts > 0
            GROUP BY s.year ORDER BY s.year'''))
        return {int(y): int(n) for y, n in r.all()}


def qb_cum_coverage():
    with _sync_engine.connect() as c:
        r = c.execute(text(
            'SELECT season, count(DISTINCT player_id) FROM nfl.qb_cumulative_stats GROUP BY season'))
        return {int(y): int(n) for y, n in r.all()}


async def main():
    before = coverage()
    print("BEFORE (QBs/season in player_weekly_stats):")
    for y in range(2016, 2026):
        print(f"  {y}: {before.get(y, 0)}")

    async with async_session() as db:
        for year in range(START, END + 1):
            try:
                print(f"\n>>> Ingesting {year}...", flush=True)
                res = await ingest_nflverse_stats(db, season_year=year)
                print(f"    {year} result: loaded={res.get('stats_loaded')} "
                      f"no_player={res.get('no_player')} no_team={res.get('no_team')}", flush=True)
                await db.commit()
            except Exception as ex:
                print(f"    {year} ERROR: {ex!r}", flush=True)
                await db.rollback()
                # continue to next season; data may be partially ingested

    after = coverage()
    print("\nAFTER (QBs/season in player_weekly_stats):")
    for y in range(2016, 2026):
        b = before.get(y, 0)
        a = after.get(y, 0)
        print(f"  {y}: {b} -> {a}  (+{a - b})")


if __name__ == "__main__":
    asyncio.run(main())
