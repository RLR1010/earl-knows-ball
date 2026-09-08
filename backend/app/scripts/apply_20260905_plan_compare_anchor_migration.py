"""Apply the 20260905_plan_compare_anchor migration. Idempotent.

Adds public.subscription_plans.compare_against_monthly_cents (INTEGER, nullable)
and seeds premium-annual with 2995 (the $29.95/mo membership anchor used for the
dynamic "save X%" badge on the pricing page).
"""

from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260905_plan_compare_anchor.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.execute(text(sql_file.read_text()))
    print("Migration applied.")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT slug, compare_against_monthly_cents "
                "FROM public.subscription_plans WHERE slug = 'premium-annual'"
            )
        ).fetchall()
        print(f"  premium-annual compare_against_monthly_cents = {rows}")


if __name__ == "__main__":
    main()
