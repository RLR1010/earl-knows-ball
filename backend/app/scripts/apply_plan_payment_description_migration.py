"""Apply the 20260824_plan_payment_description migration. Idempotent.

Adds public.subscription_plans.payment_description (TEXT, nullable) so admins
can set the label shown in a user's payment history for membership charges.
"""

from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260824_plan_payment_description.sql"
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
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='subscription_plans' "
                "AND column_name='payment_description'"
            )
        ).fetchall()
        print(f"  public.subscription_plans.payment_description present: {rows}")


if __name__ == "__main__":
    main()
