"""Apply the customer-service migration (creates cs_messages + cs_knowledge tables)."""
from pathlib import Path
from sqlalchemy import create_engine, text
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260814_customer_service.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}…")
        conn.exec_driver_sql(sql_file.read_text())
    with engine.connect() as conn:
        for table in ("cs_messages", "cs_knowledge"):
            res = conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=:t ORDER BY ordinal_position"
                ),
                {"t": table},
            )
            print(f"{table}: {[(r['column_name'], r['data_type']) for r in res.mappings()]}")
    print("Customer-service migration applied.")


if __name__ == "__main__":
    main()
