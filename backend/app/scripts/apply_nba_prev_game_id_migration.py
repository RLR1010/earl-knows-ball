"""Apply the 20260821_nba_prev_game_id migration. Idempotent (IF NOT EXISTS).

Adds prev_game_id / prev_game_date (+ _season / _side pointer variants) columns and
indexes to nba.team_rolling_stats and nba.cumulative_game_stats, so the NBA data loader
can resolve "the team's previous game" with indexed equality lookups (prev_game_id ->
game_id) instead of correlated ORDER BY ... LIMIT 1 date scans.

Columns are nullable; populated by the builders' LAG() windows on the next
populate_team_rolling_stats / cumulative_stats rebuild (idempotent).
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260821_nba_prev_game_id.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migration applied.")

    with engine.connect() as conn:
        for t in ["team_rolling_stats", "cumulative_game_stats"]:
            n = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema='nba' AND table_name=:t "
                    "AND column_name LIKE 'prev_%'"
                ),
                {"t": t},
            ).scalar()
            print(f"  nba.{t} prev_* columns = {n}")
    print("Done.")


if __name__ == "__main__":
    main()
