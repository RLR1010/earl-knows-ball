"""Apply the auto_generation_configs migration (multi-statement SQL via sync psycopg2)."""
from pathlib import Path
from sqlalchemy import text, create_engine
from app.core.config import settings

def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260808_add_auto_generation_configs.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}…")
        conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")
    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='auto_generation_configs' "
                "ORDER BY ordinal_position"
            )
        )
        print("auto_generation_configs columns:")
        for row in res:
            print("  ", row._mapping)

main()
