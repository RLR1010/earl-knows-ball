"""Apply ALTER TABLE migrations (multi-statement SQL via sync psycopg2)."""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    migrations = [
        root / "migrations" / "20260806_add_article_research_trace.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in migrations:
            print(f"Applying {sql_file.name}…")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")

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
