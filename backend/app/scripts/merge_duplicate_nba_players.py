"""Merge duplicate NBA player identities (winner keeps rows; loser's rows reassigned).

Background: two duplicate player records exist in nba.players for the SAME human:
  - Robert Williams      (pid 1450, br_id williro04, no espn_id)
    vs Robert Williams III (pid 2267, espn_id 4066211, rich bio)
  - Willy Hernangomez    (pid 2382, espn_id 2999409)
    vs Willy HernangÃ³mez (pid 2406, br_id hernawi01, mojibake name)

These caused boxscores to over-count (game 50446 +8, game 49009 +12) because both
duplicates got pgs rows in the same game.

This script merges the LOSER pid into the WINNER pid:
  - reassign all player_id refs loser->winner across every nba.* table that has player_id
  - dedupe (game_id, team_id) pgs rows (keep winner's, drop loser's) to avoid double-count
  - copy missing identity fields (br_id/espn_id/nba_id/bio) from loser to winner if winner lacks them
  - deactivate the loser row (keep it as a tombstone, don't hard-delete in case of rollback)

SAFE: dry-run by default; --commit to write. Prints every change. Uses a single txn.
"""
import argparse
import logging
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s [%(levelname)s] %(message)s")
logger = logging.getLogger("earl.nba_merge_player")

SYNC_DATABASE_URL = "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football"

# (WINNER, LOSER, br_id_to_set_on_winner)  -- winner keeps the authoritative identity
MERGES = [
    (2267, 1450, "williro04"),   # keep Robert Williams III (espn 4066211), fold in br_id
    (2382, 2406, "hernawi01"),   # keep clean Willy Hernangomez (espn 2999409), fold in br_id
]

# tables with a player_id column to reassign (exclude backups AND derived tables --
# derived rolling/season/splits rows are rebuilt from scratch by their builders after
# the pgs/identity fixes, so we only move raw source data here).
REF_TABLES = ["player_game_stats", "active_players", "inactive_players"]

# raw tables get UNIQUE-key-aware merge; derived tables are rebuilt, not merged
DERIVED_TABLES = {"player_rolling_stats", "player_season_stats", "player_splits", "dfs_salaries"}


