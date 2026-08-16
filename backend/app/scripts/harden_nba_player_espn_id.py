#!/usr/bin/env python3
"""
Hardening: backfill espn_id onto canonical NBA player rows.

After the 2026-08-14 duplicate-player consolidation, the 45 fold (espn_id-carrying)
rows were deleted, leaving the canonical rows with espn_id = NULL. Since
espn_cache in nba_player_game_stats is keyed by espn_id, a future ingest with a
NULL-espn_id canonical row would AUTO-CREATE a duplicate player again.

The ESPN athlete id for each canonical player is recoverable from
nba.player_game_stats.nba_player_id (the same column the ingest writes from the
roster statistics ref's athlete id). This backfills players.espn_id from that
source, idempotently:

  WHERE players.espn_id IS NULL
    AND pgs.nba_player_id IS NOT NULL
    AND pgs.nba_player_id > 0
    AND that espn_id is not already used by another player row.

No row deletes. Safe re-runnable.
"""
import asyncio

from sqlalchemy import text

from app.database import async_session


async def main():
    async with async_session() as db:
        # Gather candidate espn_id per canonical player from pgs.nba_player_id.
        rows = (await db.execute(text("""
            SELECT pl.id, pl.name, max(p.nba_player_id) AS espn
            FROM nba.players pl
            JOIN nba.player_game_stats p ON p.player_id = pl.id
            WHERE pl.espn_id IS NULL
              AND p.nba_player_id IS NOT NULL
              AND p.nba_player_id > 0
            GROUP BY pl.id, pl.name
        """))).all()

        # Build set of espn_id already assigned to ANY player row.
        used = set((await db.execute(text(
            "SELECT espn_id FROM nba.players WHERE espn_id IS NOT NULL"
        ))).scalars().all())

        updated = 0
        skipped_used = 0
        for player_id, name, espn in rows:
            espn = int(espn)
            if espn in used:
                print(f"  SKIP {name} (id {player_id}): espn_id {espn} already used elsewhere")
                skipped_used += 1
                continue
            await db.execute(text("UPDATE nba.players SET espn_id=:e WHERE id=:c"),
                             {"e": espn, "c": player_id})
            print(f"  SET {name} (id {player_id}): espn_id = {espn}")
            used.add(espn)
            updated += 1

        await db.commit()
        print(f"\nDone: {updated} espn_id set, {skipped_used} skipped (espn_id already used).")


if __name__ == "__main__":
    asyncio.run(main())
