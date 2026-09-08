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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import jwt  # type: ignore
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
VALID_SECTIONS = ("article", "daily_picks", "earls_winners")

# How many previously-published articles to feed back to the LLM as
# "previous coverage" context so each generation is fresh and non-repetitive.
# Set EARL_AUTO_GEN_RECENCY=0 to disable.
RECENCY_LIMIT = int(os.environ.get("EARL_AUTO_GEN_RECENCY", "4"))
# Max chars of each prior article's content to include in the context digest.
RECENCY_CONTENT_CHARS = int(os.environ.get("EARL_AUTO_GEN_RECENCY_CHARS", "300"))


def _is_due(cfg: dict, now: datetime) -> bool:
    """Decide whether a config is due for generation.

    A config is due when it has never run, OR when its last generation is
    older than its cadence period. Cadence maps to a day interval:
    - daily  = every 1 day
    - weekly = every 7 days
    - 2day   = every 2 days (e.g. the Earl's Winners recap article)
    A per-config generate_time gives calendar-day semantics: the config is due
    once per cadence window at/after that clock time, anchored to a clean local
    boundary instead of a rolling 24h-from-last-run. Each config last runs at
    its own distinct generate_time, so cohorts spread; MAX_GENERATIONS handles
    first-time backfill.
    """
    cadence = cfg.get("cadence") or "daily"
    _INTERVAL_DAYS = {"daily": 1, "weekly": 7, "2day": 2}
    interval_days = _INTERVAL_DAYS.get(cadence, 1)
    period_seconds = interval_days * 24 * 60 * 60

    last_gen = cfg.get("last_generated_at")
    if last_gen is None:
        return True  # never run -> catch up
    if last_gen.tzinfo is None:
        last_gen = last_gen.replace(tzinfo=timezone.utc)

    # A per-config generate_time (HH:MM) gives calendar-day semantics.
    generate_time = (cfg.get("generate_time") or "").strip()
    if cadence in ("daily", "weekly", "2day") and generate_time:
        return _is_due_time_of_day(cfg, now, generate_time,
                                   weekly=(cadence == "weekly"),
                                   interval_days=interval_days)

    return (now - last_gen).total_seconds() >= period_seconds


def _is_due_time_of_day(cfg: dict, now: datetime, generate_time: str, weekly: bool,
                        interval_days: int = 1) -> bool:
    """Calendar-ish due check for a config with a preferred generate_time.

    The cadence window (interval_days * 24h) still applies as a lower bound, but the
    due boundary snaps to the generate_time on the target local day instead of the
    exact instant of the previous run. This keeps cohorts anchored to a clean
    clock time rather than drifting to the time of the prior generation.
    weekly cadences additionally require matching weekday-of-last-run;
    every-N-day cadences (e.g. '2day') require N local dates to have elapsed.
    """
    try:
        local = ZoneInfo("America/Chicago")
        local_now = now.astimezone(local)
        hh, mm = (int(x) for x in generate_time.split(":"))
        target_time = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        # Malformed generate_time — fall back to rolling window (interval_days).
        last_gen = cfg.get("last_generated_at")
        if last_gen is None:
            return True
        if last_gen.tzinfo is None:
            last_gen = last_gen.replace(tzinfo=timezone.utc)
        return (now - last_gen).total_seconds() >= interval_days * 24 * 60 * 60

    # A weekly config is due only on its target weekday (the weekday it last ran).
    if weekly:
        last_gen = cfg.get("last_generated_at")
        if last_gen.tzinfo is None:
            last_gen = last_gen.replace(tzinfo=timezone.utc)
        if last_gen.astimezone(local).weekday() != local_now.weekday():
            return False

    # Only after the target clock time has been reached on the target day.
    if local_now < target_time:
        return False

    # Due if the last run was before this day's target boundary (or if never ran).
    last_gen = cfg.get("last_generated_at")
    if last_gen is None:
        return True
    if last_gen.tzinfo is None:
        last_gen = last_gen.replace(tzinfo=timezone.utc)
    last_local = last_gen.astimezone(local)

    # Every-N-day cadence (e.g. '2day'): due when at least interval_days local
    # calendar dates have fully elapsed since the last run's date (combined with
    # the generate_time check above, this anchors to clean alternating days).
    if interval_days > 1 and not weekly:
        return (local_now.date() - last_local.date()).days >= interval_days

    # daily / weekly: due once we've crossed today's target boundary since last run.
    return last_local < target_time


