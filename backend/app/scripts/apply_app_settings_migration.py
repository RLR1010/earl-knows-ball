"""Idempotent migration: create public.app_settings and seed free-chat defaults.

Run:  cd backend && PYTHONPATH=. ../venv/bin/python app/scripts/apply_app_settings_migration.py

Safe to re-run; never overwrites an existing value.
"""

import asyncio

import asyncpg

from app.core.config import settings

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.app_settings (
    key        varchar(64) PRIMARY KEY,
    value      text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
)
"""

SEED = [
    ("***", "true"),
    ("free_c…kens", "100000"),
]


async def main() -> None:
    dsn = settings.database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(CREATE_TABLE)
        for key, value in SEED:
            await conn.execute(
                "INSERT INTO public.app_settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO NOTHING",
                key,
                value,
            )
        rows = await conn.fetch("SELECT key, value FROM public.app_settings ORDER BY key")
        print("public.app_settings:")
        for r in rows:
            print(f"  {r['key']} = {r['value']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
