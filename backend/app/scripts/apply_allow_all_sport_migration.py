"""Apply the 'allow all sport' migration (adds 'all' to original_articles + article_ideas sport column)."""
from pathlib import Path
from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260813_allow_all_sport.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}…")
        conn.exec_driver_sql(sql_file.read_text())
    with engine.connect() as conn:
        for table in ("original_articles", "article_ideas"):
            res = conn.execute(
                text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname=:c"
                ),
                {"c": f"{table}_sport_check"},
            )
            print(f"{table}:", [dict(r._mapping) for r in res])
    print("Migrations applied.")


if __name__ == "__main__":
    main()
