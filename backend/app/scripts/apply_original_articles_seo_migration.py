"""Apply the original_articles SEO migration and backfill SEO meta for existing rows.

Generates a meta description + keyword tags for every row that doesn't have them
yet, using the DeepSeek LLM, then writes them to public.original_articles
(seo_description, seo_keywords).

Usage:
  cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/apply_original_articles_seo_migration.py
"""
import asyncio
from pathlib import Path

from openai import AsyncOpenAI
from sqlalchemy import text, create_engine

from app.core.config import settings

SEO_SYSTEM_PROMPT = (
    "You are an SEO specialist for a premium sports handicapping site "
    "(Earl Knows Ball). Given an article's title, summary, and body, produce "
    "two things only: a compelling meta description and a comma-separated list "
    "of 5-8 SEO keywords.\n"
    "Rules:\n"
    "- Meta description: 140-160 characters, one or two punchy sentences that "
    "  summarize the article and entice clicks. No quotes, no trailing period "
    "  if it pushes past the limit. Plain text only.\n"
    "- Keywords: comma-separated, lowercase, no spaces after commas, no trailing "
    "  comma. Use phrases a bettor would search, e.g. "
    "  'mlb betting picks, padres vs dodgers, over under odds, sportsbook analysis'.\n"
    "- Return ONLY a JSON object with exactly these keys: {\"seo_description\": "
    "\"...\", \"seo_keywords\": \"...\"}. No markdown fences, no commentary."
)


async def generate_seo(title: str, summary: str, content: str) -> dict[str, str]:
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=30.0,
    )
    body = (content or "")[:4000]
    resp = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": SEO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"TITLE:\n{title}\n\nSUMMARY:\n{summary}\n\nBODY (truncated):\n{body}",
            },
        ],
        temperature=0.3,
        max_tokens=600,
        extra_body={"response_format": {"type": "json_object"}},
    )
    raw = resp.choices[0].message.content or ""
    data = _extract_json(raw)
    return {
        "seo_description": (data.get("seo_description") or "").strip()[:500],
        "seo_keywords": (data.get("seo_keywords") or "").strip()[:500],
    }


def _extract_json(raw: str) -> dict:
    """Best-effort parse of an LLM JSON response (handles markdown fences)."""
    import json
    import re

    if not raw:
        return {}
    # Strip markdown code fences: ```json ... ```
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    # Try to pull out the first balanced JSON object.
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except Exception:
                        break
    return {}


async def main():
    msql = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "20260806_original_articles_seo.sql"
    ).read_text()
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)

    with engine.begin() as conn:
        conn.exec_driver_sql(msql)
    print("SEO migration applied.")

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, sport, title, summary, content FROM public.original_articles "
                "WHERE seo_description IS NULL OR seo_keywords IS NULL "
                "OR seo_description = '' OR seo_keywords = '' "
                "ORDER BY id"
            )
        ).fetchall()

    total = len(rows)
    print(f"Generating SEO for {total} article(s)...")
    for i, (aid, sport, title, summary, content) in enumerate(rows, 1):
        try:
            seo = await generate_seo(title, summary, content)
        except Exception as e:  # noqa: BLE001
            print(f"  [{i}/{total}] id={aid} ERROR: {e}")
            continue
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE public.original_articles SET seo_description=:d, "
                    "seo_keywords=:k WHERE id=:i"
                ),
                {"d": seo["seo_description"], "k": seo["seo_keywords"], "i": aid},
            )
        print(
            f"  [{i}/{total}] id={aid} ({sport}) desc={len(seo['seo_description'])}ch "
            f"kw={seo['seo_keywords']!r}"
        )
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
