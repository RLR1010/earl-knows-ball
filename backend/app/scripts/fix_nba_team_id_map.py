#!/usr/bin/env python3
"""Fix NBA team-id discrepancy: 2026-27 (s37) games + 2019-20 leaked POST games were
ingested under duplicate team rows (56-61) instead of canonical rows (4,8,16,23,26,28).

Mapping (dup -> canonical):
  57 NO -> 4 NOP
  61 GS -> 8 GSW
  60 NY -> 16 NYK
  59 SA -> 23 SAS
  56 UTAH -> 26 UTA
  58 WSH -> 28 WAS

Backups were created before running (nba.backup_teamidfix_*_<tbl>).
Transactional: all-or-nothing.
"""
import asyncio
import sys
from sqlalchemy import text
from app.database import async_session

MAPPING = {57: 4, 61: 8, 60: 16, 59: 23, 56: 26, 58: 28}  # dup -> canonical
# Scope: season 37 (2026-27 REG) games live ONLY on dup ids, plus the 7 leaked
# 2019-20 (s29) UTAH POST games (56->26). Both are pure moves with no collision.

async def main():
    DRY = "--dry-run" in sys.argv
    async with async_session() as db:
        async with db.begin():
            total = 0
            for col in ("home_team_id", "away_team_id"):
                for dup, canon in MAPPING.items():
                    r = await db.execute(text(
                        f"UPDATE nba.games SET {col}=:canon WHERE {col}=:dup AND season_id IN (29,37)"
                    ), {"canon": canon, "dup": dup})
                    total += r.rowcount
            print(f"[{'DRY' if DRY else 'APPLIED'}] nba.games team refs updated: {total}")
            if DRY:
                raise RuntimeError("dry-run rollback")
    print("done")

asyncio.run(main())
