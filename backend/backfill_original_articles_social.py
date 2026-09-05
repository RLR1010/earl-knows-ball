"""Backfill social captions + cards for original articles missing them.

One-time backfill (used 2026-09-04): generates a social caption (LLM) + card
(card PNG + preview_image) for original articles published in a window that do
not yet have both. Each item mirrors the publish-time auto hook
(app.routers.original_articles._auto_original_social_card) by calling it
directly, so it stays in lock-step with the "born tweet-ready" auto-gen.

Usage (run on a box with Playwright + DeepSeek creds, e.g. compute):
  python backfill_original_articles_social.py [--since 2026-09-01] [--dry-run]
                                              [--limit 5] [--sport mlb]

Safe to re-run (skips items that already have caption+card). Non-destructive:
never overwrites an existing editor caption (auto hook only fills when empty).
"""
from __future__ import annotations

import argparse
import asyncio
import re
from datetime import date

from sqlalchemy import text, create_engine

from app.core.config import settings


def _sync_engine():
    return create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))


def _candidates(since: date, sport: str | None) -> list[tuple[int, str]]:
    cond = "coalesce(published_at, updated_at, created_at) >= :since"
    params = {"since": since.isoformat()}
    if sport:
        cond += " AND sport = :sport"
        params["sport"] = sport
    cond += (" AND (social_caption IS NULL OR length(trim(social_caption)) = 0"
             " OR preview_image IS NULL OR length(trim(preview_image)) = 0)")
    sql = (
        "SELECT id, sport FROM public.original_articles "
        f"WHERE {cond} ORDER BY coalesce(published_at, updated_at, created_at) DESC"
    )
    eng = _sync_engine()
    with eng.connect() as c:
        return [(r[0], r[1]) for r in c.execute(text(sql), params)]


async def _run(since: date, sport: str | None, limit: int | None,
               dry_run: bool) -> None:
    cands = _candidates(since, sport)
    if limit:
        cands = cands[:limit]
    print(f"Backfill candidates: {len(cands)} (since {since}, sport={sport or 'all'}, dry_run={dry_run})",
          flush=True)
    if dry_run:
        for aid, sp in cands:
            print(f"  would generate {sp}/{aid}", flush=True)
        return

    # Import the shared auto hook so the backfill matches the publish-time hook.
    from app.routers.original_articles import _auto_original_social_card

    ok = err = 0
    for i, (aid, sp) in enumerate(cands, 1):
        # fresh caption+card check (cheap) so re-runs skip finished items
        try:
            eng = _sync_engine()
            with eng.connect() as c:
                done = c.execute(
                    text("SELECT 1 FROM public.original_articles WHERE id=:id AND "
                         "social_caption IS NOT NULL AND length(trim(social_caption))>0 "
                         "AND preview_image IS NOT NULL AND length(trim(preview_image))>0"),
                    {"id": aid}).scalar()
            if done:
                print(f"[{i}/{len(cands)}] skip (already ready) {sp}/{aid}", flush=True)
                continue
        except Exception as e:  # noqa: BLE001
            print(f"[{i}/{len(cands)}] pre-check err {sp}/{aid}: {e}", flush=True)
        try:
            await _auto_original_social_card(sp, aid)
            ok += 1
            print(f"[{i}/{len(cands)}] OK {sp}/{aid}", flush=True)
        except Exception as e:  # noqa: BLE001
            err += 1
            print(f"[{i}/{len(cands)}] FAIL {sp}/{aid}: {e}", flush=True)
    print(f"Finished: ok={ok} err={err} total={len(cands)}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-01", help="published on/after YYYY-MM-DD")
    ap.add_argument("--sport", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    since = date.fromisoformat(a.since)
    asyncio.run(_run(since, a.sport, a.limit, a.dry_run))


if __name__ == "__main__":
    main()
