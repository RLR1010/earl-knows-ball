#!/usr/bin/env python3
"""
Migrate nfl.player_weekly_stats to add full defensive + special-teams columns.

Adds extensive defensive and ST player stat columns (from ESPN NFL core API),
complementing the existing offensive + partial defensive schema. Idempotent —
safe to run repeatedly. 2016+ history will be backfilled by the ingest.

Usage:
    python app/ingestion/nfl_schema_migration.py
"""
import os
import sys

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.db_urls import PSYCOPG2_DATABASE_URL

# ESPN defensive category names -> our column (type, default)
DEFENSIVE_COLS = {
    # tackles
    "tackles_solo": ("integer", "0"),
    "tackles_assist": ("integer", "0"),
    "tackles_combined": ("integer", "0"),
    "tackles_for_loss": ("integer", "0"),
    "qb_hits": ("integer", "0"),
    "hurries": ("integer", "0"),
    "stuffs": ("integer", "0"),
    "sacks": ("double precision", "0"),          # already exists; keep
    "sacks_assisted": ("integer", "0"),
    "sacks_unassisted": ("integer", "0"),
    "safeties": ("integer", "0"),
    # turnovers / passes
    "fumbles_forced": ("integer", "0"),
    "passes_defended": ("integer", "0"),
    "passes_batted_down": ("integer", "0"),
    "interception_yards": ("integer", "0"),
    "interception_tds": ("integer", "0"),
    "defensive_points": ("integer", "0"),
}

# ESPN returning + punting category names -> our columns
SPECIAL_TEAMS_COLS = {    "kick_returns": ("integer", "0"),
    "kick_return_yards": ("integer", "0"),
    "kick_return_tds": ("integer", "0"),
    "long_kick_return": ("integer", "0"),
    "punt_returns": ("integer", "0"),
    "punt_return_yards": ("integer", "0"),
    "punt_return_tds": ("integer", "0"),
    "long_punt_return": ("integer", "0"),
    "punts": ("integer", "0"),
    "punt_yards": ("integer", "0"),
    "avg_punt_yards": ("double precision", "0"),
    "long_punt": ("integer", "0"),
    "punts_inside_20": ("integer", "0"),
    "punts_inside_10": ("integer", "0"),
    "punts_over_50": ("integer", "0"),
    "touchbacks_punting": ("integer", "0"),
    "fair_catches": ("integer", "0"),
}


def migrate(conn_str: str | None = None) -> list[str]:
    conn = psycopg2.connect(conn_str or PSYCOPG2_DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()

    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='nfl' AND table_name='player_weekly_stats'"
    )
    existing = {r[0] for r in cur.fetchall()}

    added = []
    for group, cols in (("defensive", DEFENSIVE_COLS), ("special_teams", SPECIAL_TEAMS_COLS)):
        for col, (typ, default) in cols.items():
            if col in existing:
                continue
            ddl = f'ALTER TABLE nfl.player_weekly_stats ADD COLUMN {col} {typ} DEFAULT {default}'
            cur.execute(ddl)
            added.append(col)
            print(f"  added {col} ({group}, {typ})")
    cur.close()
    conn.close()
    return added


def main():
    added = migrate()
    print(f"Migration done. {len(added)} columns added.")


if __name__ == "__main__":
    main()
