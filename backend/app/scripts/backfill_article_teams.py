"""Backfill the original_articles.teams column for existing published articles.

For each published article without teams, ask the LLM which teams are mentioned
(most-mentioned first) and store the abbreviation list.
Run:
    cd backend && PYTHONPATH=$PWD ../venv/bin/python app/scripts/backfill_article_teams.py [sport]
"""
from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.services.team_extractor import extract_teams

URL = settings.database_url


async def main() -> None:
    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    engine = create_async_engine(URL, echo=False)
    async with engine.connect() as conn:
        q = "SELECT id, sport, title, content FROM public.original_articles"
        cond = ["teams IS NULL OR teams = '[]'::jsonb"]
        if only:
            cond.append("sport = :sport")
        params = {"sport": only} if only else {}
        result = await conn.execute(text(q + " WHERE " + " AND ".join(cond)), params)
        rows = [dict(r) for r in result.mappings()]
    print(f"backfilling teams for {len(rows)} article(s)")
    done = 0
    for r in rows:
        teams = await extract_teams(r["sport"], r["title"], r["content"])
        if not teams:
            print(f"  - id {r['id']} ({r['sport']}): no teams detected")
            continue
        async with engine.connect() as conn:
            await conn.execute(
                text(
                    "UPDATE public.original_articles SET teams = CAST(:t AS jsonb), "
                    "updated_at = NOW() WHERE id = :id"
                ),
                {"t": json.dumps(teams), "id": r["id"]},
            )
            await conn.commit()
        done += 1
        print(f"  + id {r['id']} ({r['sport']}): {teams}")
    print(f"done. updated {done} article(s) with teams.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
