"""Apply the 20260823_plan_kind_token_amount migration. Idempotent."""
from pathlib import Path
from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260823_plan_kind_token_amount.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.execute(text(sql_file.read_text()))
    print("Migration applied.")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='subscription_plans' "
            "AND column_name IN ('kind','token_amount')"
        )).fetchall()
        print(f"  subscription_plans {[r[0] for r in rows]}")


if __name__ == "__main__":
    main()
