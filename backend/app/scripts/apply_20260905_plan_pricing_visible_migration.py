"""Apply the 20260905_plan_pricing_visible migration. Idempotent.

Adds public.subscription_plans.pricing_visible (BOOLEAN NOT NULL DEFAULT TRUE)
so admins can toggle whether a plan's card is shown in any public plan-offer
surface (/pricing page + subscribe gating modal). Default TRUE = every plan stays
visible until an admin turns it off. Hiding never affects active subscriptions or
direct checkout links for that plan.
"""

from pathlib import Path

from sqlalchemy import text, create_engine

from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_file = root / "migrations" / "20260905_plan_pricing_visible.sql"
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        print(f"Applying {sql_file.name}...")
        conn.execute(text(sql_file.read_text()))
    print("Migration applied.")
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='subscription_plans' "
                "AND column_name='pricing_visible'"
            )
        ).fetchall()
        print(f"  public.subscription_plans.pricing_visible present: {rows}")


if __name__ == "__main__":
    main()
