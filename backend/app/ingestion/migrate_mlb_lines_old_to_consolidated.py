"""
Migrate historical MLB lines from mlb.betting_lines_old -> mlb.betting_lines_consolidated.

WHY: mlb.betting_lines_consolidated only holds lines from 2021 onward (the live
mlb.betting_lines table starts there). mlb.betting_lines_old holds the historical
backfill for 2012-2020 (16,551 rows, Kaggle Vegas opening lines), which the model
needs so training is not starved for those years.

SAFETY:
  * Backs up mlb.betting_lines_consolidated to a timestamped table first
    (mlb.betting_lines_consolidated_backup_YYYYMMDD_HHMMSS) BEFORE any writes.
  * Idempotent upsert keyed on game_id (ON CONFLICT (game_id) DO UPDATE).
  * Dry-run by default (--commit to actually write).

OPENING/OFF the old table is all is_opening=True (opening lines). There is NO
separate closing set for 2012-2020, so we populate opening_* from the data and
MIRROR the same values into closing_* (best available; a game always has a line).
Book is tagged 'kaggle' (source of the historical backfill).

Usage:
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/migrate_mlb_lines_old_to_consolidated.py
        (dry run, no writes)
    PYTHONPATH=$PWD ../venv/bin/python app/ingestion/migrate_mlb_lines_old_to_consolidated.py --commit
        (backup + migrate)
"""
import argparse
import logging
from datetime import datetime

import sqlalchemy as sa
from app.database import engine

logger = logging.getLogger("earl.migrate_mlb_lines_old")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

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

BOOK = "kaggle"


def build_rows(rows):
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
            "closing_spread": d.get("spread"),
            "closing_spread_sportsbook": BOOK,
            "closing_ou": d.get("over_under"),
            "closing_ou_sportsbook": BOOK,
            "closing_home_ml": d.get("home_moneyline"),
            "closing_home_ml_sportsbook": BOOK,
            "closing_away_ml": d.get("away_moneyline"),
            "closing_away_ml_sportsbook": BOOK,
            "closing_over_odds": d.get("over_odds"),
            "closing_over_odds_sportsbook": BOOK,
            "closing_under_odds": d.get("under_odds"),
            "closing_under_odds_sportsbook": BOOK,
            "closing_spread_home_odds": d.get("spread_home_odds"),
            "closing_spread_home_odds_sportsbook": BOOK,
            "closing_spread_away_odds": d.get("spread_away_odds"),
            "closing_spread_away_odds_sportsbook": BOOK,
            "closing_home_implied_probability": None,
            "closing_away_implied_probability": None,
            "opening_spread": d.get("spread"),
            "opening_spread_sportsbook": BOOK,
            "opening_ou": d.get("over_under"),
            "opening_ou_sportsbook": BOOK,
            "opening_home_ml": d.get("home_moneyline"),
            "opening_home_ml_sportsbook": BOOK,
            "opening_away_ml": d.get("away_moneyline"),
            "opening_away_ml_sportsbook": BOOK,
            "opening_over_odds": d.get("over_odds"),
            "opening_over_odds_sportsbook": BOOK,
            "opening_under_odds": d.get("under_odds"),
            "opening_under_odds_sportsbook": BOOK,
            "opening_spread_home_odds": d.get("spread_home_odds"),
            "opening_spread_home_odds_sportsbook": BOOK,
            "opening_spread_away_odds": d.get("spread_away_odds"),
            "opening_spread_away_odds_sportsbook": BOOK,
            "opening_home_implied_probability": None,
            "opening_away_implied_probability": None,
            "has_verified_ou": None,
        })
    return out


