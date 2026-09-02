"""Apply the paid-trial migration (trial_fee_price_id column + premium-trial plan seed).

Multi-statement SQL via sync psycopg2. Idempotent (IF NOT EXISTS / ON CONFLICT).
Run from backend dir:
    PYTHONPATH=. ../venv/bin/python app/scripts/apply_paid_trial_migration.py
"""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    sql_files = [
        root / "migrations" / "20260901_add_paid_trial_plan.sql",
    ]
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            print(f"Applying {sql_file.name}…")
            conn.exec_driver_sql(sql_file.read_text())
    print("Migrations applied.")

    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT slug, name, price_cents, trial_days, stripe_price_id, "
                "trial_fee_price_id, is_active FROM public.subscription_plans "
                "WHERE slug = 'premium-trial'"
            )
        )
        print("premium-trial plan:", res.first())


main()
