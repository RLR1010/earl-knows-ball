#!/usr/bin/env python3
"""Standalone sync script to load nflverse injuries data."""
import csv, io, sys, os, httpx
from pathlib import Path

# Setup path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Async → sync wrapper
import asyncio

# We'll use sync SQLAlchemy directly
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

# Get the DB URL from the app
from app.core.config import settings
from app.database import Base

# Convert async URL to sync
SYNC_URL = settings.database_url.replace("+asyncpg", "+psycopg2")

NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
YEARS = list(range(2016, 2026))


def download_csv(url: str) -> list[dict]:
    """Sync download of CSV data."""
    resp = httpx.get(url, follow_redirects=True, timeout=30)
    resp.raise_for_status()
    text = resp.text
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def load_injuries():
    """Load injuries for given year using sync session."""
    engine = create_engine(SYNC_URL)
    Base.metadata.create_all(engine)

    # Build player_map: nflverse gsis_id → player.id
    with Session(engine) as session:
        players = session.execute(text("SELECT id, nflverse_id FROM nfl.players WHERE nflverse_id IS NOT NULL")).fetchall()
        player_map = {p.nflverse_id: p.id for p in players}

        # Build season map: year → season.id
        seasons = session.execute(text("SELECT id, year FROM nfl.seasons")).fetchall()
        season_map = {s.year: s.id for s in seasons}

    print(f"Player map: {len(player_map)} gsis_ids")
    print(f"Season map: {list(season_map.keys())}")

    total_loaded = 0
    total_skipped = 0
    errors = 0

    for year in YEARS:
        try:
            url = f"{NFLVERSE_BASE}/injuries/injuries_{year}.csv"
            print(f"Downloading {year}... ", end="", flush=True)
            rows = download_csv(url)
            print(f"{len(rows)} records")

            season_id = season_map.get(year)
            if not season_id:
                print(f"  SKIP: no season entry for {year}")
                continue

            loaded = 0
            skipped = 0
            batch = []

            with Session(engine) as session:
                for row in rows:
                    gsis = (row.get("gsis_id") or "").strip()
                    if not gsis:
                        skipped += 1
                        continue

                    player_id = player_map.get(gsis)
                    if not player_id:
                        skipped += 1
                        continue

                    try:
                        week = int(row.get("week", 0))
                    except (ValueError, TypeError):
                        continue

                    report_injury = row.get("report_primary_injury") or ""
                    report_status = row.get("report_status") or ""
                    practice_injury = row.get("practice_primary_injury") or ""
                    practice_status = (row.get("practice_status") or "")[:50]
                    game_status = (report_status or practice_status or "")[:50]

                    # Insert directly via raw SQL for speed
                    batch.append({
                        "player_id": player_id,
                        "week": week,
                        "season_id": season_id,
                        "injury_type": (report_injury or practice_injury or "Unknown")[:100],
                        "practice_status": practice_status,
                        "game_status": game_status,
                    })
                    loaded += 1

                    if len(batch) >= 500:
                        session.execute(
                            text("""
                                INSERT INTO nfl.injuries 
                                (player_id, week, season_id, injury_type, practice_status, game_status)
                                VALUES (:player_id, :week, :season_id, :injury_type, :practice_status, :game_status)
                            """),
                            batch
                        )
                        session.commit()
                        batch = []

                # Flush remaining
                if batch:
                    session.execute(
                        text("""
                            INSERT INTO nfl.injuries 
                            (player_id, week, season_id, injury_type, practice_status, game_status)
                            VALUES (:player_id, :week, :season_id, :injury_type, :practice_status, :game_status)
                        """),
                        batch
                    )
                    session.commit()

                print(f"  {year}: {loaded} loaded, {skipped} skipped")
                total_loaded += loaded
                total_skipped += skipped

        except Exception as e:
            print(f"  ERROR {year}: {e}")
            errors += 1

    print(f"\nDone: {total_loaded} loaded, {total_skipped} skipped, {errors} errors")


if __name__ == "__main__":
    load_injuries()