UPSERT_SQL = """
    INSERT INTO mlb.betting_lines_consolidated ({col_list})
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
    ap.add_argument("--commit", action="store_true", help="Backup + migrate. Default: read-only dry run.")
    ap.add_argument("--game-ids", type=str, default=None, help="Comma-separated game_ids to restrict to.")
    args = ap.parse_args()
    gids = [int(x) for x in args.game_ids.split(",")] if args.game_ids else None

    with engine.begin() as conn:
        old_total = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM mlb.betting_lines_old WHERE is_opening = true"
        )).scalar()
        cons_before = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM mlb.betting_lines_consolidated"
        )).scalar()

        gw = "AND old.game_id = ANY(:gids)" if gids else ""
        params = {"gids": gids} if gids else {}
        sel = sa.text("""
            SELECT old.game_id, g.date AS game_time, home.name AS home_team,
                   away.name AS away_team, s.year, g.home_score, g.away_score,
                   g.venue, g.status::text,
                   old.spread AS closing_spread, old.spread AS spread,
                   old.over_under, old.home_moneyline, old.away_moneyline,
                   old.spread_home_odds, old.spread_away_odds,
                   old.over_odds, old.under_odds,
                   old.home_implied_probability, old.away_implied_probability,
                   'kaggle' AS sportsbook
            FROM mlb.betting_lines_old old
            JOIN mlb.games g ON g.id = old.game_id
            JOIN mlb.teams home ON home.id = g.home_team_id
            JOIN mlb.teams away ON away.id = g.away_team_id
            JOIN mlb.seasons s ON s.id = g.season_id
            WHERE old.is_opening = true AND old.spread IS NOT NULL
              {gw}
            ORDER BY old.game_id
        """.format(gw=gw))
        rows = conn.execute(sel, params).mappings().all()
        payload = build_rows(rows)

        gids_out = [r["game_id"] for r in payload]
        existing = set()
        if gids_out:
            ex = conn.execute(sa.text(
                "SELECT game_id FROM mlb.betting_lines_consolidated WHERE game_id = ANY(:g)"
            ), {"g": gids_out}).fetchall()
            existing = {r[0] for r in ex}
        n_new = sum(1 for g in gids_out if g not in existing)
        n_update = len(gids_out) - n_new

        logger.info(f"Source mlb.betting_lines_old (opening): {old_total} rows")
        logger.info(f"Target mlb.betting_lines_consolidated before: {cons_before} rows")
        logger.info(f"Migrating {len(payload)} games -> {n_new} new / {n_update} updates (book: {BOOK})")

        if not payload:
            logger.info("Nothing to migrate.")
            return

        for r in payload[:5]:
            logger.info("  sample: gid=%s year=%s spread=%.1f ou=%.1f homeML=%s awayML=%s",
                        r["game_id"], r["year"], r["closing_spread"], r["closing_ou"],
                        r["closing_home_ml"], r["closing_away_ml"])

        if not args.commit:
            logger.info("DRY RUN — no changes made. Re-run with --commit to apply.")
            return

        # ---- BACKUP before any write ----
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_tbl = f"betting_lines_consolidated_backup_{stamp}"
        conn.execute(sa.text(f"CREATE TABLE mlb.{backup_tbl} AS TABLE mlb.betting_lines_consolidated"))
        backup_cnt = conn.execute(sa.text(f"SELECT COUNT(*)::int FROM mlb.{backup_tbl}")).scalar()
        logger.info(f"BACKUP created: mlb.{backup_tbl} ({backup_cnt} rows)")

        # ---- UPSERT ----
        col_list = ", ".join(COLS)
        placeholders = ", ".join(f":{c}" for c in COLS)
        for p in payload:
            conn.execute(sa.text(UPSERT_SQL.format(col_list=col_list, placeholders=placeholders)), p)
        logger.info(f"Upserted {len(payload)} rows into mlb.betting_lines_consolidated.")

        after = conn.execute(sa.text(
            "SELECT COUNT(*)::int FROM mlb.betting_lines_consolidated"
        )).scalar()
        logger.info(f"Target after: {after} rows (was {cons_before})")
        logger.info("Restore with: DROP TABLE mlb.betting_lines_consolidated; "
                    f"ALTER TABLE mlb.{backup_tbl} RENAME TO betting_lines_consolidated;")

    logger.info("Done.")


if __name__ == "__main__":
    main()
