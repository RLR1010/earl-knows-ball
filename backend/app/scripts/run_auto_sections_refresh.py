#!/usr/bin/env python3
"""
Auto-generation refresh runner (staggered).

Scheduled into the Earl task system as a SINGLE `subprocess` job that fires
frequently through the day (every 30 min) and handles ALL active
auto_generation configs. Each config declares its own cadence (daily/weekly,
target section, sport, instructions, etc.). A fleet of daily + weekly tasks
ever piles up because each config becomes due only when its OWN last run is
older than its cadence period (24h daily / 7d weekly), and the per-pass cap
(MAX_GENERATIONS) forces only a few generations per 30-min tick so the LLM / 
API never gets hammered at a single moment.

Generation is performed by POSTing to the local original-articles /generate
endpoint (same code path the admin UI uses), which persists the article with
the config's target `section`.

Usage:
    cd <repo>/backend && PYTHONPATH=$PWD/backend <repo>/venv/bin/python app/scripts/run_auto_sections_refresh.py

Exit code 0 on success, 2 on fatal error.
"""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.app.database import async_session  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("auto_sections_refresh")

API_BASE = os.environ.get("EARL_AUTO_GEN_API", "http://localhost:8002")
# How many due configs to generate per pass (prevents a thundering herd in
# one invocation even if many become due at once).
MAX_GENERATIONS = int(os.environ.get("EARL_AUTO_GEN_MAX_PER_PASS", "3"))
# Brief sleep between individual generations to keep load gentle.
SLEEP_BETWEEN = float(os.environ.get("EARL_AUTO_GEN_SLEEP", "5.0"))
VALID_SECTIONS = ("article", "daily_picks")


def _is_due(cfg: dict, now: datetime) -> bool:
    """Decide whether a config is due for generation.

    A config is due when it has never run, OR when its last generation is
    older than its cadence period (24h daily / 7d weekly). Each config last
    runs at its own distinct time, so cohorts naturally spread instead of all
    firing at a shared boundary; the MAX_GENERATIONS per-pass cap handles
    first-time bootstrapping/backfill without a thundering herd.
    """
    cadence = cfg.get("cadence") or "daily"
    period_seconds = (7 * 24 * 60 * 60) if cadence == "weekly" else (24 * 60 * 60)

    last_gen = cfg.get("last_generated_at")
    if last_gen is None:
        return True  # never run -> catch up
    if last_gen.tzinfo is None:
        last_gen = last_gen.replace(tzinfo=timezone.utc)

    # A per-config generate_time (HH:MM) gives calendar-day semantics: the
    # config is due once per local day, at/after that clock time, regardless
    # of exactly when it last fired (so a "daily 08:00" article lands each
    # morning, not a rolling 24h after yesterday's run).
    generate_time = (cfg.get("generate_time") or "").strip()
    if cadence in ("daily", "weekly") and generate_time:
        return _is_due_time_of_day(cfg, now, generate_time, weekly=(cadence == "weekly"))

    return (now - last_gen).total_seconds() >= period_seconds


def _is_due_time_of_day(cfg: dict, now: datetime, generate_time: str, weekly: bool) -> bool:
    """Calendar-ish due check for a config with a preferred generate_time.

    The cadence window (24h daily / 7d weekly) still applies as a lower bound, but the
    due boundary snaps to the generate_time on the target local day instead of the
    exact instant of the previous run. This keeps cohorts anchored to a clean
    clock time rather than drifting to the time of the prior generation.
    """
    try:
        local = ZoneInfo("America/Chicago")
        local_now = now.astimezone(local)
        hh, mm = (int(x) for x in generate_time.split(":"))
        target_time = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        # Malformed generate_time — fall back to rolling window.
        cadence = cfg.get("cadence") or "daily"
        period_seconds = (7 * 24 * 60 * 60) if cadence == "weekly" else (24 * 60 * 60)
        last_gen = cfg.get("last_generated_at")
        if last_gen.tzinfo is None:
            last_gen = last_gen.replace(tzinfo=timezone.utc)
        return (now - last_gen).total_seconds() >= period_seconds

    # A weekly config is due only on its target weekday (the weekday it last ran).
    if weekly:
        last_gen = cfg.get("last_generated_at")
        if last_gen.tzinfo is None:
            last_gen = last_gen.replace(tzinfo=timezone.utc)
        if last_gen.astimezone(local).weekday() != local_now.weekday():
            return False

    # Only after the target clock time has been reached on the Windows day.
    if local_now < target_time:
        return False

    # Due if the last run was before this day's target boundary (or how never ran).
    last_gen = cfg.get("last_generated_at")
    if last_gen is None:
        return True
    if last_gen.tzinfo is None:
        last_gen = last_gen.replace(tzinfo=timezone.utc)
    return last_gen.astimezone(local) < target_time


