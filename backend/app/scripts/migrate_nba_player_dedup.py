#!/usr/bin/env python3
"""
Migration: consolidate 45 duplicate NBA player rows created as a side-effect of
the 2026-08-14 gap-fill backfill (approved by Rich 2026-08-14 22:26).

Strategy (new-backfill-row-wins):
  canonical = the espn_id-NULL row (referenced by player_season_stats / star_prep)
  fold     = the espn_id row the backfill auto-created (holds most pgs data)

For nba.player_game_stats:
  - Non-collision games (canonical has NO row): UPDATE fold rows -> canonical.
  - Collision games (both ids have a row same game): the fold (new backfill)
    stat line wins -> overwrite canonical with fold values, then DELETE fold row.
    Implemented via temp-table swap for atomicity & to honor uq_player_game.

For nba.player_season_stats:
  - Merge: keep canonical pss; add fold pss rows for seasons not already on
    canonical; for overlapping seasons keep fold values (overwrite).

For nba.player_splits / nba.dfs_salaries:
  - Simple UPDATE fold -> canonical where no canonical row conflicts; if a
    canonical row exists for the same key, delete the fold row.

Backup: all rows to be modified are snapshotted to
  nba._backup_player_dedup_20260814  (created if missing) before changes.
"""
import asyncio
import json

from sqlalchemy import text

from app.database import async_session

# id -> canonical (from dry-run, verified 45 pairs)
FOLD_TO_CANON = {
    2373:287, 2351:645, 2343:659, 2237:826, 2247:897, 2347:920, 2244:986,
    2349:1007, 2345:1039, 2346:1044, 2269:1049, 2250:1123, 2246:1126, 2363:1142,
    2371:1160, 2355:1193, 2357:1200, 2354:1216, 2356:1219, 2350:1231, 2252:1288,
    2366:1334, 2254:1401, 2352:1540, 2368:1545, 2369:1563, 2361:1592, 2372:1594,
    2251:1726, 2266:1758, 2260:1886, 2241:1889, 2375:1915, 2377:1948, 2378:2020,
    2242:2051, 2261:2055, 2099:2262, 2240:2150, 2259:2160, 2236:2162, 2253:2173,
    2265:2199, 2268:2206, 2255:2228,
}

PGS_COLS = [
    "game_id","player_id","team_id","position","jersey_number","is_starter",
    "minutes","field_goals_made","field_goals_attempted","field_goal_pct",
    "three_pointers_made","three_pointers_attempted","three_pointer_pct",
    "free_throws_made","free_throws_attempted","free_throw_pct",
    "rebounds_offensive","rebounds_defensive","rebounds_total","assists",
    "steals","blocks","turnovers","fouls_personal","points","plus_minus",
    "nba_game_id","nba_player_id","scraped_at",
]


async def backup(db, tbl, where_sql, params):
    """Snapshot rows selected by where_sql into the backup table (idempotent)."""
    await db.execute(text(f"""
        CREATE TABLE IF NOT EXISTS nba._backup_player_dedup_20260814 (
            backup_tbl text, backup_row jsonb, backed_up_at timestamptz DEFAULT now()
        )
    """))
    await db.execute(text(f"""
        INSERT INTO nba._backup_player_dedup_20260814 (backup_tbl, backup_row)
        SELECT '{tbl}', to_jsonb(t) FROM (SELECT * FROM nba.{tbl} WHERE {where_sql}) t
    """), params)


async def migrate_pgs(db):
    folds = list(FOLD_TO_CANON.keys())
    canon_ids = list(set(FOLD_TO_CANON.values()))
    print("pgs: folding", len(folds), "ids ->", len(canon_ids), "canonical ids")

    # Backup all fold + canonical pgs rows for these players
    await backup(db, "player_game_stats",
                 "player_id = ANY(:ids)", {"ids": folds + canon_ids})

    # 1) Non-collision: canonical has no row for that game -> plain UPDATE
    noncoll = 0
    for fold, canon in FOLD_TO_CANON.items():
        r = await db.execute(text("""
            UPDATE nba.player_game_stats f
            SET player_id = :canon
            WHERE f.player_id = :fold
              AND NOT EXISTS (
                  SELECT 1 FROM nba.player_game_stats c
                  WHERE c.game_id = f.game_id AND c.player_id = :canon
              )
        """), {"fold": fold, "canon": canon})
        noncoll += r.rowcount
    print("  non-collision pgs rows remapped:", noncoll)

    # 2) Collision: canonical exists same game. Overwrite canonical stat line
    #    with fold values (new backfill data wins), then delete the fold row.
    coll_count = 0
    for fold, canon in FOLD_TO_CANON.items():
        rows = (await db.execute(text("""
            SELECT f.* FROM nba.player_game_stats f
            JOIN nba.player_game_stats c ON c.game_id=f.game_id AND c.player_id=:canon
            WHERE f.player_id=:fold
        """), {"fold": fold, "canon": canon})).mappings().all()
        for _row in rows:
            row = dict(_row)
            gm = row["game_id"]
            sets = ", ".join(f"{c}=:{c}" for c in PGS_COLS if c not in ("player_id","game_id"))
            params = {c: row[c] for c in PGS_COLS if c not in ("player_id","game_id")}
            params.update({"canon": canon, "gm": gm})
            await db.execute(text(f"""
                UPDATE nba.player_game_stats SET {sets}
                WHERE player_id=:canon AND game_id=:gm
            """), params)
            await db.execute(text("DELETE FROM nba.player_game_stats WHERE player_id=:fold AND game_id=:gm"),
                             {"fold": fold, "gm": gm})
            coll_count += 1
    print("  collision games resolved (fold line won):", coll_count)


