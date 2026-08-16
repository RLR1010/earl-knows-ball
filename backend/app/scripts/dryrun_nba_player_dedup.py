#!/usr/bin/env python3
"""
DRY-RUN: NBA duplicate-player consolidation audit (Option B).

Identifies the 45 accent-variant duplicate player rows created as a side-effect
of the 2026-08-14 gap-fill backfill, and reports the EXACT id-mappings and the
affected-row counts across all 4 player_id-referencing tables:

    nba.dfs_salaries, nba.player_game_stats, nba.player_season_stats, nba.player_splits

For each duplicate pair:
  - espn_id row  = the NEW row the backfill auto-created (has an espn_id, holds
                   most of the pgs data ingested 2026-08-14)
  - null row     = the ORIGINAL row (espn_id NULL) that player_season_stats /
                   star_prep actually reference (this is the star-ranking source)

No writes are performed. Prints a summary + the full mapping for review.
"""
import asyncio
import unicodedata
from collections import defaultdict

from sqlalchemy import text

from app.database import async_session

TABLES = ["player_game_stats", "player_season_stats", "player_splits", "dfs_salaries"]


def norm_name(s: str) -> str:
    """Strip diacritics/whitespace, lowercase -> group key."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).strip().lower()


def side(p: tuple) -> str:
    """p = (id, name, espn_id) -> which side of a dup pair this row is."""
    return "espn_id" if p[2] is not None else "null(original)"


async def main():
    async with async_session() as db:
        res = await db.execute(text("SELECT id, name, espn_id FROM nba.players ORDER BY id"))
        players = res.fetchall()

        groups = defaultdict(list)
        for p in players:
            groups[norm_name(p[1])].append(p)

        dups = {k: v for k, v in groups.items() if len(v) > 1}
        # only keep groups that have BOTH an espn_id row and a null row (real dups)
        pairs = {}
        for k, grp in dups.items():
            espn = [p for p in grp if p[2] is not None]
            null = [p for p in grp if p[2] is None]
            if espn and null:
                pairs[k] = (espn, null)

        print(f"Duplicate groups with both espn_id + null row: {len(pairs)}\n")

        # per-player counts on each table, per id
        counts = {}  # tbl -> {id: rowcount}
        for tbl in TABLES:
            q = await db.execute(
                text(f"SELECT player_id, count(*) FROM nba.{tbl} GROUP BY player_id")
            )
            counts[tbl] = dict(q.fetchall())

        print(f"{'name':<28}{'espn rows':<28}{'null rows':<28}")
        print(f"{'':<28}{'ids / counts':<28}{'ids / counts':<28}")
        print("-" * 84)

        grand_move = defaultdict(int)  # tbl -> rows that would move espn->null
        grand_null_move = defaultdict(int)  # tbl -> rows that would move null->null(same, no move)

        for name, (espn, null) in sorted(pairs.items()):
            espn_ids = [p[0] for p in espn]
            null_ids = [p[0] for p in null]
            canonical = null_ids[0]  # star_prep references null row -> keep it
            # show per-table counts on the espn-id side (would remap) vs null side
            parts = [name]
            for tbl in TABLES:
                e = sum(counts[tbl].get(i, 0) for i in espn_ids)
                n = sum(counts[tbl].get(i, 0) for i in null_ids)
                grand_move[tbl] += e
                if e:
                    parts.append(f"{tbl}={e}")
            espn_str = f"[{','.join(map(str,espn_ids))}] {espn.__dict__ if False else ''}"
            # build compact display
            espn_lbl = ", ".join(f"{i}({counts['player_game_stats'].get(i,0)})" for i in espn_ids)
            null_lbl = ", ".join(f"{i}({counts['player_game_stats'].get(i,0)})" for i in null_ids)
            detail = "  ".join(parts[1:]) if len(parts) > 1 else ""
            print(f"{name:<28}{espn_lbl:<28}{null_lbl:<28}  cann={canonical} {detail}")

        print("\n" + "=" * 84)
        print("AGGREGATE rows that would be REMAPPED espn_id_row -> canonical(null) row:")
        for tbl in TABLES:
            print(f"    {tbl:<24} {grand_move[tbl]}")

        # CRITICAL: unique-key collision check on player_game_stats
        print("\n" + "=" * 84)
        print("CRITICAL: would remapping create (game_id, player_id) collisions in player_game_stats?")
        collision_pairs = {}
        for name, (espn, null) in pairs.items():
            espn_id = espn[0][0]
            canon = null[0][0]
            # games where BOTH ids have a row
            q = await db.execute(text("""
                SELECT COUNT(*) FROM (
                    SELECT e.game_id FROM nba.player_game_stats e
                    JOIN nba.player_game_stats n ON n.game_id=e.game_id
                    WHERE e.player_id=:e AND n.player_id=:n
                ) x
            """), {"e": espn_id, "n": canon})
            n = q.scalar()
            if n:
                collision_pairs[name] = n
        if collision_pairs:
            for name, n in collision_pairs.items():
                print(f"    {name}: {n} games have BOTH espn_id and null row -> remap WOULD collide")
        else:
            print("    NONE - remap into distinct games, no unique-key collision risk.")

        # Also: does the espn_id row appear in player_season_stats (star source)?
        print("\n" + "=" * 84)
        print("Espn_id rows that ALSO have player_season_stats rows (would need pss remap):")
        pss_ids = set(counts.get("player_season_stats", {}).keys())
        cnt = 0
        for name, (espn, null) in pairs.items():
            for p in espn:
                if p[0] in pss_ids:
                    cnt += 1
                    print(f"    {name}: espn_id {p[0]} has {counts['player_season_stats'][p[0]]} pss rows")
        if not cnt:
            print("    NONE - all espn_id dup rows are pgs-only (clean remap to canonical).")


if __name__ == "__main__":
    asyncio.run(main())
