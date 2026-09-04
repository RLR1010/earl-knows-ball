"""Apply: add original_articles.preview_image (og:image social card URL).

Usage:
    cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/apply_original_articles_preview_image_migration.py

Idempotent: uses ADD COLUMN IF NOT EXISTS.
"""
import asyncio
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import settings


async def main() -> None:
    sql = (Path(__file__).resolve().parents[2]
           / "migrations" / "20260903_original_articles_preview_image.sql").read_text()

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.begin() as conn:
        conn.exec_driver_sql(sql)
    print("preview_image migration applied.")

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='original_articles' "
                "ORDER BY ordinal_position"
            )
        ).fetchall()
        print("original_articles columns:", ", ".join(r[0] for r in rows))


if __name__ == "__main__":
    asyncio.run(main())
