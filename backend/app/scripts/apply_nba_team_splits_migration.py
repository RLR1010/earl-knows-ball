"""Apply the nba.team_splits migration. Idempotent (CREATE IF NOT EXISTS).

Creates nba.team_splits: per-team, per-split-type form (home/away, vs East/West)
plus ATS/O/U records derived from nba.games x nba.betting_lines.
season_id NULL = career aggregate.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_nba_team_splits.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    with engine.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='nba' AND table_name='team_splits'"
            )
        ).scalar()
        print(f"  nba.team_splits columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
