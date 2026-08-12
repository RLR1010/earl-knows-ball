"""
Migrate historical lines from nfl.betting_lines_old -> nfl.betting_lines_consolidated.

WHY: nfl.betting_lines_consolidated only holds lines from 2021 onward (the
modern nfl.betting_lines table starts there). nfl.betting_lines_old holds the
nflverse backfill for 2016-2020 (1201 games, source='nflverse'), which the ATS
model needs so training is no longer starved for those years.

SAFETY:
  * Backs up nfl.betting_lines_consolidated to a timestamped table first
    (nfl.betting_lines_consolidated_backup_YYYYMMDD_HHMMSS) — created BEFORE
    any writes, so a bad migration is trivially reversible.
  * Idempotent upsert keyed on game_id (ON CONFLICT (game_id) DO UPDATE).
  * Dry-run by default (--commit to actually write).

CRITICAL SIGN CONVENTION (verified 2026-08-11, ~99.6% against moneylines):
  * nfl.betting_lines_old uses the REVERSED convention:
        spread > 0  => HOME is favored   (home -7.5 when spread=+7.5)
        spread < 0  => AWAY is favored   (home +3.5 when spread=-3.5)
  * nfl.betting_lines_consolidated (ours):
        closing_spread < 0 => HOME is favored
  * So we MUST flip the sign:  migrated_spread = -1 * old.spread.

OPENING LINES: betting_lines_old has no opening lines (all is_opening=false),
so we write the SAME value into both opening_* and closing_* (per Rich).

Usage:
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/migrate_betting_lines_old_to_consolidated.py
        (dry run — reports what would change, no writes)
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/migrate_betting_lines_old_to_consolidated.py --commit
        (backup + migrate for real)
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/migrate_betting_lines_old_to_consolidated.py --commit --game-ids 401916523,401916524
        (backup + migrate only listed game_ids; still creates a backup)
"""
import argparse
import logging
import sys
from datetime import datetime

from app.database import engine

logger = logging.getLogger("earl.migrate_betting_lines_old")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

# Same target columns as nfl_betting_lines_consolidate.upsert_rows, so the
# consolidated row shape is identical regardless of source.
COLS = [
    "game_id", "game_time", "home_team", "away_team", "year",
    "home_score", "away_score", "venue", "status",
    "closing_spread", "closing_spread_sportsbook",
    "closing_ou", "closing_ou_sportsbook",
    "closing_home_ml", "closing_home_ml_sportsbook",
    "closing_away_ml", "closing_away_ml_sportsbook",
    "opening_ou", "opening_ou_sportsbook",
    "opening_spread", "opening_spread_sportsbook",
    "opening_home_ml", "opening_home_ml_sportsbook",
    "opening_away_ml", "opening_away_ml_sportsbook",
    "has_verified_ou",
    "closing_over_odds", "closing_over_odds_sportsbook",
    "closing_under_odds", "closing_under_odds_sportsbook",
    "closing_spread_home_odds", "closing_spread_home_odds_sportsbook",
    "closing_spread_away_odds", "closing_spread_away_odds_sportsbook",
    "closing_home_implied_probability", "closing_away_implied_probability",
    "opening_over_odds", "opening_over_odds_sportsbook",
    "opening_under_odds", "opening_under_odds_sportsbook",
    "opening_spread_home_odds", "opening_spread_home_odds_sportsbook",
    "opening_spread_away_odds", "opening_spread_away_odds_sportsbook",
    "opening_home_implied_probability", "opening_away_implied_probability",
]

# The old lines all come from nflverse.
OLD_BOOK = "nflverse"


