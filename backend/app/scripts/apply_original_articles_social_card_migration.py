"""Apply the original_articles social-card migration in production.

Adds card_accent + social_caption + preview_image columns to
public.original_articles (idempotent ADD COLUMN IF NOT EXISTS). Mirrors the
metadata/usage migration pattern used on the production compute box. NOTE:
preview_image is authored as part of the social-card vertical slice and must
exist for the shipped original_articles.py SELECT/INSERT/UPDATE paths.
"""
from pathlib import Path

from sqlalchemy import text, create_engine
from app.core.config import settings


def main():
    root = Path(__file__).resolve().parents[2]
    cols = {
        "card_accent": root / "migrations" / "20260904_original_articles_card_accent.sql",
        "social_caption": root / "migrations" / "20260904_original_articles_social_caption.sql",
        # preview_image has no committed SQL file yet; inline here (idempotent).
        "preview_image": "ALTER TABLE public.original_articles ADD COLUMN IF NOT EXISTS preview_image TEXT",
    }
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    for name, sql in cols.items():
        with engine.begin() as conn:
            if hasattr(sql, "read_text"):
                sql = sql.read_text()
            conn.exec_driver_sql(sql)
        print(f"Applied {name}")

    with engine.connect() as conn:
        res = conn.execute(
            text(
                "SELECT column_name, is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='original_articles' "
                "AND column_name IN ('card_accent','social_caption') ORDER BY ordinal_position"
            )
        )
        for row in res:
            print("column:", row)

    engine.dispose()


main()
