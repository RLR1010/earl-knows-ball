"""Weekly power-rankings article generator (NFL / NBA / MLB).

For each sport this reads the latest published ``{sport}.power_ratings`` snapshot
(joined to the written blurbs), asks the writer model for a weekly *power rankings
column*, and stores it as a **published** ``public.original_articles`` row with
``section = 'power-ranking'``.

Idempotent per ``(sport, season, week)`` via the article slug, so re-running the
weekly job (or a retry) never duplicates a column.

Usage::

    from app.writeups.power_rankings import generate_power_rankings_article, run
    await generate_power_rankings_article(db, "nfl")      # one sport
    await run(sports=["nfl", "nba", "mlb"])               # weekly job
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

log = logging.getLogger(__name__)

SPORT_LABELS = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB"}
SECTION = "power-ranking"

_SYSTEM_PROMPT = (
    "You are Earl, the lead analyst at Earl Knows Ball — a data-first sports "
    "handicapping site. You write the weekly POWER RANKINGS column. Voice: confident, "
    "sharp, conversational, zero fluff, no clichés, no hype. Ground EVERY claim in the "
    "numbers you are given and never invent stats, scores, injuries, quotes, or games "
    "that are not in the data. Return ONLY a single valid JSON object — no prose, no "
    "code fences."
)


def _call_llm_kwargs(user_prompt: str, *, max_tokens: int = 2600, temperature: float = 0.75):
    return dict(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )


async def _call_llm(user_prompt: str, *, max_tokens: int = 2600, temperature: float = 0.75):
    """Call DeepSeek with reasoning disabled. Returns (content, tokens, usage)."""
    client = AsyncOpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
    base = _call_llm_kwargs(user_prompt, max_tokens=max_tokens, temperature=temperature)
    try:
        resp = await client.chat.completions.create(
            **base, extra_body={"thinking": {"type": "disabled"}}
        )
    except Exception as exc:  # noqa: BLE001 - fall back to a plain request
        log.warning("power-rankings thinking=disabled rejected (%s); retrying without", exc)
        resp = await client.chat.completions.create(**base)
    content = (resp.choices[0].message.content or "").strip()
    tokens = 0
    usage: dict[str, Any] = {}
    try:
        if resp.usage is not None:
            usage = resp.usage.model_dump()
            tokens = int(usage.get("total_tokens") or 0)
    except Exception:  # noqa: BLE001
        usage, tokens = {}, 0
    return (content or None), tokens, usage


def _parse_json(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _slugify(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def _strip_repeat_of_title(title: str, text_value: str) -> str:
    """Drop a leading repeat of the headline from the dek (the summary column
    often echoes the title; we never want the dek to repeat it verbatim)."""
    if not title or not text_value:
        return text_value or ""
    tw = title.lower().split()
    aw = text_value.split()
    n = 0
    while n < len(tw) and n < len(aw) and aw[n].lower().strip(".,:;!?—-") == tw[n].strip(".,:;!?—-"):
        n += 1
    if n >= max(3, len(tw) - 1):
        return " ".join(aw[n:]).lstrip(" .,:;-—").strip()
    return text_value


async def _latest_snapshot(db: AsyncSession, sport: str) -> Optional[tuple[int, int, Any]]:
    row = (
        await db.execute(
            text(
                f"SELECT season, week, as_of_date FROM {sport}.power_ratings "
                "ORDER BY season DESC, week DESC LIMIT 1"
            )
        )
    ).first()
    if not row:
        return None
    return int(row[0]), int(row[1]), row[2]


async def _load_rankings(db: AsyncSession, sport: str, season: int, week: int) -> list[dict]:
    rows = (
        await db.execute(
            text(
                f"""
                SELECT p.rank, p.prev_rank, p.rank_delta, p.rating, p.rating_delta,
                       p.sos, p.games_played, p.wins, p.losses, p.ties,
                       t.abbreviation AS team, t.name AS team_name, b.blurb
                FROM {sport}.power_ratings p
                LEFT JOIN {sport}.power_ranking_blurbs b
                       ON b.season = p.season AND b.week = p.week AND b.team_id = p.team_id
                LEFT JOIN {sport}.teams t ON t.id = p.team_id
                WHERE p.season = :s AND p.week = :w
                ORDER BY p.rank
                """
            ),
            {"s": season, "w": week},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _record(r: dict) -> str:
    if r.get("wins") is None:
        return "—"
    rec = f"{r['wins']}-{r['losses']}"
    if r.get("ties"):
        rec += f"-{r['ties']}"
    return rec


def _fmt_change(v) -> str:
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{f:+.2f}"


def _rankings_block(rows: list[dict]) -> str:
    lines = []
    for r in rows:
        lines.append(
            f"#{r['rank']:>2}  {r['team']:<4} {r.get('team_name') or ''} — "
            f"rating {float(r['rating']):.2f} ({_fmt_change(r.get('rating_delta'))} wk), "
            f"record {_record(r)}, SOS {float(r['sos']):.2f}, "
            f"rank chg {r.get('rank_delta') if r.get('rank_delta') is not None else '—'} | "
            f"blurb: {(r.get('blurb') or '').strip()[:320]}"
        )
    return "\n".join(lines)


def _build_prompt(sport: str, season: int, week: int, as_of: Any, rows: list[dict]) -> str:
    label = SPORT_LABELS.get(sport, sport.upper())
    top = ", ".join(f"{r['team']} ({_fmt_change(r.get('rating_delta'))})" for r in rows[:5])
    return f"""Write this week's {label} POWER RANKINGS column for Earl Knows Ball.