def build_rows(rows):
    """Expand one raw old-row into a consolidated dict (opening == closing)."""
    out = []
    for r in rows:
        d = dict(r)
        gid = d.pop("game_id")
        out.append({
            "game_id": gid,
            "game_time": d.get("game_time"),
            "home_team": d.get("home_team"),
            "away_team": d.get("away_team"),
            "year": d.get("year"),
            "home_score": d.get("home_score"),
            "away_score": d.get("away_score"),
            "venue": d.get("venue"),
            "status": d.get("status"),

            # Closing
            "closing_spread": d.get("closing_spread"),
            "closing_spread_sportsbook": OLD_BOOK,
            "closing_ou": d.get("closing_ou"),
            "closing_ou_sportsbook": OLD_BOOK,
            "closing_home_ml": d.get("closing_home_ml"),
            "closing_home_ml_sportsbook": OLD_BOOK,
            "closing_away_ml": d.get("closing_away_ml"),
            "closing_away_ml_sportsbook": OLD_BOOK,
            "closing_over_odds": d.get("closing_over_odds"),
            "closing_over_odds_sportsbook": OLD_BOOK,
            "closing_under_odds": d.get("closing_under_odds"),
            "closing_under_odds_sportsbook": OLD_BOOK,
            "closing_spread_home_odds": d.get("closing_spread_home_odds"),
            "closing_spread_home_odds_sportsbook": OLD_BOOK,
            "closing_spread_away_odds": d.get("closing_spread_away_odds"),
            "closing_spread_away_odds_sportsbook": OLD_BOOK,
            "closing_home_implied_probability": d.get("closing_home_implied_prob"),
            "closing_away_implied_probability": d.get("closing_away_implied_prob"),

            # Opening — same line as closing (no real opening lines exist)
            "opening_spread": d.get("closing_spread"),
            "opening_spread_sportsbook": OLD_BOOK,
            "opening_ou": d.get("closing_ou"),
            "opening_ou_sportsbook": OLD_BOOK,
            "opening_home_ml": d.get("closing_home_ml"),
            "opening_home_ml_sportsbook": OLD_BOOK,
            "opening_away_ml": d.get("closing_away_ml"),
            "opening_away_ml_sportsbook": OLD_BOOK,
            "opening_over_odds": d.get("closing_over_odds"),
            "opening_over_odds_sportsbook": OLD_BOOK,
            "opening_under_odds": d.get("closing_under_odds"),
            "opening_under_odds_sportsbook": OLD_BOOK,
            "opening_spread_home_odds": d.get("closing_spread_home_odds"),
            "opening_spread_home_odds_sportsbook": OLD_BOOK,
            "opening_spread_away_odds": d.get("closing_spread_away_odds"),
            "opening_spread_away_odds_sportsbook": OLD_BOOK,
            "opening_home_implied_probability": d.get("closing_home_implied_prob"),
            "opening_away_implied_probability": d.get("closing_away_implied_prob"),

            "has_verified_ou": None,
        })
    return out


