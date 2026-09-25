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


async def _ensure_fresh_original_cards_async(limit: int = 9) -> int:
    """Best-effort: make fresh (<= ORIGINAL_MAX_AGE_HOURS) public, published,
    still-unposted original articles tweet-ready by generating their social
    caption + card when either is missing.

    Self-healing safety net: the per-publish auto-card hook only fires when an
    article transitions draft -> published, so anything inserted/published by a
    path without that hook (or whose render failed at the time) would otherwise
    be invisible to the picker and Today's plan would fall back to game previews
    only. Runs before dt.plan_day() so originals show up as original picks.

    Must run inside a single event loop (the async DB engine is loop-bound),
    which is why the sync wrapper below is only used from the standalone
    subprocess rather than from the web app process.
    """
    from app.routers.original_articles import _auto_original_social_card

    eng = _eng()
    try:
        hours = int(dt.ORIGINAL_MAX_AGE_HOURS)
    except Exception:
        hours = 48
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT id, sport FROM public.original_articles"
            " WHERE status = 'published' AND visibility = 'public'"
            "   AND x_posted_at IS NULL"
            "   AND coalesce(published_at, updated_at, created_at)"
            "       >= now() - make_interval(hours => :h)"
            "   AND (social_caption IS NULL OR length(btrim(social_caption)) = 0"
            "        OR preview_image IS NULL OR length(btrim(preview_image)) = 0)"
            " ORDER BY coalesce(published_at, updated_at, created_at) DESC"
            " LIMIT :n"
        ), {"h": hours, "n": int(limit)}).mappings().all()
    made = 0
    for r in rows:
        try:
            await _auto_original_social_card(r["sport"], int(r["id"]))
            made += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("x_daily_plan: ensure card failed for %s/%s: %s", r["sport"], r["id"], e)
    if made:
        logger.info("x_daily_plan: generated %d original social card(s)", made)
    return made


def _ensure_fresh_original_cards(limit: int = 9) -> int:
    """Sync wrapper for the ensure step. Only safe OUTSIDE a running event loop
    (the standalone x_daily_plan subprocess); skipped when a loop is running."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_ensure_fresh_original_cards_async(limit))
    logger.info("x_daily_plan: original-card ensure skipped (already in event loop)")
    return 0


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
    if not a.print:
        # 0) self-heal: make sure fresh originals are tweet-ready (caption + card)
        #    so the picker can recommend original-article picks, not only game
        #    previews. Runs in this subprocess (single event loop).
        try:
            _ensure_fresh_original_cards()
        except Exception as e:  # noqa: BLE001
            logger.warning("x_daily_plan: original-card ensure step failed: %s", e)
    data = generate(commit=not a.print, max_items=a.max)
    print(json.dumps(data, indent=2, default=str))


if __name__ == "__main__":
    main()
