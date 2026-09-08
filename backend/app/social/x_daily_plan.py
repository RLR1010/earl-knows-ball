"""x_daily_plan — persists today's full planned-tweet lineup (the old 6 daily posts)
into public.x_daily_plan so admin/social/x > "Post Today" shows a stable, copyable set.

Run once per day (09:20 CT cron, via task_config task_type=subprocess). It does NOT post
to X and does NOT stamp any source row — it only snapshots what the day's tweets WOULD be,
so Rich can copy the captions/links and post them manually from the Post Today tab.

Usage: python -m app.social.x_daily_plan            # generate + persist today's plan
       python -m app.social.x_daily_plan --print    # print today's plan (no write)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime

from sqlalchemy import text

from app.core.config import settings
from app.social import daily_tweet as dt

logger = logging.getLogger("x_daily_plan")


def _eng():
    return dt._eng()


def _today_central() -> date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:
        return datetime.now(dt.CENTRAL).date()


def generate(commit: bool = True, max_items: int | None = None) -> dict:
    """Replace today's persisted lineup with a freshly-generated plan. No X call, no stamp."""
    eng = _eng()
    plan_date = _today_central()
    # 1) latest full-day plan from the existing daily_tweet picker (authoritative, no post)
    res = dt.plan_day()
    items = res.get("plan", [])
    if max_items is not None:
        items = items[:max_items]
    # also expose the limiter/spare info for context
    data = {
        "plan_date": plan_date.isoformat(),
        "generated": date.today().isoformat(),
        "today_counts": res.get("today_counts"),
        "pool": res.get("pool"),
        "plan": items,
    }
    if not commit:
        return data
    with eng.begin() as c:
        c.execute(text("DELETE FROM public.x_daily_plan WHERE plan_date = :d"), {"d": plan_date})
        for i, it in enumerate(items, start=1):
            c.execute(text(
                "INSERT INTO public.x_daily_plan"
                " (plan_date, slot, kind, sport, item_id, title, url, text, created_at)"
                " VALUES (:d, :slot, :kind, :sport, :item_id, :title, :url, :txt, now())"
            ), {
                "d": plan_date, "slot": i,
                "kind": it.get("kind"), "sport": it.get("sport") or None,
                "item_id": it.get("id"), "title": it.get("title") or it.get("source_title"),
                "url": it.get("url") or "", "txt": it.get("text") or it.get("caption") or "",
            })
    logger.info("x_daily_plan: persisted %d rows for %s", len(items), plan_date)
    return data


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", action="store_true", help="print today's plan, don't write")
    ap.add_argument("--max", type=int, default=None, help="cap how many items to persist")
    a = ap.parse_args()
    data = generate(commit=not a.print, max_items=a.max)
    print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
