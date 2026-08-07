"""Original Articles API — LLM-written editorial articles per sport.

The article writer reuses each sport's ToolChatEngine (the exact research
engine backing the chat feature), so authors get the same DB research tools
(team stats, standings, lines, injuries, etc.) when composing an article.

Flow:
1. POST /original-articles/{sport}/generate   {instructions}  -> {title, content}
   Runs the sport engine's research_and_answer loop and returns a draft
   (markdown). NOT stored.
2. POST /original-articles/{sport}/publish    {title, content, instructions}
   Stores the article in public.original_articles with status='published'
   and published_at=NOW() (matches the no-draft writeup convention).
3. GET  /original-articles/{sport}                     public list
4. GET  /original-articles/{sport}/{article_id}         public fetch
5. GET  /admin/original-articles/{sport}                admin list (any status)
6. DELETE /admin/original-articles/{sport}/{article_id} admin delete

Admin endpoints follow the app's existing convention (no bespoke auth gate
on the router; consistent with the writeups router).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import get_db
from app.services.team_extractor import extract_teams

# Per-sport chat engines (reused so articles get the same research tools as chat).
from app.routers.chat import nfl_chat_engine
from app.routers.chat_mlb import mlb_chat_engine
from app.routers.chat_nba import nba_chat_engine

logger = logging.getLogger("original_articles")

router = APIRouter(tags=["original-articles"])

SPORTS = ("mlb", "nfl", "nba")

ENGINES = {
    "mlb": mlb_chat_engine,
    "nfl": nfl_chat_engine,
    "nba": nba_chat_engine,
}

# Default article-writing system prompt. Mirrors the game-preview writeup
# philosophy (research first, accurate, numbers-backed) but frames it for a
# free-form editorial article rather than a game write-up.
ARTICLE_SYSTEM_PROMPT = (
    "You are a professional sports journalist writing an original editorial "
    "article. Use the research tools to pull accurate, current stats, standings, "
    "lines, injuries, and context before writing. Base every claim on the data "
    "you retrieve. Write in clean, engaging Markdown with a clear headline "
    "(start with a single `# ` title line), a strong lede, short sections with "
    "`## ` subheadings, and tight, punchy paragraphs. Favor specifics (numbers, "
    "names, dates) over vague praise. Keep it authoritative and readable — no "
    "fluff, no speculation labeled as fact. End with a one-line takeaway. "
    "FORMATTING RULE: Clean editorial prose only — do NOT use emoji, icons, "
    "emoticons, or decorative symbols anywhere (no 🏆🎯⬆⬇💡 or similar). "
    "Use plain Markdown headings, tables, and lists only."
)


class GenerateRequest(BaseModel):
    instructions: str = Field(..., min_length=1, max_length=4000)
    model: Optional[str] = Field(None)  # optional override
    author: Optional[str] = Field(None, min_length=1, max_length=100)
    tokens_used: Optional[int] = Field(None, ge=0)
    reasoning: Optional[str] = Field(None)  # minimal | low | medium | high | xhigh
    word_count: Optional[tuple[int, int]] = Field(None)  # (min_words, max_words)


class PublishRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=300)
    content: str = Field(..., min_length=1)  # markdown body
    summary: Optional[str] = Field(None, max_length=500)
    instructions: Optional[str] = Field(None, max_length=4000)
    model: Optional[str] = Field(None)
    # The exact prompt (system + user messages) and the research transcript
    # (tool calls + results) that produced the article.
    prompt: Optional[Any] = Field(None)
    research: Optional[Any] = Field(None)
    author: Optional[str] = Field(None, min_length=1, max_length=100)
    tokens_used: Optional[int] = Field(None, ge=0)
    reasoning: Optional[str] = Field(None)
    word_count: Optional[tuple[int, int]] = Field(None)
    seo_description: Optional[str] = Field(None, max_length=500)
    seo_keywords: Optional[str] = Field(None, max_length=500)


class UpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    content: Optional[str] = Field(None, min_length=1)
    summary: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = Field(None, pattern="^(published|draft)$")
    author: Optional[str] = Field(None, min_length=1, max_length=100)
    reasoning: Optional[str] = Field(None)
    word_count: Optional[tuple[int, int]] = Field(None)
    seo_description: Optional[str] = Field(None, max_length=500)
    seo_keywords: Optional[str] = Field(None, max_length=500)


def _validate_sport(sport: str) -> str:
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported sport '{sport}'. Must be one of {list(SPORTS)}.",
        )
    return sport


def _capture_research(messages: list) -> dict:
    """Extract the research transcript from the post-loop messages list.

    Returns {prompt: [...], tool_calls: [...]} where tool_calls is an ordered
    trace of every tool call the model made (with args) + the result returned.
    """
    prompt_messages = []
    research_steps: list = []
    calls_by_id: dict = {}

    for msg in messages:
        role = msg.get("role")
        if role == "system":
            prompt_messages.append({"role": "system", "content": msg.get("content")})
        elif role == "user":
            prompt_messages.append({"role": "user", "content": msg.get("content")})
        elif role == "assistant" and msg.get("tool_calls"):
            prompt_messages.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "has_tool_calls": len(msg["tool_calls"]),
                }
            )
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "unknown")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                step = {"tool": name, "arguments": args}
                calls_by_id[tc.get("id", name)] = step
                research_steps.append(step)
        elif role == "tool":
            call_id = msg.get("tool_call_id")
            result = msg.get("content")
            step = calls_by_id.get(call_id)
            if step is not None:
                # Best-effort decode. Tool results are often stored as a JSON
                # string (sometimes double-encoded), so unpack nested JSON too.
                parsed = result
                for _ in range(3):
                    if isinstance(parsed, str) and parsed.strip():
                        try:
                            parsed = json.loads(parsed)
                        except (json.JSONDecodeError, TypeError):
                            break
                    else:
                        break
                step["result"] = parsed

    return {
        "prompt": prompt_messages,
        "tool_calls": [
            {k: v for k, v in s.items() if k in ("tool", "arguments", "result")}
            for s in research_steps
        ],
    }


def _guess_summary(content: str, limit: int = 280) -> str:
    """Derive a short plain-text summary from the markdown body."""
    text_only = re.sub(r"[#>*_`\[\]]+", " ", content)
    text_only = re.sub(r"\s+", " ", text_only).strip()
    if len(text_only) <= limit:
        return text_only
    cut = text_only[:limit]
    # Don't chop mid-word.
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(".,;: ") + "…"


def _slugify_title(title: str) -> str:
    """Turn a title into an SEO-friendly slug fragment."""
    t = (title or "").strip().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t[:80] or "article"


def _article_slug(dt, title: str) -> str:
    """Build the full unique slug: YYYY-MM-DD-<title-slug>."""
    if dt is None:
        prefix = "0000-00-00"
    elif hasattr(dt, "astimezone"):
        prefix = dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    else:
        prefix = dt.strftime("%Y-%m-%d")
    return f"{prefix}-{_slugify_title(title)}"


async def _assign_unique_slug(db, sport: str, article_id: int, base: str) -> str:
    """Set a unique (per sport) slug on the article, suffixing with -N if taken."""
    cand = base
    idx = 0
    while True:
        taken = await db.execute(
            text(
                "SELECT 1 FROM public.original_articles "
                "WHERE sport = :s AND slug = :sl AND id <> :i LIMIT 1"
            ),
            {"s": sport, "sl": cand, "i": article_id},
        )
        if taken.scalar() is None:
            break
        idx += 1
        cand = f"{base}-{idx}"
    await db.execute(
        text("UPDATE public.original_articles SET slug = :sl WHERE id = :i"),
        {"sl": cand, "i": article_id},
    )
    return cand


# ──────────────────────────────────────────────
#  SEO meta generation (description + keywords for <head>)
# ──────────────────────────────────────────────

SEO_SYSTEM_PROMPT = (
    "You are an SEO specialist for Earl Knows Ball, a premium sports handicapping "
    "site. Given an article's title, summary, and body, produce a compelling meta "
    "description and a keyword list."
    "\nRules:"
    "\n- Meta description: 140-160 chars, 1-2 punchy sentences that summarize and "
    "  entice clicks. Plain text, no quotes, no trailing period if it exceeds the "
    "  limit."
    "\n- Keywords: a comma-separated list of 5-8 lowercase SEO phrases a bettor "
    "  would search, e.g. 'mlb betting picks, padres vs dodgers, over under odds'."
    "  No spaces after commas, no trailing comma."
    "\n- Return ONLY JSON: {\"seo_description\": \"...\", \"seo_keywords\": "
    "\"...\"}. No markdown, no commentary."
)


def _extract_seo_json(raw: str) -> dict:
    """Best-effort parse of the LLM's SEO JSON (handles code fences)."""
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    start = raw.find("{")
    if start != -1:
        depth = 0
        for i in range(start, len(raw)):
            if raw[i] == "{":
                depth += 1
            elif raw[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw[start : i + 1])
                    except Exception:
                        break
    return {}


