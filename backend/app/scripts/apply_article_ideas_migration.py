"""Apply the article_ideas migration (multi-statement SQL via sync psycopg2)."""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260808_add_article_ideas.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}…")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")

    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, data_type, column_default FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='article_ideas' ORDER BY ordinal_position"
            )
        )
        print("article_ideas columns:")
        for row in res:
            print("  ", row._mapping)


main()