UPSERT_SQL = """
    INSERT INTO nfl.betting_lines_consolidated
        ({col_list})
    VALUES ({placeholders})
    ON CONFLICT (game_id) DO UPDATE SET
        game_time = EXCLUDED.game_time,
        home_team = EXCLUDED.home_team,
        away_team = EXCLUDED.away_team,
        year = EXCLUDED.year,
        venue = EXCLUDED.venue,
        closing_spread = EXCLUDED.closing_spread,
        closing_spread_sportsbook = EXCLUDED.closing_spread_sportsbook,
        closing_ou = EXCLUDED.closing_ou,
        closing_ou_sportsbook = EXCLUDED.closing_ou_sportsbook,
        closing_home_ml = EXCLUDED.closing_home_ml,
        closing_home_ml_sportsbook = EXCLUDED.closing_home_ml_sportsbook,
        closing_away_ml = EXCLUDED.closing_away_ml,
        closing_away_ml_sportsbook = EXCLUDED.closing_away_ml_sportsbook,
        closing_over_odds = EXCLUDED.closing_over_odds,
        closing_over_odds_sportsbook = EXCLUDED.closing_over_odds_sportsbook,
        closing_under_odds = EXCLUDED.closing_under_odds,
        closing_under_odds_sportsbook = EXCLUDED.closing_under_odds_sportsbook,
        closing_spread_home_odds = EXCLUDED.closing_spread_home_odds,
        closing_spread_home_odds_sportsbook = EXCLUDED.closing_spread_home_odds_sportsbook,
        closing_spread_away_odds = EXCLUDED.closing_spread_away_odds,
        closing_spread_away_odds_sportsbook = EXCLUDED.closing_spread_away_odds_sportsbook,
        closing_home_implied_probability = EXCLUDED.closing_home_implied_probability,
        closing_away_implied_probability = EXCLUDED.closing_away_implied_probability,
        opening_spread = EXCLUDED.opening_spread,
        opening_spread_sportsbook = EXCLUDED.opening_spread_sportsbook,
        opening_ou = EXCLUDED.opening_ou,
        opening_ou_sportsbook = EXCLUDED.opening_ou_sportsbook,
        opening_home_ml = EXCLUDED.opening_home_ml,
        opening_home_ml_sportsbook = EXCLUDED.opening_home_ml_sportsbook,
        opening_away_ml = EXCLUDED.opening_away_ml,
        opening_away_ml_sportsbook = EXCLUDED.opening_away_ml_sportsbook,
        opening_over_odds = EXCLUDED.opening_over_odds,
        opening_over_odds_sportsbook = EXCLUDED.opening_over_odds_sportsbook,
        opening_under_odds = EXCLUDED.opening_under_odds,
        opening_under_odds_sportsbook = EXCLUDED.opening_under_odds_sportsbook,
        opening_spread_home_odds = EXCLUDED.opening_spread_home_odds,
        opening_spread_home_odds_sportsbook = EXCLUDED.opening_spread_home_odds_sportsbook,
        opening_spread_away_odds = EXCLUDED.opening_spread_away_odds,
        opening_spread_away_odds_sportsbook = EXCLUDED.opening_spread_away_odds_sportsbook,
        opening_home_implied_probability = EXCLUDED.opening_home_implied_probability,
        opening_away_implied_probability = EXCLUDED.opening_away_implied_probability,
        home_score = EXCLUDED.home_score,
        away_score = EXCLUDED.away_score,
        status = EXCLUDED.status
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Actually write (backup + upsert). Default is a read-only dry run.")
    ap.add_argument("--game-ids", type=str, default=None,
                    help="Comma-separated game_ids to restrict the migration to (still backs up).")
    args = ap.parse_args()

    gids = [int(x) for x in args.game_ids.split(",")] if args.game_ids else None

    import sqlalchemy as sa

    with engine.begin() as conn:
        old_total = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM nfl.betting_lines_old WHERE is_opening = false"
        )).scalar()
        cons_before = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM nfl.betting_lines_consolidated"
        )).scalar()

        gw = "AND old.game_id = ANY(:gids)" if gids else ""
        params = {"gids": gids} if gids else {}
        sel = sa.text("""
            SELECT old.game_id, g.date AS game_time, home.name AS home_team,
                   away.name AS away_team, s.year, g.home_score, g.away_score,
                   g.venue, g.status::text,
                   (-1 * old.spread)::numeric(6,1) AS closing_spread,
                   old.over_under AS closing_ou,
                   old.home_moneyline AS closing_home_ml,
                   old.away_moneyline AS closing_away_ml,
                   old.spread_home_odds AS closing_spread_home_odds,
                   old.spread_away_odds AS closing_spread_away_odds,
                   old.over_odds AS closing_over_odds,
                   old.under_odds AS closing_under_odds,
                   old.home_implied_probability AS closing_home_implied_prob,
                   old.away_implied_probability AS closing_away_implied_prob,
                   'nflverse' AS sportsbook
            FROM nfl.betting_lines_old old
            JOIN nfl.games g ON g.id = old.game_id
            JOIN nfl.teams home ON home.id = g.home_team_id
            JOIN nfl.teams away ON away.id = g.away_team_id
            JOIN nfl.seasons s ON s.id = g.season_id
            WHERE old.is_opening = false AND old.spread IS NOT NULL
              {gw}
            ORDER BY old.game_id
        """.format(gw=gw))
        rows = conn.execute(sel, params).mappings().all()

        payload = build_rows(rows)

        # Where do these year-game rows currently stand in the target?
        gids_out = [r["game_id"] for r in payload]
        existing = {}
        if gids_out:
            ex = conn.execute(sa.text(
                "SELECT game_id FROM nfl.betting_lines_consolidated WHERE game_id = ANY(:g)"
            ), {"g": gids_out}).fetchall()
            existing = {r[0] for r in ex}

        n_new = sum(1 for g in gids_out if g not in existing)
        n_update = len(gids_out) - n_new

        logger.info(f"Source betting_lines_old (closing): {old_total} rows")
        logger.info(f"Target betting_lines_consolidated before: {cons_before} rows")
        logger.info(f"Migrating {len(payload)} games -> {n_new} new / {n_update} updates (old book: {OLD_BOOK})")

        if not payload:
            logger.info("Nothing to migrate.")
            return

        # Show a few flipped rows for sanity, esp. sign correctness.
        for r in payload[:5]:
            logger.info("  sample: gid=%s year=%s closing_spread=%.1f closing_ou=%.1f home_ml=%s away_ml=%s book=%s",
                        r["game_id"], r["year"], r["closing_spread"], r["closing_ou"],
                        r["closing_home_ml"], r["closing_away_ml"], r["closing_spread_sportsbook"])

        if not args.commit:
            logger.info("DRY RUN — no changes made. Re-run with --commit to apply.")
            return

        # ---- BACKUP (before any write) ----
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_tbl = f"betting_lines_consolidated_backup_{stamp}"
        conn.execute(sa.text(
            f"CREATE TABLE nfl.{backup_tbl} AS TABLE nfl.betting_lines_consolidated"
        ))
        backup_cnt = conn.execute(sa.text(
            f"SELECT COUNT(*)::int FROM nfl.{backup_tbl}"
        )).scalar()
        logger.info(f"BACKUP created: nfl.{backup_tbl} ({backup_cnt} rows)")

        # ---- UPSERT ----
        placeholders = ", ".join(f":{c}" for c in COLS)
        col_list = ", ".join(COLS)
        for p in payload:
            conn.execute(
                sa.text(UPSERT_SQL.format(col_list=col_list, placeholders=placeholders)),
                p,
            )
        logger.info(f"Upserted {len(payload)} rows into nfl.betting_lines_consolidated.")

        after = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM nfl.betting_lines_consolidated"
        )).scalar()
        logger.info(f"Target after: {after} rows (was {cons_before})")
        logger.info("Restore with: DROP TABLE nfl.betting_lines_consolidated; "
                    f"ALTER TABLE nfl.{backup_tbl} RENAME TO betting_lines_consolidated;")

    logger.info("Done.")


if __name__ == "__main__":
    main()
