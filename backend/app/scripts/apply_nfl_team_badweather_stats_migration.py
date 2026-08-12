"""Apply the nfl.team_badweather_stats migration. Idempotent (CREATE IF NOT EXISTS).

Creates nfl.team_badweather_stats: per-team per-game cold/precipitation
PPG/YPG/win% from PRIOR games (leak-free), mirroring team_rolling_stats so the
data loader joins it the same way.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260812_nfl_team_badweather_stats.sql"
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
                "WHERE table_schema='nfl' AND table_name='team_badweather_stats'"
            )
        ).scalar()
        print(f"  nfl.team_badweather_stats columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
