"""Apply the nfl.qb_badweather_stats migration. Idempotent (CREATE IF NOT EXISTS).

Creates nfl.qb_badweather_stats: per-QB cold/precipitation passer rating from
PRIOR starts only (leak-free), mirroring team_badweather_stats for the data
loader's resolved starter join.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_nfl_qb_badweather_stats.sql"
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
                "WHERE table_schema='nfl' AND table_name='qb_badweather_stats'"
            )
        ).scalar()
        print(f"  nfl.qb_badweather_stats columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
