"""One-off backfill: draft social_caption for MLB writeups that shipped with a
card but no caption (cols: social_caption NULL/empty). Mirrors the generate()
social-caption block: storyline pulled from stored public_content[:900], caption
drafted via MLBWriteupGenerator._generate_caption(title, storyline).

Usage (run from backend repo root on compute, same as run_mlb_writeups_daily):
  cd /home/rich/earl-knows-football/backend && PYTHONPATH=/home/rich/earl-knows-football:/home/rich/earl-knows-football/backend \
    /home/rich/venv/bin/python backfill_mlb_social_captions.py [--start YYYY-MM-DD] [--dry-run]

Default window: today (America/New_York day), status='published'.
"""
import argparse
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from backend.app.database import async_session

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_mlb_social_captions")

ET = ZoneInfo("America/New_York")


def et_day_window(start_str: str | None, end_str: str | None):
    if start_str and end_str:
        start = datetime.fromisoformat(start_str).replace(tzinfo=ET)
        end = datetime.fromisoformat(end_str).replace(tzinfo=ET)
    else:
        now = datetime.now(ET)
        start = datetime(now.year, now.month, now.day, tzinfo=ET)
        end = start + timedelta(days=1)
    return start, end


async def fetch_targets(start, end) -> list[dict]:
    async with async_session() as db:
        rows = await db.execute(
            text(
                """
                SELECT g.id AS game_id,
                       ht.abbreviation AS home_abbr,
                       at.abbreviation AS away_abbr,
                       w.title,
                       w.public_content,
                       w.social_caption
                FROM mlb.game_writeups w
                JOIN mlb.games g ON g.id = w.game_id
                JOIN mlb.teams ht ON ht.id = g.home_team_id
                JOIN mlb.teams at ON at.id = g.away_team_id
                WHERE w.created_at >= :start AND w.created_at < :end
                  AND w.status = 'published'
                  AND (w.social_caption IS NULL OR btrim(w.social_caption) = '')
                ORDER BY g.id
                """
            ),
            {"start": start, "end": end},
        )
        return [dict(r._mapping) for r in rows.fetchall()]


async def backfill(start, end, dry_run: bool) -> int:
    from app.writeups.mlb.generator import MLBWriteupGenerator

    targets = await fetch_targets(start, end)
    logger.info("Found %d published writeup(s) missing social_caption in window %s..%s",
                len(targets), start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes"))
    if not targets:
        return 0

    gen = MLBWriteupGenerator()
    results, failures = [], 0
    for t in targets:
        title = (t.get("title") or "").strip()
        pub = (t.get("public_content") or "").strip()
        storyline = pub[:900] or title
        try:
            usage_log: list = []
            caption = await gen._generate_caption(title, storyline, usage_log=usage_log)
            caption = (caption or "").strip()[:500] or None
            if dry_run:
                logger.info("[dry-run] game %s: caption=%r", t["game_id"], caption)
                continue
            if not caption:
                logger.warning("game %s produced EMPTY caption; leaving unchanged", t["game_id"])
                continue
            async with async_session() as db:
                await db.execute(
                    text(
                        "UPDATE mlb.game_writeups SET social_caption=:cap, updated_at=CURRENT_TIMESTAMP "
                        "WHERE game_id=:gid AND status='published'"
                    ),
                    {"cap": caption, "gid": t["game_id"]},
                )
                await db.commit()
            logger.info("game %s (%s @ %s): caption set (%d chars)",
                        t["game_id"], t["away_abbr"], t["home_abbr"], len(caption))
            results.append({"game_id": t["game_id"], "matchup": f"{t['away_abbr']} @ {t['home_abbr']}", "caption": caption})
        except Exception as e:  # noqa: BLE001
            failures += 1
            logger.exception("game %s FAILED: %s", t["game_id"], e)
            results.append({"game_id": t["game_id"], "matchup": f"{t['away_abbr']} @ {t['home_abbr']}", "error": str(e)})

    logger.info("DONE: %d updated, %d failed%s", len(results) - failures, failures, " (dry-run)" if dry_run else "")
    return failures


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    start, end = et_day_window(args.start, args.end)
    return await backfill(start, end, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
