"""Apply the original_articles migration via a raw DBAPI connection (supports multi-statement SQL)."""
import asyncio
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260806_add_original_articles.sql"
    )
    sql = sql_path.read_text()

    # Sync engine (psycopg2) executes multi-statement SQL directly.
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    print("Migration applied.")

    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='original_articles' ORDER BY ordinal_position"
            )
        )
        for row in res:
            print(row)


main()