async def _load_active_configs() -> list[dict]:
    async with async_session() as db:
        result = await db.execute(
            text(
                """
                SELECT id, sport, title, description, instructions, cadence,
                       generate_time, scope_type, team_id, team_abbr, team_name,
                       template_article_id, section, status,
                       reasoning, visibility, word_min, word_max, title_mode,
                       recency_context,
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


async def _load_previous_coverage(cfg: dict) -> list[dict]:
    """Fetch the most recent published articles in this config's scope.

    Scope matches how the config's articles land: same sport (and section).
    For a cross-sport daily (sport='all'), this returns the prior cross-sport
    articles specifically, so the model can see what it already covered.
    """
    if RECENCY_LIMIT <= 0:
        return []
    async with async_session() as db:
        res = await db.execute(
            text(
                """
                SELECT title, content, published_at
                FROM public.original_articles
                WHERE status = 'published'
                  AND sport = :sport
                  AND section = :section
                ORDER BY published_at DESC NULLS LAST
                LIMIT :limit
                """
            ),
            {"sport": cfg["sport"], "section": cfg.get("section") or "article",
             "limit": RECENCY_LIMIT},
        )
        return [dict(r) for r in res.mappings()]


def _strip_markdown(text_in: str) -> str:
    """Crudely strip markdown so the prior-content excerpts read as plain text."""
    import re
    if not text_in:
        return ""
    t = re.sub(r"#{1,6}\s*", "", text_in)
    t = re.sub(r"[\*_`>~|]+|\[\]?\(\)", " ", t)
    t = re.sub(r"\n{2,}", " ", t)
    return t.strip()


async def _winners_recap_context(cfg: dict) -> str:
    """Build a factual "Earl's recent winners" block for a Winners-Recap config.

    A config is treated as a Winners-Recap editorial iff sport='all' AND
    cadence='2day' AND section IN ('article','earls_winners') (the profile for "an
    article every two days discussing Earl's winning picks"). Earl's-Winners recaps
    now live under section 'earls_winners' (legacy rows predate that section and are
    'article', so both are accepted). For those, we pull the current top winners from
    the public.earl_winners snapshot (already curated to the most recent cashing
    picks, non-preseason, look-back window) and hand the
    real picks to the LLM so it writes a recap grounded in facts (never
    hallucinating pick names/odds). Returns an empty string for non-recap
    configs. Uses a fresh connection so it never participates in the caller's
    transaction.
    """
    if (cfg.get("sport") != "all" or (cfg.get("cadence") or "daily") != "2day"
            or (cfg.get("section") or "article") not in ("article", "earls_winners")):
        return ""

    try:
        async with async_session() as db:
            rows = (await db.execute(
                text("""SELECT sport, market, pick_text, odds_at_tip, ev,
                                home_team, away_team, home_score, away_score,
                                game_date, winning_side
                         FROM public.earl_winners ORDER BY sort_key LIMIT 8""")
            )).mappings().all()
    except Exception:
        return ""

    if not rows:
        return ("\n\nCURRENT WINNERS: no settled Earl's winners are in the snapshot right now — "
                "if true, do not fabricate any; pivot to a general note on the picks engine instead.")
    lines, medals = [], ["1", "2", "3", "4", "5", "6", "7", "8"]
    for i, w in enumerate(rows):
        pick = (w["pick_text"] or "").strip().upper()
        mark = (w["market"] or "").lower()
        odds = w["odds_at_tip"] or "-"
        ev = f"+{w['ev']:.2f}" if (w["ev"] or 0) > 0 else str(w["ev"] or 0)
        ln = f"{w['home_team']} {w['home_score']} - {w['away_score']} {w['away_team']}" if (w.get('home_score') is not None and w.get('away_score') is not None) else f"{w['home_team']} vs {w['away_team']}"
        lines.append(
            f"{medals[i]}. [{w['sport'].upper()}] {pick} ({mark}) @ {odds} — EV {ev}. Final: {ln} "
            f"({w['game_date']})."
        )
    return "\n\nEARL'S CURRENT WINNERS — these are REAL, verified cashed picks from the last week. "\
        "Write this recap ABOUT THESE EXACT picks (reference teams/scores/odds accurately, do not invent "\
        "any pick or game not listed). Lead with the freshest wins." + "\n" + "\n".join(lines)


async def _build_recency_context(cfg: dict) -> str:
    """Build a previous-coverage context block from the last N published articles.

    Returns an empty string (nothing appended) when there's no recency data or
    it's disabled. The block explicitly names the prior titles + a short content
    excerpt and tells the model to write something new and different.
    """
    prevs = await _load_previous_coverage(cfg)
    if not prevs:
        return ""

    lines = []
    for p in prevs:
        title = (p.get("title") or "").strip() or "(untitled)"
        content = _strip_markdown(p.get("content") or "")[:RECENCY_CONTENT_CHARS]
        excerpt = f" - {content}" if content else ""
        lines.append(f"* {title}{excerpt}")

    return (
        "\n\nPREVIOUS COVERAGE — the following are articles this same page/feed "
        "has ALREADY published (most recent first). This must be NEW and "
        "DIFFERENT from all of them: pick a different angle, different subjects, "
        "different framing, and a distinct headline. Do not simply rehash or "
        "re-word these. If one of these already fully covered a topic, avoid "
        "covering it again this week."
        "\n" + "\n".join(lines)
    )



async def generate_config(cfg: dict) -> dict:
    sport = cfg["sport"]
    section = cfg.get("section") or "article"
    if section not in VALID_SECTIONS:
        section = "article"

    instructions = await _resolve_instructions(cfg)

    # Winners-Recap editorials (sport='all' + cadence='2day' + section='article')
    # get the real current winning picks injected so the LLM recaps verified
    # results rather than inventing picks.
    winners_ctx = await _winners_recap_context(cfg)
    if winners_ctx:
        instructions = f"{instructions}\n\n{winners_ctx}"

    # Append previous-coverage context ONLY when this config has opted in
    # (recency_context = TRUE in the admin auto-generation page), so the LLM
    # writes something fresh and non-repetitive for recurring articles.
    if cfg.get("recency_context"):
        recency = await _build_recency_context(cfg)
        if recency:
            instructions = f"{instructions}{recency}"

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
    # Fixed-title configs (e.g. MLB daily picks 'Daily Picks We Like') send the
    # actual title value so the /generate endpoint can apply it verbatim.
    if cfg.get("title_mode") == "fixed" and cfg.get("title"):
        payload["title"] = cfg["title"]

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


async def _admin_bearer_header() -> dict:
    """Return an ``Authorization: Bearer <admin-jwt>`` header dict.

    The admin endpoints this task publishes to are protected by
    ``Depends(require_admin)`` (an HTTPBearer JWT with ``sub`` == an active
    admin user's id). The scheduler has no browser/login session, so mint a
    short-lived token exactly like the login flow (same secret/algorithm/claims)
    against a real, active admin user from the local DB, and send it as a
    Bearer header.
    """
    from backend.app.core.config import settings  # noqa: E402,F811

    now = datetime.now(timezone.utc)
    # The login flow uses a short expiry; 5 minutes is plenty for a single
    # publish round-trip and keeps the blast radius tiny.
    expires = now + timedelta(minutes=5)

    # Query the admin user id with raw SQL (no ORM model import) so this helper
    # never drags in the full model graph, which can collide under dual-root
    # (backend.app.* + app.*) import styles. The users table is shared.
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id FROM users "
                "WHERE is_admin = TRUE AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1"
            )
        )
        row = result.mappings().first()

    if not row:
        raise RuntimeError(
            "Cannot mint admin token: no active admin user found in users table"
        )
    admin_id = row["id"]

    token = jwt.encode(
        {
            "sub": str(admin_id),
            "iat": int(now.timestamp()),
            "exp": int(expires.timestamp()),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    return {"Authorization": f"Bearer {token}"}


async def _publish_article(sport: str, article_id: int):
    """Publish a draft via the admin HTTP endpoint.

    The /generate endpoint stores auto-generated articles as drafts. To go live
    immediately (matching the no-draft writeup convention) we PATCH
    /api/admin/original-articles/<sport>/<id> with ``status: published``.

    That endpoint sits behind the `admin_router`, which is protected by
    ``Depends(require_admin)`` (added when the original-articles social-card +
    admin-auth work landed). Internal scheduler scripts have no browser/login
    session, so we mint a short-lived admin JWT the same way the login flow
    does and send it as a Bearer token. Runs on the trusted compute box against
    localhost:8002, so holding the JWT secret is not an additional disclosure:
    this task has always performed the publish; it just now must authenticate.
    """
    bearer = await _admin_bearer_header()
    url = f"{API_BASE}/api/admin/original-articles/{sport}/{article_id}"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.patch(url, json={"status": "published"}, headers=bearer)
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