async def _generate_seo(title: str, summary: str, content: str) -> dict[str, str]:
    """Return {seo_description, seo_keywords} using the DeepSeek LLM."""
    if not settings.deepseek_api_key:
        return {}
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=60.0,
    )
    body = (content or "")[:4000]
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": SEO_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"TITLE:\n{title}\n\nSUMMARY:\n{summary}"
                    f"\n\nBODY (truncated):\n{body}",
                },
            ],
            temperature=0.3,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("SEO generation failed for %r: %s", title, e)
        return {}
    data = _extract_seo_json(raw)
    return {
        "seo_description": (data.get("seo_description") or "").strip()[:500],
        "seo_keywords": (data.get("seo_keywords") or "").strip()[:500],
    }


# ──────────────────────────────────────────────
#  Generate a draft article (research-loop, reused chat engine)
# ──────────────────────────────────────────────


@router.post("/original-articles/{sport}/generate")
async def generate_original_article(
    sport: str,
    req: GenerateRequest,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    engine = ENGINES[sport]

    reasoning = (req.reasoning or "medium").strip().lower()
    _allowed_reasoning = {"minimal", "low", "medium", "high", "xhigh", "max"}
    if reasoning not in _allowed_reasoning:
        raise HTTPException(status_code=422, detail=f"reasoning must be one of {sorted(_allowed_reasoning)}.")

    length_clause = ""
    if req.word_count is not None:
        lo, hi = req.word_count
        if lo <= 0 or hi < lo:
            raise HTTPException(status_code=422, detail="word_count range is invalid.")
        length_clause = f" Aim for approximately {lo}–{hi} words (about {lo // 90}–{hi // 90} minutes of reading)."

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{engine.system_prompt}\n\n---\n\n{ARTICLE_SYSTEM_PROMPT}\n\n"
                f"Write for the {sport.upper()} section of the site."
                f"{length_clause}"
            ),
        },
        {"role": "user", "content": req.instructions},
    ]

    try:
        answer, tokens = await engine.research_and_answer(db, messages, max_turns=15, reasoning=reasoning)
    except Exception as e:  # noqa: BLE001
        logger.exception("Original-article generation failed for %s", sport)
        raise HTTPException(status_code=500, detail=f"Article generation failed: {e}")

    if not answer or not answer.strip():
        raise HTTPException(status_code=502, detail="The model returned an empty article.")

    # Pull the first `# ` headline off the top as the title if present.
    title = ""
    m = re.search(r"^#\s+(.+)$", answer, flags=re.MULTILINE)
    if m:
        title = m.group(1).strip()
        # Keep the raw body intact; the frontend strips the title line when rendering.
    else:
        title = f"{sport.upper()} Original Article"

    trace = _capture_research(messages)
    now = datetime.now(timezone.utc)
    author = (req.author or "Earl").strip()
    final_word_count = len(re.findall(r"\S+", answer))
    word_lo, word_hi = req.word_count if req.word_count is not None else (None, None)

    # Persist immediately as a DRAFT so it shows up in the Edit Articles tab
    # (and so token/prompt/research provenance survives a page refresh). The
    # admin then flips it to published (or edits it) explicitly.
    draft_slug = _article_slug(now, title)

    # Ask the LLM which teams are mentioned, most-mentioned first.
    teams = await extract_teams(sport, title, answer)

    insert = await db.execute(
        text(
            """
            INSERT INTO public.original_articles
                (sport, title, summary, content, instructions, status, slug,
                 created_at, updated_at, prompt_json, research_json, author, tokens_used,
                 reasoning, word_min, word_max, word_count, teams)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'draft', :slug,
                 :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used, :reasoning, :word_lo, :word_hi, :word_count,
                 CAST(:teams AS jsonb))
            RETURNING id, sport, title, summary, content, instructions, status, slug,
                      created_at, author, tokens_used
            """
        ),
        {
            "sport": sport,
            "title": title,
            "summary": _guess_summary(answer),
            "content": answer,
            "slug": draft_slug,
            "instructions": req.instructions,
            "now": now,
            "prompt": json.dumps(trace["prompt"]),
            "research": json.dumps(trace["tool_calls"]),
            "author": author,
            "tokens_used": tokens,
            "reasoning": reasoning,
            "word_lo": word_lo,
            "word_hi": word_hi,
            "word_count": final_word_count,
            "teams": json.dumps(teams),
        },
    )
    draft_row = insert.mappings().first()
    await db.commit()

    return {
        "draft_id": draft_row["id"],
        "status": "draft",
        "sport": sport,
        "title": draft_row["title"],
        "content": draft_row["content"],
        "summary": draft_row["summary"],
        "author": draft_row["author"],
        "tokens_used": draft_row["tokens_used"],
        "tokens": tokens,
        "prompt": trace["prompt"],
        "research": trace["tool_calls"],
    }


