"""Fast deterministic SEO backfill for game writeups missing SEO fields.

Generates a non-empty meta description from the title + body without any LLM
calls, so every row gets populated immediately. New writeups already get
LLM-quality SEO (with the same fallback) via the base generator.

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_writeup_seo.py
"""
from sqlalchemy import text, create_engine

from app.core.config import settings

SCHEMAS = ["mlb", "nfl", "nba"]


def fallback_description(title: str, body: str) -> str:
    """Title-first meta description (always non-empty)."""
    title = (title or "").strip()
    body_plain = " ".join((body or "").split())
    if title:
        return title[:160]
    if body_plain:
        return body_plain[:160]
    return ""


def fallback_keywords(title: str, body: str) -> str:
    """Extract a short keyword list from title + leading body words."""
    import re

    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']*", (title or "") + " " + (body or ""))
    seen = []
    for w in words:
        lw = w.lower()
        if lw in seen or len(lw) < 4:
            continue
        seen.append(lw)
        if len(seen) >= 8:
            break
    return ", ".join(seen[:8])


def main():
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    grand_total = 0
    for schema in SCHEMAS:
        with engine.begin() as conn:
            rows = conn.execute(
                text(
                    f"""
                    SELECT id, title, public_content, premium_content
                    FROM {schema}.game_writeups
                    WHERE seo_description IS NULL OR seo_description = ''
                    OR seo_keywords IS NULL OR seo_keywords = ''
                    ORDER BY id
                    """
                )
            ).fetchall()
        updated = 0
        for rid, title, pub, prem in rows:
            body = (pub or "") + "\n" + (prem or "")
            desc = fallback_description(title, body)
            kw = fallback_keywords(title, body)
            if not desc and not kw:
                continue
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {schema}.game_writeups SET seo_description = COALESCE(NULLIF(seo_description, ''), :d), seo_keywords = COALESCE(NULLIF(seo_keywords, ''), :k) WHERE id = :i"
                    ),
                    {"d": desc, "k": kw or None, "i": rid},
                )
            updated += 1
        print(f"{schema}: updated {updated} writeups")
        grand_total += updated
    print(f"Total updated: {grand_total}")


if __name__ == "__main__":
    main()
