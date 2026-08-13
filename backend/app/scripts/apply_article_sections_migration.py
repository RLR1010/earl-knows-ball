"""Apply the article-sections migration (adds `section` to original_articles + auto_generation_configs)."""
from pathlib import Path
from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260813_article_sections.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}…")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")
    with engine.connect() as conn:
        for table in ("original_articles", "auto_generation_configs"):
            res = conn.execute(
                text(
                    "SELECT column_name, data_type, column_default FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t AND column_name='section'"
                ),
                {"t": table},
            )
            print(f"{table}.section:", [dict(r._mapping) for r in res])


if __name__ == "__main__":
    main()