# ──────────────────────────────────────────────
#  Publish


@router.post("/original-articles/{sport}/publish")
async def publish_original_article(
    sport: str,
    req: PublishRequest,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    now = datetime.now(timezone.utc)
    author = (req.author or "Earl").strip()
    title = req.title.strip()
    summary = (req.summary or _guess_summary(req.content))
    # Generate/refresh SEO meta unless the client passed explicit values.
    seo = {"seo_description": req.seo_description, "seo_keywords": req.seo_keywords}
    if not ((req.seo_description or "").strip() and (req.seo_keywords or "").strip()):
        seo = await _generate_seo(title, summary, req.content)
    result = await db.execute(
        text(
            """
            INSERT INTO public.original_articles
                (sport, title, summary, content, instructions, status, slug,
                 created_at, updated_at, published_at, prompt_json, research_json,
                 author, tokens_used, reasoning, word_min, word_max, word_count,
                 seo_description, seo_keywords)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'published', :slug,
                 :now, :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used, :reasoning, :word_lo, :word_hi, :word_count,
                 :seo_desc, :seo_kw)
            RETURNING id, sport, title, summary, content, instructions, status, slug,
                      published_at, created_at, author, tokens_used,
                      seo_description, seo_keywords
            """
        ),
        {
            "sport": sport,
            "title": req.title.strip(),
            "slug": _article_slug(now, req.title.strip()),
            "summary": (req.summary or _guess_summary(req.content)),
            "content": req.content,
            "instructions": req.instructions,
            "now": now,
            "prompt": json.dumps(req.prompt) if req.prompt is not None else "null",
            "research": json.dumps(req.research) if req.research is not None else "null",
            "author": author or "Earl",
            "tokens_used": req.tokens_used,
            "reasoning": (req.reasoning or "medium"),
            "word_lo": (req.word_count[0] if req.word_count else None),
            "word_hi": (req.word_count[1] if req.word_count else None),
            "word_count": len(re.findall(r"\S+", req.content)),
            "seo_desc": (seo.get("seo_description") or "").strip()[:500] or None,
            "seo_kw": (seo.get("seo_keywords") or "").strip()[:500] or None,
        },
    )
    row = result.mappings().first()
    await db.commit()
    # content_html left NULL for now; frontend renders markdown directly.
    return {"article": dict(row)}


# ──────────────────────────────────────────────
#  Public read
# ──────────────────────────────────────────────


@router.get("/original-articles/{sport}")
async def list_original_articles(
    sport: str,
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    result = await db.execute(
        text(
            """
            SELECT id, sport, title, summary, content, status, slug, published_at, created_at, author,
                   seo_description, seo_keywords, teams
            FROM public.original_articles
            WHERE sport = :sport AND status = 'published'
            ORDER BY published_at DESC
            LIMIT :limit
            """
        ),
        {"sport": sport, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings()]
    return {"sport": sport, "articles": rows}


@router.get("/original-articles/{sport}/{ref}")
async def get_original_article(
    sport: str,
    ref: str,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    # `ref` may be a numeric id (legacy) or an SEO slug (date + title).
    if ref.isdigit():
        result = await db.execute(
            text(
                """
                SELECT id, sport, title, summary, content, status, slug,
                       published_at, created_at, author, tokens_used,
                       seo_description, seo_keywords, teams
                FROM public.original_articles
                WHERE id = :aid AND sport = :sport
                """
            ),
            {"sport": sport, "aid": int(ref)},
        )
    else:
        result = await db.execute(
            text(
                """
                SELECT id, sport, title, summary, content, status, slug,
                       published_at, created_at, author, tokens_used,
                       seo_description, seo_keywords, teams
                FROM public.original_articles
                WHERE slug = :sl AND sport = :sport
                """
            ),
            {"sport": sport, "sl": ref},
        )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"article": dict(row), "slug": row["slug"]}


# Admin routes live under `/api/admin` (matching the existing admin.py
# convention) so the Next.js rewrite (/api/admin/* -> localhost:8001/api/admin/*)
# maps to them correctly. Public + generate/publish routes stay on the main
# router (no prefix), served via the catch-all /api/:path* -> /:path* rewrite.

admin_router = APIRouter(prefix="/api/admin", tags=["original-articles-admin"])


class ReEditRequest(BaseModel):
    instructions: str = Field(..., min_length=1, max_length=4000)
    include_research: bool = True


def _format_research_steps(steps: list[dict]) -> str:
    """Render stored research tool calls/results into a readable transcript block.

    Each step is {tool, arguments, result}. Results are trimmed so the context
    stays within the model's window; the model can always re-query for more.
    """
    if not steps:
        return ""
    lines: list[str] = []
    for i, st in enumerate(steps, 1):
        tool = st.get("tool") or "unknown"
        args = st.get("arguments") or {}
        result = st.get("result")
        try:
            args_txt = json.dumps(args, default=str)[:600]
        except Exception:  # noqa: BLE001
            args_txt = str(args)[:600]
        try:
            result_txt = json.dumps(result, default=str)
        except Exception:  # noqa: BLE001
            result_txt = str(result)
        # Trim oversized results to keep the transcript manageable.
        if len(result_txt) > 15000:
            result_txt = result_txt[:15000] + "…[truncated]"
        lines.append(f"### Step {i}: {tool}")
        lines.append(f"Arguments: {args_txt}")
        lines.append(f"Result: {result_txt}")
    return "\n\n".join(lines)


@admin_router.post("/original-articles/{sport}/{article_id}/re-edit")
async def re_edit_original_article(
    sport: str,
    article_id: int,
    req: ReEditRequest,
    db: AsyncSession = Depends(get_db),
):
    """Regenerate an existing article guided by the user's edit instructions.

    Loads the article's stored content and sends the user's instructions to the
    sport engine's research-and-answer loop, which may do fresh research to
    inform the rewrite. Returns the new title + content; the client persists
    via the normal PATCH endpoint.
    """
    sport = _validate_sport(sport)
    engine = ENGINES[sport]

    result = await db.execute(
        text(
            """
            SELECT id, sport, title, content, summary, author, research_json, prompt_json,
                   seo_description, seo_keywords
            FROM public.original_articles
            WHERE id = :id AND sport = :sport
            """
        ),
        {"id": article_id, "sport": sport},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found.")

    # Load the research the model already gathered when it wrote this article,
    # so we can hand it back instead of re-researching from scratch.
    steps: list[dict] = []
    if req.include_research and row["research_json"]:
        rj = row["research_json"]
        if isinstance(rj, str):
            try:
                rj = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                rj = None
        if isinstance(rj, list):
            steps = [s for s in rj if isinstance(s, dict)]
    transcript = _format_research_steps(steps)[:120000]

    system_block = (
        f"{engine.system_prompt}\n\n---\n\n{ARTICLE_SYSTEM_PROMPT}\n\n"
        f"You are revising an existing article for the {sport.upper()} "
        f"section of the site. Follow the user's revision instructions "
        f"and rewrite the article accordingly. Return the full updated "
        f"article as markdown with a `# ` title on the first line. "
        f"Do not lose information or drop sections unless instructed."
    )
    if transcript:
        system_block += (
            "\n\n---\n\n"
            "PRIOR RESEARCH ALREADY GATHERED FOR THIS ARTICLE:\n"
            f"{transcript}\n\n"
            "These are the tool results the previous draft was built from. You may "
            "rely on them to answer the revision. If the revision needs data not "
            "covered here, call the research tools to gather it — otherwise avoid "
            "duplicating research you already have."
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_block},
        {
            "role": "user",
            "content": (
                f"EXISTING ARTICLE TITLE:\n{row['title']}\n\n"
                f"EXISTING ARTICLE CONTENT:\n{row['content']}\n\n"
                f"REVISION INSTRUCTIONS:\n{req.instructions}\n\n"
                f"Rewrite the article now."
            ),
        },
    ]

    try:
        answer, tokens = await engine.research_and_answer(db, messages, max_turns=15)
    except Exception as e:  # noqa: BLE001
        logger.exception("Original-article re-edit failed for %s", sport)
        raise HTTPException(status_code=500, detail=f"Article re-edit failed: {e}")

    if not answer or not answer.strip():
        raise HTTPException(status_code=502, detail="The model returned an empty article.")

    # Pull the first `# ` headline off the top as the title if present.
    title = ""
    m = re.search(r"^#\s+(.+)$", answer, flags=re.MULTILINE)
    if m:
        title = m.group(1).strip()
    else:
        title = row["title"]

    return {
        "article_id": article_id,
        "sport": sport,
        "title": title,
        "content": answer,
        "tokens": tokens,
    }


@admin_router.get("/original-articles/{sport}")
async def admin_list_original_articles(
    sport: str,
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    result = await db.execute(
        text(
            """
            SELECT id, sport, title, summary, content, status, slug, published_at, created_at,
                   updated_at, author, tokens_used,
                   reasoning, word_min, word_max, word_count,
                   seo_description, seo_keywords,
                   (prompt_json IS NOT NULL) AS has_prompt,
                   (research_json IS NOT NULL) AS has_research,
                   jsonb_array_length(COALESCE(research_json, '[]'::jsonb)) AS research_steps
            FROM public.original_articles
            WHERE sport = :sport
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"sport": sport, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings()]
    return {"sport": sport, "articles": rows}


@admin_router.delete("/original-articles/{sport}/{article_id}")
async def delete_original_article(
    sport: str,
    article_id: int,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    result = await db.execute(
        text(
            """
            DELETE FROM public.original_articles
            WHERE id = :aid AND sport = :sport
            RETURNING id
            """
        ),
        {"sport": sport, "aid": article_id},
    )
    if not result.scalar():
        raise HTTPException(status_code=404, detail="Article not found")
    await db.commit()
    return {"deleted": article_id}


@admin_router.get("/original-articles/{sport}/{article_id}")
async def admin_get_original_article(
    sport: str,
    article_id: int,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    result = await db.execute(
        text(
            """
            SELECT id, sport, title, summary, content, instructions, status, slug,
                   published_at, created_at, updated_at, prompt_json, research_json,
                   author, tokens_used, reasoning, word_min, word_max, word_count,
                   seo_description, seo_keywords
            FROM public.original_articles
            WHERE id = :aid AND sport = :sport
            """
        ),
        {"sport": sport, "aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return {"article": dict(row)}


@admin_router.patch("/original-articles/{sport}/{article_id}")
async def update_original_article(
    sport: str,
    article_id: int,
    req: UpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)

    # Fetch current row to determine which fields change and how published_at
    # should be managed on status transitions.
    current = await db.execute(
        text(
            "SELECT id, status, title, summary, content, published_at, author "
            "FROM public.original_articles WHERE id = :aid AND sport = :sport"
        ),
        {"sport": sport, "aid": article_id},
    )
    row = current.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")

    new_status = req.status if req.status is not None else row.status
    new_title = (req.title if req.title is not None else row.title).strip()
    new_summary = req.summary if req.summary is not None else row.summary
    new_content = req.content if req.content is not None else row.content
    new_author = (req.author if req.author is not None else row.author or "Earl").strip()
    if new_summary is None:
        new_summary = _guess_summary(new_content)
    now = datetime.now(timezone.utc)

    published_at = row.published_at
    if new_status == "published" and (row.status != "published" or published_at is None):
        published_at = now
    elif new_status == "draft":
        published_at = None

    # Regenerate the SEO slug when title or publish date changes.
    slug_changed = (
        (req.title is not None and req.title.strip() != row.title)
        or (published_at != row.published_at)
    )
    if req.title is not None:
        row.title = new_title
    if slug_changed:
        await _assign_unique_slug(db, sport, article_id, _article_slug(published_at, new_title))

    # SEO: persist explicit values, else (re)generate on publish/content/title change.
    seo_desc = req.seo_description if req.seo_description is not None else None
    seo_kw = req.seo_keywords if req.seo_keywords is not None else None
    content_or_title_changed = (
        (req.content is not None and req.content != row.content)
        or (req.title is not None and req.title.strip() != row.title)
    )
    # Regenerate SEO on publish/content/title change, OR when the client explicitly
    # cleared the fields (requests a fresh LLM write).
    seo_wants_refresh = not ((seo_desc or "").strip() and (seo_kw or "").strip())
    if new_status == "published" and (content_or_title_changed or seo_wants_refresh):
        gen = await _generate_seo(new_title, new_summary, new_content)
        seo_desc = seo_desc or (gen.get("seo_description") or "")
        seo_kw = seo_kw or (gen.get("seo_keywords") or "")

    # Re-extract mentioned teams (most-mentioned first) if the body changed.
    new_teams = None
    if req.content is not None and req.content != row.content:
        new_teams = await extract_teams(sport, new_title, new_content)

    # -- start --
    result = await db.execute(
        text(
            """
            UPDATE public.original_articles
            SET title = :title,
                summary = :summary,
                content = :content,
                status = :status,
                published_at = :published_at,
                updated_at = :now,
                author = :author,
                word_count = :word_count,
                reasoning = COALESCE(:reasoning, reasoning),
                word_min = COALESCE(:word_lo, word_min),
                word_max = COALESCE(:word_hi, word_max),
                seo_description = COALESCE(:seo_desc, seo_description),
                seo_keywords = COALESCE(:seo_kw, seo_keywords),
                teams = COALESCE(CAST(:teams AS jsonb), teams)
            WHERE id = :aid AND sport = :sport
            RETURNING id, sport, title, summary, content, status, published_at,
                      updated_at, author, tokens_used,
                      seo_description, seo_keywords
            """
        ),
        {
            "title": new_title,
            "summary": new_summary,
            "content": new_content,
            "status": new_status,
            "published_at": published_at,
            "now": now,
            "author": new_author or "Earl",
            "word_count": len(re.findall(r"\S+", new_content)),
            "reasoning": (req.reasoning if req.reasoning else None),
            "word_lo": (req.word_count[0] if req.word_count else None),
            "word_hi": (req.word_count[1] if req.word_count else None),
            "seo_desc": (seo_desc or "").strip()[:500] or None,
            "seo_kw": (seo_kw or "").strip()[:500] or None,
            "teams": json.dumps(new_teams) if new_teams is not None else None,
            "aid": article_id,
            "sport": sport,
        },
    )
    updated = result.mappings().first()
    await db.commit()
    return {"article": dict(updated)}
