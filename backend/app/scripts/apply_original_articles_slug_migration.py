"""Apply the original_articles slug migration and backfill slugs for existing rows."""
from pathlib import Path
import re
from datetime import timezone

from sqlalchemy import text, create_engine
from app.core.config import settings


def slugify_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t[:80] or "article"


def main():
    msql = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260806_original_articles_slug.sql"
    ).read_text()

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)

    with engine.begin() as conn:
        conn.exec_driver_sql(msql)
    print("Slug migration applied.")

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, sport, title, published_at FROM public.original_articles "
                "ORDER BY id"
            )
        ).fetchall()
        for r in rows:
            aid, sport, title, published_at = r
            # Date prefix: from published_at, else created_at.
            dt = published_at
            if dt is None:
                got = conn.execute(
                    text("SELECT created_at FROM public.original_articles WHERE id=:i"),
                    {"i": aid},
                ).scalar()
                dt = got
            date_prefix = (
                dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
                if hasattr(dt, "astimezone")
                else (dt.strftime("%Y-%m-%d") if dt else "0000-00-00")
            )
            base = f"{date_prefix}-{slugify_title(title)}"
            # Guarantee uniqueness per sport by appending the id if needed.
            cand = base
            dup = True
            counter = 0
            while dup:
                existing = conn.execute(
                    text(
                        "SELECT 1 FROM public.original_articles "
                        "WHERE sport=:s AND slug=:sl AND id<>:i LIMIT 1"
                    ),
                    {"s": sport, "sl": cand, "i": aid},
                ).scalar()
                if existing is None:
                    dup = False
                else:
                    counter += 1
                    cand = f"{base}-{counter}"
            conn.execute(
                text("UPDATE public.original_articles SET slug=:sl WHERE id=:i"),
                {"sl": cand, "i": aid},
            )
        print(f"Backfilled slugs for {len(rows)} rows.")


main()
