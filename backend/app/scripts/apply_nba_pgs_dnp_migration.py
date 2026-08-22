"""Apply the 20260822_nba_pgs_dnp migration. Idempotent (ADD COLUMN IF NOT EXISTS).

Adds nba.player_game_stats.dnp (boolean) + dnp_reason (varchar) and sets up the
columns that the ESPN active/inactive backfill (backfill_nba_game_active_inactive.py)
populates. is_starter already exists; it just needs populating (currently all NULL).
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260822_nba_pgs_dnp.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.execute(text(sql_file.read_text()))
    print("Migration applied.")
    with engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='nba' AND table_name='player_game_stats' "
                "AND column_name IN ('dnp','dnp_reason') ORDER BY column_name"
            )
        ).scalars().all()
        print(f"  nba.player_game_stats new columns present: {cols}")


if __name__ == "__main__":
    main()
