"""Apply the original_articles usage_json migration (per-call token breakdown)."""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260808_original_articles_usage.sql"
    )
    sql = sql_path.read_text()

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    print("Migration applied.")

    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='original_articles' "
                "AND column_name='usage_json'"
            )
        )
        for row in res:
            print(row)
    rows = res.all()
    if not rows:
        print("WARNING: usage_json column not found.")


main()
