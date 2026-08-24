"""Apply the 20260823_extra_token_balance migration. Idempotent.

Adds public.users.extra_token_balance (BIGINT, NOT NULL DEFAULT 0) for
one-time purchased token top-ups that roll over between billing periods.
"""
from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260823_extra_token_balance.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.execute(text(sql_file.read_text()))
    print("Migration applied.")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, column_default "
                "FROM information_schema.columns WHERE table_schema='public' "
                "AND table_name='users' AND column_name='extra_token_balance'"
            )
        ).fetchall()
        print(f"  public.users.extra_token_balance present: {rows}")


if __name__ == "__main__":
    main()
