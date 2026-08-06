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
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

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


class UpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    content: Optional[str] = Field(None, min_length=1)
    summary: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = Field(None, pattern="^(published|draft)$")
    author: Optional[str] = Field(None, min_length=1, max_length=100)


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

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                f"{engine.system_prompt}\n\n---\n\n{ARTICLE_SYSTEM_PROMPT}\n\n"
                f"Write for the {sport.upper()} section of the site."
            ),
        },
        {"role": "user", "content": req.instructions},
    ]

    try:
        answer, tokens = await engine.research_and_answer(db, messages, max_turns=15)
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

    # Persist immediately as a DRAFT so it shows up in the Edit Articles tab
    # (and so token/prompt/research provenance survives a page refresh). The
    # admin then flips it to published (or edits it) explicitly.
    insert = await db.execute(
        text(
            """
            INSERT INTO public.original_articles
                (sport, title, summary, content, instructions, status,
                 created_at, updated_at, prompt_json, research_json, author, tokens_used)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'draft',
                 :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used)
            RETURNING id, sport, title, summary, content, instructions, status,
                      created_at, author, tokens_used
            """
        ),
        {
            "sport": sport,
            "title": title,
            "summary": _guess_summary(answer),
            "content": answer,
            "instructions": req.instructions,
            "now": now,
            "prompt": json.dumps(trace["prompt"]),
            "research": json.dumps(trace["tool_calls"]),
            "author": author,
            "tokens_used": tokens,
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
# ──────────────────────────────────────────────


@router.post("/original-articles/{sport}/publish")
async def publish_original_article(
    sport: str,
    req: PublishRequest,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    now = datetime.now(timezone.utc)
    author = (req.author or "Earl").strip()
    result = await db.execute(
        text(
            """
            INSERT INTO public.original_articles
                (sport, title, summary, content, instructions, status,
                 created_at, updated_at, published_at, prompt_json, research_json,
                 author, tokens_used)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'published',
                 :now, :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used)
            RETURNING id, sport, title, summary, content, instructions, status,
                      published_at, created_at, author, tokens_used
            """
        ),
        {
            "sport": sport,
            "title": req.title.strip(),
            "summary": (req.summary or _guess_summary(req.content)),
            "content": req.content,
            "instructions": req.instructions,
            "now": now,
            "prompt": json.dumps(req.prompt) if req.prompt is not None else "null",
            "research": json.dumps(req.research) if req.research is not None else "null",
            "author": author or "Earl",
            "tokens_used": req.tokens_used,
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
            SELECT id, sport, title, summary, content, status, published_at, created_at, author
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


@router.get("/original-articles/{sport}/{article_id}")
async def get_original_article(
    sport: str,
    article_id: int,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    result = await db.execute(
        text(
            """
            SELECT id, sport, title, summary, content, status, published_at, created_at, author, tokens_used
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


# Admin routes live under `/api/admin` (matching the existing admin.py
# convention) so the Next.js rewrite (/api/admin/* -> localhost:8001/api/admin/*)
# maps to them correctly. Public + generate/publish routes stay on the main
# router (no prefix), served via the catch-all /api/:path* -> /:path* rewrite.

admin_router = APIRouter(prefix="/api/admin", tags=["original-articles-admin"])


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
            SELECT id, sport, title, summary, status, published_at, created_at,
                   updated_at, author, tokens_used,
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
            SELECT id, sport, title, summary, content, instructions, status,
                   published_at, created_at, updated_at, prompt_json, research_json,
                   author, tokens_used
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
                author = :author
            WHERE id = :aid AND sport = :sport
            RETURNING id, sport, title, summary, content, status, published_at,
                      updated_at, author, tokens_used
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
            "aid": article_id,
            "sport": sport,
        },
    )
    updated = result.mappings().first()
    await db.commit()
    return {"article": dict(updated)}