Snapshot: {label} {season} season, week {week}{f", as of {as_of}" if as_of else ""}.
Teams rated 1..{len(rows)} by a points/SRS-style rating that already accounts for
strength of schedule. A bigger rating = better team; the rating gap between two teams
is roughly the margin the higher-rated team would be expected to win by on a neutral field.

The full ranking (use ONLY these numbers):
{_rankings_block(rows)}

The top five this week: {top}

Write a genuine, interesting weekly column — NOT a plain list dump. Structure:
1. A lede that captures the story of the week (who is on top and why, the big
   movement, the theme).
2. "The Top Five" — a short paragraph on each of #1..#5, using the rating, the weekly
   rating change, the record, SOS, and the supplied blurb as raw material. You MAY
   quote/paraphrase a team's blurb.
3. "Risers and Fallers" — call out the biggest rank/rating movers by name (use rank chg
   and rating change). If movement is minimal, say so and explain why the board is stable.
4. "How the Board Sets Up" — 2-4 teams to watch next week, grounded in the numbers.
5. A short sign-off in Earl's voice.

Rules: 700-1000 words. Markdown with "##" subheads. No tables. No invented stats,
games, injuries or quotes. Do not state a team's rating is a win total or a spread;
treat it as our power rating.

Return ONLY this JSON object:
{{
  "title": "a specific, compelling headline (<= 95 chars)",
  "summary": "one or two sentence dek, 180-300 chars, MUST NOT repeat the title wording",
  "content": "the full column in Markdown",
  "seo_description": "<= 155 chars",
  "seo_keywords": ["6-10 keyword phrases"]
}}"""


async def _existing_id(db: AsyncSession, sport: str, slug: str) -> Optional[int]:
    return (
        await db.execute(
            text(
                "SELECT id FROM public.original_articles "
                "WHERE sport = :s AND slug = :g AND section = :sec LIMIT 1"
            ),
            {"s": sport, "g": slug, "sec": SECTION},
        )
    ).scalar_one_or_none()


_UPDATE_SQL = text(
    """
    UPDATE public.original_articles SET
        title = :title, summary = :summary, content = :content,
        seo_description = :seo_description, seo_keywords = :seo_keywords,
        teams = CAST(:teams AS jsonb), prompt_json = CAST(:prompt_json AS jsonb),
        usage_json = CAST(:usage_json AS jsonb), tokens_used = :tokens_used,
        word_count = :word_count, published_at = :published_at, updated_at = now()
    WHERE id = :id
    RETURNING id
    """
)


_INSERT_SQL = text(
    """
    INSERT INTO public.original_articles
        (sport, title, summary, content, slug, seo_description, seo_keywords, teams,
         section, status, visibility, author, prompt_json, usage_json, tokens_used,
         word_count, published_at, created_at, updated_at)
    VALUES
        (:sport, :title, :summary, :content, :slug, :seo_description, :seo_keywords,
         CAST(:teams AS jsonb), :section, 'published', 'public', 'Earl',
         CAST(:prompt_json AS jsonb), CAST(:usage_json AS jsonb), :tokens_used,
         :word_count, :published_at, now(), now())
    RETURNING id
    """
)


async def generate_power_rankings_article(
    db: AsyncSession, sport: str, *, force: bool = False
) -> dict[str, Any]:
    """Write the weekly power-rankings column for one sport. Idempotent per week."""
    sport = (sport or "").lower()
    if sport not in SPORT_LABELS:
        raise ValueError(f"unsupported sport for power rankings: {sport!r}")

    snap = await _latest_snapshot(db, sport)
    if not snap:
        return {"sport": sport, "status": "skipped", "reason": "no power ratings published"}
    season, week, as_of = snap

    slug = _slugify(f"{sport}-power-rankings-{season}-week-{week}")
    existing = await _existing_id(db, sport, slug)
    if existing and not force:
        return {"sport": sport, "status": "exists", "id": int(existing), "slug": slug}

    rows = await _load_rankings(db, sport, season, week)
    if not rows:
        return {"sport": sport, "status": "skipped", "reason": "no rows for latest week"}

    prompt = _build_prompt(sport, season, week, as_of, rows)
    raw, tokens, usage = await _call_llm(prompt)
    data = _parse_json(raw)
    if not data:
        raw, tokens2, usage2 = await _call_llm(
            prompt + "\n\nREMINDER: respond with ONLY the JSON object, nothing else."
        )
        tokens += tokens2
        usage = usage2 or usage
        data = _parse_json(raw)
    if not data:
        raise RuntimeError(f"{sport}: power-rankings article returned no parseable JSON")

    title = (data.get("title") or "").strip()[:200]
    content = (data.get("content") or "").strip()
    summary = _strip_repeat_of_title(title, (data.get("summary") or "").strip())
    if not title or not content:
        raise RuntimeError(f"{sport}: power-rankings article missing title/content")

    keywords = data.get("seo_keywords")
    if isinstance(keywords, list):
        seo_keywords = ", ".join(str(k) for k in keywords if str(k).strip())
    else:
        seo_keywords = str(keywords or "").strip() or None

    # Match the site-wide article convention: `teams` is an array of team
    # ABBREVIATION strings (RecentContent renders each as <TeamLogo abbr=...>).
    # Storing objects here crashed the sport home page.
    teams_json = [r["team"] for r in rows[:5] if r.get("team")]

    params = {
            "sport": sport,
            "title": title,
            "summary": summary or None,
            "content": content,
            "slug": slug,
            "seo_description": (data.get("seo_description") or "").strip()[:300] or None,
            "seo_keywords": seo_keywords,
            "teams": json.dumps(teams_json),
            "section": SECTION,
            "prompt_json": json.dumps({"season": season, "week": week, "sport": sport}),
            "usage_json": json.dumps(usage or {}),
            "tokens_used": tokens,
            "word_count": len(content.split()),
            "published_at": as_of,
        }
    if existing:
        result = await db.execute(_UPDATE_SQL, {**params, "id": int(existing)})
        status = "updated"
    else:
        result = await db.execute(_INSERT_SQL, params)
        status = "created"
    new_id = int(result.scalar_one())
    await db.commit()
    log.info(
        "power-rankings article: %s %s wk%s -> article %s (%s words)",
        sport, season, week, new_id, len(content.split()),
    )
    return {
        "sport": sport,
        "status": status,
        "id": new_id,
        "slug": slug,
        "season": season,
        "week": week,
        "words": len(content.split()),
    }


async def run(db: AsyncSession, sports: Optional[list[str]] = None, *, force: bool = False) -> list[dict]:
    """Generate the weekly column for each sport (best-effort: one failure never
    blocks the others)."""
    out: list[dict] = []
    for sport in (sports or list(SPORT_LABELS)):
        try:
            out.append(await generate_power_rankings_article(db, sport, force=force))
        except Exception as exc:  # noqa: BLE001 - isolate per-sport failures
            log.exception("power-rankings article failed for %s", sport)
            await db.rollback()
            out.append({"sport": sport, "status": "error", "error": str(exc)})
    return out
