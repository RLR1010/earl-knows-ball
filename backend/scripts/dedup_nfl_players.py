"""Deduplicate nfl.players by nflverse_id and drop nflverse placeholder rows.

Merges duplicates safely: for each duplicated nflverse_id, keep ONE player row
(prefer has team_id, then has sleeper_id, else lowest id). For referencing
tables with a (player_id, ...) natural key, copy dup rows into the keeper where
missing (ON CONFLICT DO NOTHING) then delete the dup's rows; for id-only-key
tables, reassign player_id to the keeper. Finally delete the dup player row.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/dedup_nfl_players.py [--apply]
"""
import os, sys
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL

APPLY = "--apply" in sys.argv
_sync_engine = sa.create_engine(SYNC_DATABASE_URL)

# keyed tables: (table, unique-key-columns) -> merge by copying missing rows
KEYED = [
    ("nfl.player_weekly_stats", ["player_id", "game_id"]),
    ("nfl.qb_cumulative_stats", ["player_id", "season", "game_id"]),
    ("nfl.qb_rolling_stats", ["player_id", "season", "game_id"]),
]
# id-only-key tables: just reassign player_id column
REASSIGN = [
    ("nfl.depth_charts", "player_id"),
    ("nfl.depth_charts_archive", "player_id"),
    ("nfl.injuries", "player_id"),
    ("nfl.transactions", "player_id"),
]

PLACEHOLDERS = ("Player Invalid", "Duplicate Player")


def main():
    if APPLY:
        with _sync_engine.connect() as c:
            nph = c.execute(text(
                "SELECT count(*) FROM nfl.players WHERE name IN :names"),
                {"names": PLACEHOLDERS}).scalar()
        with _sync_engine.begin() as c:
            c.execute(text(
                "DELETE FROM nfl.players WHERE name IN :names"), {"names": PLACEHOLDERS})
        print(f"deleted {nph} placeholder rows")

    with _sync_engine.connect() as c:
        groups = list(c.execute(text('''
            SELECT nflverse_id FROM nfl.players
            WHERE nflverse_id IS NOT NULL GROUP BY nflverse_id HAVING count(*)>1''')))

    print(f"duplicate-nflverse_id groups: {len(groups)}")
    removed = 0
    for (gsis,) in groups:
        with _sync_engine.connect() as c:
            rows = list(c.execute(text(
                "SELECT id, team_id, sleeper_id FROM nfl.players WHERE nflverse_id=:g ORDER BY id"),
                {"g": gsis}))
        keeper = min(rows, key=lambda x: (x[1] is None, x[2] is None, x[0]))
        keep_id = keeper[0]
        for pid, _t, _s in rows:
            if pid == keep_id:
                continue
            if APPLY:
                with _sync_engine.begin() as c:
                    # keyed tables: copy missing rows from pid into keeper
                    for tbl, cols in KEYED:
                        sel_cols = ", ".join(cols)
                        for r_ in list(c.execute(text(
                            f"SELECT * FROM {tbl} WHERE player_id=:d"), {"d": pid})):
                            pass
                        # refresh keeper keys after prior copies
                        keep_keys = set(c.execute(text(
                            f"SELECT {sel_cols} FROM {tbl} WHERE player_id=:k"), {"k": keep_id}).all())
                        dup_rows = list(c.execute(text(
                            f"SELECT * FROM {tbl} WHERE player_id=:d"), {"d": pid}))
                        colnames = list(c.execute(text(f"SELECT * FROM {tbl} LIMIT 0")).keys())
                        for row in dup_rows:
                            key = tuple(row[colnames.index(x)] for x in cols)
                            if key in keep_keys:
                                continue
                            data = dict(zip(colnames, row))
                            data["player_id"] = keep_id
                            collist = ", ".join(colnames)
                            placeholders = ", ".join(":" + x for x in colnames)
                            c.execute(text(
                                f"INSERT INTO {tbl} ({collist}) VALUES ({placeholders}) "
                                f"ON CONFLICT DO NOTHING"), data)
                        # delete dup's now-merged rows
                        c.execute(text(f"DELETE FROM {tbl} WHERE player_id=:d"), {"d": pid})
                    # id-only-key tables: reassign
                    for tbl, col in REASSIGN:
                        c.execute(text(f"UPDATE {tbl} SET {col}=:k WHERE {col}=:d"),
                                  {"k": keep_id, "d": pid})
                    # delete the dup player
                    c.execute(text("DELETE FROM nfl.players WHERE id=:d"), {"d": pid})
            removed += 1

    print(f"removed {removed} duplicate player rows" + ("" if APPLY else " (dry — pass --apply)"))
    with _sync_engine.connect() as c:
        print("final players:", c.execute(text("SELECT count(*) FROM nfl.players")).scalar())
        print("dup nflverse_id groups left:", c.execute(text(
            "SELECT count(*) FROM (SELECT nflverse_id FROM nfl.players WHERE nflverse_id IS NOT NULL GROUP BY nflverse_id HAVING count(*)>1) x"
        )).scalar())


if __name__ == "__main__":
    main()