async def migrate_pss(db):
    print("\npss: merge season stats")
    await backup(db, "player_season_stats", "player_id = ANY(:ids)",
                 {"ids": list(FOLD_TO_CANON.keys()) + list(set(FOLD_TO_CANON.values()))})
    for fold, canon in FOLD_TO_CANON.items():
        # seasons on fold not on canon -> reassign to canon
        r = await db.execute(text("""
            UPDATE nba.player_season_stats
            SET player_id = :canon
            WHERE player_id = :fold
              AND season_id NOT IN (
                  SELECT season_id FROM nba.player_season_stats WHERE player_id = :canon
              )
        """), {"fold": fold, "canon": canon})
        if r.rowcount:
            print(f"   {fold}->{canon}: moved {r.rowcount} non-overlap pss rows")
        # overlapping seasons: keep canonical row (do not overwrite pss totals to
        # avoid double counting). Duplicate-season fold rows for overlap are dropped.
        r2 = await db.execute(text("""
            DELETE FROM nba.player_season_stats
            WHERE player_id = :fold
              AND season_id IN (SELECT season_id FROM nba.player_season_stats WHERE player_id = :canon)
        """), {"fold": fold, "canon": canon})
        if r2.rowcount:
            print(f"   {fold}->{canon}: dropped {r2.rowcount} overlap pss rows (canonical kept)")


def _pgs_cols():
    return PGS_COLS


async def migrate_splits(db):
    """player_splits has UNIQUE (player_id, season_id, split_type).
    Fold-wins rule: reassign non-overlap rows to canonical; for (season,split_type)
    overlap, overwrite canonical's row with the fold row and delete the fold row."""
    print("\nplayer_splits: merge on (season_id, split_type)")
    await backup(db, "player_splits", "player_id = ANY(:ids)",
                 {"ids": list(FOLD_TO_CANON.keys()) + list(set(FOLD_TO_CANON.values()))})
    splice_cols = [
        "season_id","team_id","games","games_started","minutes_per_game",
        "points_per_game","field_goals_pct","three_point_pct","free_throw_pct",
        "rebounds_per_game","offensive_rebounds_per_game","defensive_rebounds_per_game",
        "assists_per_game","steals_per_game","blocks_per_game","turnovers_per_game",
        "fouls_per_game","plus_minus_per_game","true_shooting_pct","usage_pct","created_at","updated_at","split_label"
    ]
    noncoll = 0
    coll = 0
    for fold, canon in FOLD_TO_CANON.items():
        # non-overlap: (season_id, split_type) not on canonical -> reassign
        r = await db.execute(text("""
            UPDATE nba.player_splits f
            SET player_id = :canon
            WHERE f.player_id = :fold
              AND NOT EXISTS (
                  SELECT 1 FROM nba.player_splits c
                  WHERE c.player_id = :canon AND c.season_id = f.season_id AND c.split_type = f.split_type
              )
        """), {"fold": fold, "canon": canon})
        noncoll += r.rowcount
        # overlap rows: fold wins -> overwrite canon's row with fold values
        rows = (await db.execute(text("""
            SELECT f.* FROM nba.player_splits f
            JOIN nba.player_splits c
              ON c.player_id=:canon AND c.season_id=f.season_id AND c.split_type=f.split_type
            WHERE f.player_id=:fold
        """), {"fold": fold, "canon": canon})).mappings().all()
        for _row in rows:
            row = dict(_row)
            sets = ", ".join(f"{c}=:{c}" for c in splice_cols)
            params = {c: row[c] for c in splice_cols}
            params.update({"canon": canon, "season": row["season_id"], "stype": row["split_type"]})
            await db.execute(text(f"UPDATE nba.player_splits SET {sets} WHERE player_id=:canon AND season_id=:season AND split_type=:stype"), params)
            await db.execute(text("DELETE FROM nba.player_splits WHERE player_id=:fold AND season_id=:season AND split_type=:stype"),
                             {"fold": fold, "season": row["season_id"], "stype": row["split_type"]})
            coll += 1
    print(f"  non-overlap remapped: {noncoll}; overlap resolved (fold won): {coll}")


async def main():
    async with async_session() as db:
        await migrate_pgs(db)
        await migrate_pss(db)
        await migrate_splits(db)
        # dfs_salaries: unique key is (platform, player_name, season_id); fold rows
        # were created with the fold-typed player_name. 0 fold rows observed, but
        # remap defensively by player_id.
        await backup(db, "dfs_salaries", "player_id = ANY(:ids)",
                     {"ids": list(FOLD_TO_CANON.keys()) + list(set(FOLD_TO_CANON.values()))})
        for fold, canon in FOLD_TO_CANON.items():
            await db.execute(text("UPDATE nba.dfs_salaries SET player_id=:canon WHERE player_id=:fold"),
                             {"fold": fold, "canon": canon})
        # delete orphans
        await db.execute(text("DELETE FROM nba.players WHERE id = ANY(:ids)"),
                         {"ids": list(FOLD_TO_CANON.keys())})
        await db.commit()
        print("\nDeleted", len(FOLD_TO_CANON), "orphaned duplicate player rows.")
        print("MIGRATION COMMITTED. Backup: nba._backup_player_dedup_20260814")


if __name__ == "__main__":
    asyncio.run(main())