def player_tables(engine):
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema='nba'
              AND table_name NOT ILIKE 'bak%%'
              AND table_name NOT ILIKE '%%_bak%%'
              AND table_name NOT ILIKE '%%backup%%'
        """)).fetchall()
        return [r[0] for r in rows]


def has_player_id(engine, table):
    with engine.connect() as c:
        return c.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema='nba' AND table_name=:t AND column_name='player_id'
        """), {"t": table}).scalar() > 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--winner", type=int, default=0)
    ap.add_argument("--loser", type=int, default=0)
    ap.add_argument("--br-id", default=None)
    args = ap.parse_args(argv)

    merges = MERGES
    if args.winner and args.loser:
        merges = [(args.winner, args.loser, args.br_id)]

    engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
    mode = "COMMIT" if args.commit else "DRY-RUN"

    all_tables = player_tables(engine)
    ref_tables = [t for t in all_tables if has_player_id(engine, t)]
    logger.info(f"[{mode}] ref tables with player_id: {ref_tables}")

    with engine.begin() as conn:
        for winner, loser, br_id in merges:
            logger.info("=" * 60)
            w = conn.execute(text("SELECT name,espn_id,br_id,active FROM nba.players WHERE id=:p"), {"p": winner}).first()
            l = conn.execute(text("SELECT name,espn_id,br_id,active FROM nba.players WHERE id=:p"), {"p": loser}).first()
            if not w or not l:
                logger.warning(f"pid {winner} or {loser} not found; skip"); continue
            logger.info(f"MERGE {loser} ('{l[0]}', espn={l[1]}, br={l[2]}) -> {winner} ('{w[0]}', espn={w[1]}, br={w[2]})")

            # derived tables are rebuilt by their builders after this -- note, don't merge
            derived_present = [t for t in DERIVED_TABLES if t in ref_tables]
            if derived_present:
                logger.info(f"   [skip] derived tables {derived_present} will be REBUILT by builders, not merged")

            # 1) reassign player_id refs (raw tables only; derived are rebuilt)
            moved = 0
            for t in ref_tables:
                if t in DERIVED_TABLES or 'bak' in t.lower():
                    continue
                n = conn.execute(text(f"SELECT COUNT(*) FROM nba.{t} WHERE player_id=:p"), {"p": loser}).scalar()
                if n:
                    if args.commit:
                        # raw tables with UNIQUE(game_id, player_id): delete loser's row
                        # where the winner already has a row for that game, else reassign
                        if t in ("player_game_stats", "active_players"):
                            conn.execute(text(f"""
                                DELETE FROM nba.{t}
                                WHERE player_id=:l
                                  AND (game_id) IN (
                                      SELECT game_id FROM nba.{t} WHERE player_id=:w)
                            """), {"w": winner, "l": loser})
                        conn.execute(text(f"UPDATE nba.{t} SET player_id=:w WHERE player_id=:l"),
                                     {"w": winner, "l": loser})
                    moved += n
                    logger.info(f"   {t}: {n} rows player_id {loser}->{winner}"
                                + ("" if args.commit else " (dry)"))
            logger.info(f"   total rows moved: {moved}")

            # 2) dedupe pgs (game_id, team_id) collisions: keep winner row, drop loser's
            if args.commit:
                dups = conn.execute(text("""
                    SELECT a.game_id, a.team_id, a.player_id, b.player_id
                    FROM nba.player_game_stats a JOIN nba.player_game_stats b
                      ON a.game_id=b.game_id AND a.team_id=b.team_id
                      AND a.player_id=:w AND b.player_id=:l
                """), {"w": winner, "l": loser}).fetchall()
                for d in dups:
                    conn.execute(text(
                        "DELETE FROM nba.player_game_stats WHERE game_id=:g AND team_id=:t AND player_id=:p"
                    ), {"g": d[0], "t": d[1], "p": loser})
                    logger.info(f"   deduped pgs game {d[0]} team {d[1]} (kept pid {winner}, dropped {loser})")
                # also dedupe other tables with natural keys if any duplicates formed
            else:
                dups = conn.execute(text("""
                    SELECT a.game_id, a.team_id
                    FROM nba.player_game_stats a JOIN nba.player_game_stats b
                      ON a.game_id=b.game_id AND a.team_id=b.team_id
                      AND a.player_id=:w AND b.player_id=:l
                """), {"w": winner, "l": loser}).fetchall()
                for d in dups:
                    logger.info(f"   (dry) would dedupe pgs game {d[0]} team {d[1]}")

            # 3) copy missing identity fields to winner
            fields = {"br_id": br_id}
            if w[1] is None and l[1] is not None:
                fields["espn_id"] = l[1]
            if args.commit:
                # transfer identity fields: clear loser's br_id first (unique index)
                # since loser is merged/deactivated, its slug moves to winner
                for col, val in fields.items():
                    if val is not None:
                        conn.execute(text(f"UPDATE nba.players SET {col}=NULL WHERE id=:p"), {"p": loser})
                        conn.execute(text(f"UPDATE nba.players SET {col}=:v WHERE id=:p"),
                                     {"v": val, "p": winner})
                        logger.info(f"   moved winner {winner}.{col}={val} (cleared on loser {loser})")
                # 4) deactivate loser (tombstone; don't hard-delete)
                conn.execute(text("UPDATE nba.players SET active=0 WHERE id=:p"), {"p": loser})
                logger.info(f"   deactivated loser {loser}")
            else:
                for col, val in fields.items():
                    if val is not None:
                        logger.info(f"   (dry) set winner {winner}.{col}={val}")
                logger.info(f"   (dry) deactivate loser {loser}")

        if not args.commit:
            logger.info("DRY-RUN complete. Pass --commit to apply.")
        else:
            logger.info("COMMITTED all merges.")


if __name__ == "__main__":
    main(None)
