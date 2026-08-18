"""Apply the NBA betting_lines_consolidated closing_spread integrity migration.

Backfills missing/NaN closing_spread for final games from opening_spread,
normalizes status casing, and adds a CHECK constraint enforcing that final
games always have a non-NULL, non-NaN closing_spread.
"""
from pathlib import Path
import psycopg2
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260816_nba_betting_closing_spread_integrity.sql"
    # psycopg2 sync DSN from the async settings URL (strip the +psycopg2 dialect)
    dsn = settings.database_url.replace("+asyncpg", "")
    print(f"Applying {sql_file.name} …")
    with psycopg2.connect(dsn) as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            # psycopg2 runs the whole multi-statement script as a single simple
            # query (no bound params), so the DO $$...$$ block is preserved verbatim.
            cur.execute(sql_file.read_text())
            conn.commit()
    # Post-checks
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM nba.betting_lines_consolidated "
                "WHERE status IS NOT NULL AND lower(status)='final' "
                "AND (closing_spread IS NULL OR closing_spread='NaN'::numeric)"
            )
            missing = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM nba.betting_lines_consolidated")
            total = cur.fetchone()[0]
            print(f"Remaining final games missing closing_spread: {missing} (of {total} total rows)")
            cur.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname='betting_lines_consolidated_closing_spread_notnull_chk'"
            )
            print("Constraint:", cur.fetchall())
            cur.execute(
                "SELECT game_id, year, home_team, away_team, opening_spread, closing_spread "
                "FROM nba.betting_lines_consolidated "
                "WHERE closing_spread = opening_spread "
                "AND game_id IN (48713, 48714, 48715) ORDER BY game_id"
            )
            print("Sample backfilled rows:", cur.fetchall())
    print("Migration applied.")


if __name__ == "__main__":
    main()