async def _load_active_configs() -> list[dict]:
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT id, sport, title, description, instructions, cadence,
                       generate_time, scope_type, team_id, team_abbr, team_name,
                       template_article_id, section, status,
                       reasoning, visibility, word_min, word_max, title_mode,
                       last_generated_at
                FROM public.auto_generation_configs
                WHERE status = 'active'
                ORDER BY id ASC
                """
            )
        )
        return [dict(r) for r in result.mappings()]


async def _mark_generated(config_id: int):
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE public.auto_generation_configs "
                "SET last_generated_at = NOW(), updated_at = NOW() WHERE id = :id"
            ),
            {"id": config_id},
        )
        await db.commit()


async def _resolve_instructions(cfg: dict) -> str:
    """Use config.instructions; fall back to the template article's instructions."""
    if (cfg.get("instructions") or "").strip():
        return cfg["instructions"].strip()

    template_id = cfg.get("template_article_id")
    if template_id:
        async with async_session() as db:
            res = await db.execute(
                text(
                    "SELECT instructions, content FROM public.original_articles "
                    "WHERE id = :aid LIMIT 1"
                ),
                {"aid": template_id},
            )
            row = res.mappings().first()
            if row and (row.get("instructions") or "").strip():
                return row["instructions"].strip()
    return f"Write a {cfg.get('cadence', 'daily')} article for {cfg.get('sport', '')}."


async def generate_config(cfg: dict) -> dict:
    sport = cfg["sport"]
    section = cfg.get("section") or "article"
    if section not in VALID_SECTIONS:
        section = "article"

    instructions = await _resolve_instructions(cfg)

    payload = {
        "instructions": instructions,
        "section": section,
        "visibility": cfg.get("visibility") or "public",
        "reasoning": cfg.get("reasoning") or "medium",
    }
    wmin, wmax = cfg.get("word_min"), cfg.get("word_max")
    if (wmin or wmax) is not None:
        payload["word_count"] = [wmin if wmin is not None else 0, wmax if wmax is not None else 2500]
    if cfg.get("title_mode"):
        payload["title_mode"] = cfg["title_mode"]

    url = f"{API_BASE}/original-articles/{sport}/generate"
    async with httpx.AsyncClient(timeout=1200) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()

    # The /generate endpoint stores the article as a draft; auto-generated
    # articles must go live immediately (matching the no-draft writeup
    # convention), so publish via the admin PATCH endpoint.
    # The generate response exposes the new row id as `draft_id`.
    article_id = None
    if isinstance(data, dict):
        article_id = data.get("draft_id") or data.get("id") or (data.get("article") or {}).get("id")
    if article_id:
        await _publish_article(sport, article_id)
    return data


async def _publish_article(sport: str, article_id: int):
    url = f"{API_BASE}/api/admin/original-articles/{sport}/{article_id}"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.patch(url, json={"status": "published"})
        resp.raise_for_status()
        logger.info("Published article %s (%s, section via generate)", article_id, sport)


async def run() -> int:
    now = datetime.now(timezone.utc)
    configs = await _load_active_configs()
    logger.info("Loaded %d active auto-gen configs", len(configs))

    due = []
    for cfg in configs:
        try:
            if _is_due(cfg, now):
                due.append(cfg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Config %s due-check skipped (%s)", cfg.get("id"), exc)

    logger.info("%d config(s) due in this pass", len(due))

    generated = []
    failures = 0
    for i, cfg in enumerate(due):
        if i >= MAX_GENERATIONS:
            logger.info("Hit MAX_GENERATIONS=%d for this pass; %d left for next tick",
                        MAX_GENERATIONS, len(due) - i)
            break
        cfg_id = cfg["id"]
        try:
            logger.info("Generating config %s (%s) [%s/%s] section=%s",
                        cfg_id, cfg.get("title"), i + 1, min(len(due), MAX_GENERATIONS),
                        cfg.get("section"))
            result = await generate_config(cfg)
            generated.append({"config_id": cfg_id, "title": cfg.get("title"), "result": result})
            logger.info("✔️  Config %s generated -> %s", cfg_id,
                        (result or {}).get("id") or (result or {}).get("article_id"))
            await _mark_generated(cfg_id)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            logger.error("✖️  Config %s FAILED: %s", cfg_id, exc)
            generated.append({"config_id": cfg_id, "title": cfg.get("title"), "error": str(exc)})

        if i < len(due) - 1 and SLEEP_BETWEEN > 0:
            await asyncio.sleep(SLEEP_BETWEEN)

    summary = {
        "ran_at": now.isoformat(timespec="seconds"),
        "active_configs": len(configs),
        "due": len(due),
        "generated_this_pass": len(generated),
        "failures": failures,
        "results": generated,
    }
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


def main() -> int:
    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Fatal error in auto-sections refresh")
        return 2


if __name__ == "__main__":
    sys.exit(main())
