"""Apply the MLB doubleheader start-time migration (idempotent).

Adds mlb.games.start_time_tbd (BOOLEAN NOT NULL DEFAULT FALSE) so the schedule
can show "TBD" for a doubleheader nightcap whose first pitch the MLB feed has
not set yet, instead of the feed's placeholder (game 1 time + 5 min).

Run:  python -m app.scripts.apply_mlb_games_dh_migration
"""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260925_mlb_games_start_time_tbd.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(sql_file.read_text())
    print("Applied mlb.games.start_time_tbd")
    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, data_type, column_default FROM information_schema.columns "
                "WHERE table_schema='mlb' AND table_name='games' AND column_name='start_time_tbd'"
            )
        )
        for row in res:
            print("column:", row)
    engine.dispose()


main()
