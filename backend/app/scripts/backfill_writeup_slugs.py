"""Backfill SEO-friendly slugs for existing public writeups in mlb/nfl/nba.game_writeups.

Slug pattern (mirrors Original Articles): YYYY-MM-DD-<title-slug> using each game's
scheduled date + slugified title. Within a sport, ensures uniqueness by appending -2, -3, ...

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_writeup_slugs.py
"""
import asyncio
import re
from pathlib import Path

from sqlalchemy import text, create_engine, select

from app.core.config import settings

SCHEMAS = ["mlb", "nfl", "nba"]


def slugify_title(title: str) -> str:
    t = (title or "").strip().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    t = re.sub(r"-{2,}", "-", t)
    return t[:80]  # keep date + core slug within sane length


async def main():
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)

    for schema in SCHEMAS:
        with engine.begin() as conn:
            # Get writeups needing a slug, joined to their game date.
            rows = conn.execute(
                text(
                    f"""
                    SELECT w.id, w.title, COALESCE(g.date, w.updated_at) AS gdate
                    FROM {schema}.game_writeups w
                    LEFT JOIN {schema}.games g ON g.id = w.game_id
                    WHERE w.slug IS NULL OR w.slug = ''
                    ORDER BY w.id
                    """
                )
            ).fetchall()

        # Pre-fetch existing slugs in this sport to avoid collisions.
        with engine.begin() as conn:
            existing = {
                r[0]
                for r in conn.execute(text(f"SELECT slug FROM {schema}.game_writeups WHERE slug IS NOT NULL")).fetchall()
                if r[0]
            }

        updated = 0
        for wid, title, gdate in rows:
            date_part = gdate.strftime("%Y-%m-%d") if gdate else ""
            base = slugify_title(title or "")
            if not base:
                base = f"writeup-{wid}"
            slug = f"{date_part}-{base}" if date_part else base
            # De-dup within sport.
            cand, i = slug, 2
            while cand in existing:
                cand = f"{slug}-{i}"
                i += 1
            existing.add(cand)
            with engine.begin() as conn:
                conn.execute(
                    text(f"UPDATE {schema}.game_writeups SET slug = :s WHERE id = :i"),
                    {"s": cand, "i": wid},
                )
            updated += 1
        print(f"{schema}: generated {updated} slug(s)")

    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
