import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/home/rich/.openclaw/workspace/earl-knows-football/backend")

from sqlalchemy import text

from app.database import async_session
from app.routers.original_articles import _verify_original_accuracy


async def fix(broken_ids: list[int]):
    async with async_session() as db:
        rows = []
        for aid in broken_ids:
            r = await db.execute(
                text(
                    "SELECT id, title, content, research_json, visibility "
                    "FROM public.original_articles WHERE id=:id"
                ),
                {"id": aid},
            )
            row = r.mappings().first()
            if not row:
                print(f"  {aid}: not found")
                continue
            research_json = row["research_json"]
            brief = None
            if isinstance(research_json, str):
                try:
                    brief = json.loads(research_json)
                except Exception:
                    brief = None
            if isinstance(brief, dict):
                brief = json.dumps(brief, ensure_ascii=False)[:40000]

            usage: list[dict] = []
            ac, tokens = await _verify_original_accuracy(
                row["title"],
                row["content"],
                [],
                visibility=row["visibility"],
                research_brief=brief,
                usage_log=usage,
            )
            # Mirror the caller's flag derivation (Fix 2).
            has_findings = bool(ac.get("findings")) and not ac.get("skipped")
            check_failed = (not ac.get("passed")) and not ac.get("skipped")
            ac["retries_used"] = 0
            ac["has_inaccuracy"] = bool(has_findings or check_failed)
            ac["accuracy_pass"] = not bool(has_findings or check_failed)
            ac_json = json.dumps(ac, ensure_ascii=False)
            await db.execute(
                text(
                    "UPDATE public.original_articles SET accuracy_check=:ac "
                    "WHERE id=:id"
                ),
                {"ac": ac_json, "id": aid},
            )
            print(
                f"  {aid}: passed={ac.get('passed')} raw_len={len(ac.get('raw') or '')} "
                f"findings={ac.get('findings')} error={ac.get('error')}"
            )
            rows.append(aid)
        await db.commit()
        print("updated:", rows)


if __name__ == "__main__":
    asyncio.run(fix([int(x) for x in sys.argv[1:]]))
