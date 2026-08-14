"""Apply the nba.prior_team_stats migration.

Creates the per-team-season prior-stats table used by the NBA data loader to
blend backward-looking features with the previous season for the first games of
a new season (fixes blank/0 features on opening-night games like 37960).
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260814_nba_prior_team_stats.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}...")
            conn.exec_driver_sql(sql_file.read_text())

    # Verify
    with engine.connect() as conn:
        cols = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='nba' AND table_name='prior_team_stats' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()
        print(f"nba.prior_team_stats columns ({len(cols)}):")
        print("  " + ", ".join(r[0] for r in cols))
    print("Done.")


if __name__ == "__main__":
    main()
