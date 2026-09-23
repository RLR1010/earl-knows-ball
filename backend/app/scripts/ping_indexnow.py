#!/usr/bin/env python3
"""Scheduled task: ping IndexNow with recently published URLs.

Collects URLs for original articles + recaps (``original_articles``) and game
previews (``<sport>.game_writeups``) published within the last ``--hours`` and
submits them to IndexNow (Bing/Yandex instant indexing). Optionally also pings
the RSS feed URLs themselves to speed up feed discovery.

Runs as a `subprocess` task (hourly). Idempotent — re-pinging the same URL is
harmless.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python app/scripts/ping_indexnow.py
    # options: --hours N  --sport mlb  --limit N  --include-feeds  --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.database import async_session  # noqa: E402
from app.services.indexnow import submit_urls  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("ping_indexnow")

SPORTS = ["nfl", "nba", "mlb"]


async def _collect(hours: int, sports: list[str], limit: int) -> list[str]:
    urls: list[str] = []
    async with async_session() as db:
        # Original articles + recaps.
        rows = await db.execute(text("""
            SELECT sport, slug FROM public.original_articles
            WHERE status = 'published' AND visibility = 'public'
              AND slug IS NOT NULL AND slug <> ''
              AND COALESCE(published_at, created_at) > now() - make_interval(hours => :h)
              AND sport = ANY(:sports)
            ORDER BY COALESCE(published_at, created_at) DESC
            LIMIT :lim
        """), {"h": hours, "sports": sports, "lim": limit})
        for r in rows.mappings().all():
            urls.append(f"/{r['sport']}/articles/{r['slug']}")

        # Game previews.
        for sp in sports:
            try:
                rows = await db.execute(text(f"""
                    SELECT COALESCE(NULLIF(w.slug, ''), CAST(w.id AS text)) AS ident
                    FROM {sp}.game_writeups w
                    WHERE w.status = 'published'
                      AND w.published_at > now() - make_interval(hours => :h)
                    ORDER BY w.published_at DESC
                    LIMIT :lim
                """), {"h": hours, "lim": limit})
                for r in rows.mappings().all():
                    urls.append(f"/{sp}/articles/previews/{r['ident']}")
            except Exception as exc:
                logger.warning("skip %s.game_writeups: %s", sp, exc)

    # de-dup, keep order
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _main() -> int:
    parser = argparse.ArgumentParser(description="Ping IndexNow with recent URLs.")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--sport", default="all", help="mlb | nfl | nba | all")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--include-feeds", action="store_true",
                        help="also submit the RSS feed URLs")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    sports = SPORTS if args.sport == "all" else [args.sport]
    for sp in sports:
        if sp not in SPORTS:
            logger.error("unknown sport %r", sp)
            return 2

    urls = asyncio.run(_collect(args.hours, sports, args.limit))
    if args.include_feeds:
        urls += ["/feed.xml"] + [f"/{sp}/feed.xml" for sp in sports]

    if not urls:
        logger.info("no recent URLs to submit (last %dh)", args.hours)
        return 0

    logger.info("collected %d URL(s) from the last %dh", len(urls), args.hours)
    if args.dry_run:
        for u in urls:
            print(u)
        return 0

    result = asyncio.run(submit_urls(urls))
    logger.info("IndexNow result: %s", result)
    if result.get("skipped"):
        logger.info("IndexNow skipped: %s", result.get("reason"))
        return 0
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_main())
