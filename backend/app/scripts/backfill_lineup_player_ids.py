"""
Backfill NULL player_id in mlb.lineups (fallback SP rows).

The live refresh's update_lineups_for_date fallback inserted the probable SP with
player_id=None (name only). This resolves player_name -> mlb.players.id
(accent-insensitive) and updates those rows. Matched-by-name, so it's the same
resolution the boxscore player matching uses. Unmatched names are logged for review.

Idempotent: only touches rows with player_id IS NULL AND player_name IS NOT NULL.

Usage (non-login shell):
    export XDG_RUNTIME_DIR=/run/user/$(id -u)
    ./venv/bin/python app/scripts/backfill_lineup_player_ids.py
"""
import asyncio, sys, os, unicodedata as _ud
from sqlalchemy import text
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.database import async_session


def _norm(s: str) -> str:
    return _ud.normalize("NFD", s).encode("ascii", "ignore").decode().strip().lower()


async def main():
    async with async_session() as s:
        # name -> [players.id ...] (accent-insensitive)
        name_map = {}
        rows = (await s.execute(text(
            "SELECT id, name, mlb_id FROM mlb.players WHERE name IS NOT NULL"
        ))).fetchall()
        for pid, pname, mlb_id in rows:
            name_map.setdefault(_norm(pname), []).append(pid)
        print(f"Loaded {len(name_map)} unique player-name keys from {len(rows)} players")

        blanks = (await s.execute(text(
            "SELECT id, player_name, game_id, team_side, batting_order "
            "FROM mlb.lineups WHERE player_id IS NULL AND player_name IS NOT NULL "
            "ORDER BY game_id"
        ))).fetchall()
        print(f"Found {len(blanks)} lineup rows with NULL player_id")

        updated = 0
        unresolved = {}
        for lid, pname, gid, side, bo in blanks:
            candidates = name_map.get(_norm(pname), [])
            if not candidates:
                unresolved.setdefault(pname, 0)
                unresolved[pname] += 1
                continue
            pid = candidates[0]  # name is nearly unique (suffix/rare collisions acceptable)
            await s.execute(
                text("UPDATE mlb.lineups SET player_id=:pid, updated_at=now() WHERE id=:lid"),
                {"pid": pid, "lid": lid},
            )
            updated += 1

        await s.commit()
        print(f"Updated {updated} lineup rows with resolved player_id")
        if unresolved:
            print(f"Unresolved (count -> name): {len(unresolved)} distinct names, {sum(unresolved.values())} rows")
            for name, cnt in sorted(unresolved.items(), key=lambda x: -x[1])[:60]:
                print(f"  {cnt}x  {name!r}")


asyncio.run(main())
