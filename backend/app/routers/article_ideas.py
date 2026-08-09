"""Article Ideas API — brainstorm, store, and reuse editorial article concepts.

This powers the "Article Ideas" tab in the admin original-articles tool.

Flow:
1. POST /api/admin/article-ideas/{sport}/generate   {instructions, teamFilter?}
   -> LLM returns a list of {title, description, team_id?, team_abbr?} ideas.
   NOT stored yet — shown to the author first.
2. POST /api/admin/article-ideas/{sport}/store     {ideas:[...]}
   Persist (some or all) generated ideas as 'active'.
3. POST /api/admin/article-ideas/{sport}/build-prompt  {idea_id}
   -> LLM returns a full article-prompt string (dropped straight into the
      Create Article tab, replacing the instructions box content).
4. GET  /api/admin/article-ideas/{sport}   list ideas (active + used + archived)
5. PATCH /api/admin/article-ideas/{sport}/{idea_id}   edit fields / mark used / archive
6. DELETE /api/admin/article-ideas/{sport}/{idea_id}  remove an idea
7. GET  /api/admin/article-ideas/{sport}/teams        teams for the sport
   (for the optional team-specific scoping dropdown)

Persistence uses the public.article_ideas table. When an idea is marked used,
used_at=NOW() (and optionally used_article_id) is recorded so we can trace
which ideas actually got published.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.database import get_db
from app.chat_tools.base import ToolChatEngine
from app.routers.original_articles import (
    ENGINES,
    _capture_research,
    _deterministic_research_brief,
)

logger = logging.getLogger("article_ideas")

SPORTS = ("mlb", "nfl", "nba")
admin_router = APIRouter(prefix="/api/admin/article-ideas", tags=["article-ideas-admin"])


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class IdeaModel(BaseModel):
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    team_id: Optional[int] = None
    team_abbr: Optional[str] = None
    team_name: Optional[str] = None


class GenerateIdeasRequest(BaseModel):
    instructions: str = Field("", description="Extra author guidance for the brainstorm.")
    team_filter: Optional[str] = Field(None, description="Optional team name/abbr to scope ideas to.")
    count: int = Field(6, ge=1, le=12)
    quick: bool = Field(False, description="Quick mode: skip research tools, just ideate.")


class StoreIdeasRequest(BaseModel):
    ideas: list[IdeaModel]


class BuildPromptRequest(BaseModel):
    idea_id: int


class EditIdeaRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    prompt: Optional[str] = None
    team_id: Optional[int] = None
    team_abbr: Optional[str] = None
    team_name: Optional[str] = None
    status: Optional[str] = Field(None, pattern="^(active|used|archived)$")
    used_article_id: Optional[int] = None  # pass 0 to clear, else the article id


# --------------------------------------------------------------------------- #
# LLM helpers (same DeepSeek client convention as the rest of the app)
# --------------------------------------------------------------------------- #
def _llm_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
    )


def _extract_json(text_: str) -> Any:
    """Pull a JSON array/object out of an LLM response (strip code fences)."""
    t = text_.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start = t.find("[")
    if start == -1:
        start = t.find("{")
    if start != -1:
        # Try to parse from first bracket; walk back if truncated trailing.
        for end in range(len(t), start, -1):
            if t[end - 1] in ("]", "}"):
                try:
                    return json.loads(t[start:end])
                except json.JSONDecodeError:
                    continue
    raise ValueError("Could not extract JSON from LLM response")


async def _chat(messages: list[dict], max_tokens: int = 2000, json_mode: bool = False) -> str:
    client = _llm_client()
    kwargs: dict[str, Any] = dict(
        model=settings.deepseek_model,
        messages=messages,
        temperature=0.8 if not json_mode else 0.4,
        max_tokens=max_tokens,
        extra_body={"thinking": {"type": "disabled"}},  # light gen task
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""
    except Exception as exc:  # noqa: BLE001
        logger.error("article_ideas LLM call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")


# --------------------------------------------------------------------------- #
# Grounded research loop (thinking DISABLED — this is a light/gen task)
# --------------------------------------------------------------------------- #
async def _run_research_loop(
    engine,
    db: AsyncSession,
    messages: list[dict],
    max_turns: int = 7,
    timeout: float = 300.0,
) -> tuple[list[dict], int]:
    """Run a tool-calling research loop with thinking disabled.

    Mirrors the chat engine's research loop but forces `thinking: disabled` and
    a configurable (smaller) turn cap, since ideation research is a lighter task
    than writing a full article. Returns (full_messages, total_tokens).
    """
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=timeout,
    )
    extra_body: dict = {"thinking": {"type": "disabled"}}
    total_tokens = 0

    def _append_assistant(msgs, am):
        # Store tool_calls as plain JSON-serializable dicts so the downstream
        # _capture_research helper (expects tc.get("function")) can parse them.
        tcs = None
        if am.tool_calls:
            tcs = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments or "{}",
                    },
                }
                for tc in am.tool_calls
            ]
        msgs.append({"role": "assistant", "content": am.content or "", "tool_calls": tcs})

    response = await client.chat.completions.create(
        model=engine.model,
        extra_body=extra_body,
        messages=messages,
        tools=engine.tools,
        tool_choice="auto",
    )
    if response.usage:
        total_tokens += response.usage.total_tokens
    assistant_msg = response.choices[0].message
    _append_assistant(messages, assistant_msg)

    turns = 0
    while assistant_msg.tool_calls and turns < max_turns:
        turns += 1
        logger.info(
            "article_ideas research round %d/%d: %d tool(s)",
            turns, max_turns, len(assistant_msg.tool_calls),
        )
        for tool_call in assistant_msg.tool_calls:
            try:
                result = await engine.executor(db, tool_call)
                content = json.dumps(result, default=str)
            except Exception as exc:  # noqa: BLE001
                logger.exception("article_ideas tool exec failed: %s", exc)
                content = json.dumps({"error": str(exc)})
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": content,
            })

        response = await client.chat.completions.create(
            model=engine.model,
            extra_body=extra_body,
            messages=messages,
            tools=engine.tools,
            tool_choice="auto",
        )
        if response.usage:
            total_tokens += response.usage.total_tokens
        assistant_msg = response.choices[0].message
        _append_assistant(messages, assistant_msg)

    # If we hit the turn cap with pending tool calls, clear them so the trace is
    # still parseable downstream.
    logger.info("article_ideas research done: turns=%d total_tokens=%d", turns, total_tokens)
    return messages, total_tokens


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def _row(r) -> dict:
    m = dict(r._mapping)
    # Keep created_at/updated_at as iso strings for the frontend.
    for k in ("created_at", "updated_at", "used_at"):
        if m.get(k) is not None and hasattr(m[k], "isoformat"):
            m[k] = m[k].isoformat()
    return m


def _validate_sport(sport: str) -> str:
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(status_code=400, detail=f"Unsupported sport '{sport}'.")
    return sport


# --------------------------------------------------------------------------- #
# Teams lookup (for team-specific scoping)
# --------------------------------------------------------------------------- #
@admin_router.get("/{sport}/teams")
async def list_teams(sport: str, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    rows = (
        await db.execute(
            text(
                f"SELECT id, abbreviation AS abbr, name FROM {sport}.teams "
                "ORDER BY name"
            )
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- #
# LLM brainstorm — come up with article ideas
# --------------------------------------------------------------------------- #
@admin_router.post("/{sport}/generate")
async def generate_ideas(sport: str, req: GenerateIdeasRequest, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    sport_label = {"mlb": "MLB (baseball)", "nfl": "NFL (football)", "nba": "NBA (basketball)"}[sport]

    # Gather existing titles so the LLM avoids duplicates.
    existing = (
        await db.execute(
            text("SELECT title FROM public.article_ideas WHERE sport=:s AND status != 'archived'"),
            {"s": sport},
        )
    ).fetchall()
    existing_titles = "\n".join(f"- {r[0]}" for r in existing) or "(none yet)"

    scope = ""
    if req.team_filter:
        row = (
            await db.execute(
                text(
                    f"SELECT id, abbreviation AS abbr, name FROM {sport}.teams "
                    "WHERE LOWER(name)=LOWER(:f) OR LOWER(abbreviation)=LOWER(:f) LIMIT 1"
                ),
                {"f": req.team_filter.strip()},
            )
        ).mappings().first()
        if row:
            scope = (
                f"Please scope ALL ideas to this team: {row['name']} "
                f"(fill team_id={row['id']}, team_abbr='{row['abbr']}', team_name='{row['name']}')."
            )
        else:
            scope = f"Author mentioned team '{req.team_filter}' — use it if relevant, else leave team fields null."

    user_instruction = req.instructions.strip()

    engine = ENGINES[sport]

    # ---- Phase A: grounded research --------------------------------------- #
    # Full mode: let the LLM use OUR research tools (vector/news search,
    # standings, team stats, injuries, etc.) with a light turn cap and thinking
    # DISABLED (this is a brainstorming/gen task, not a hard write). Quick mode:
    # skip research entirely and go straight to ideation.
    research_brief = None
    if not req.quick:
        research_system_prefix = (
            f"{engine.system_prompt}\n\n---\n\n"
            f"You are generating fresh, data-backed article ideas for the "
            f"{sport.upper()} section of earlknowsball.com.\n\n"
            f"CRITICAL: You are in RESEARCH-ONLY mode. Use the available tools to "
            f"gather facts, news, standings, stats, and storylines relevant to the "
            f"idea brainstorm. Call as many tools as needed to build a rich picture "
            f"of what is interesting RIGHT NOW (in-season storylines, team trends, "
            f"best/worst ATS or OU teams, injuries, pitching duels, star matchups). "
            f"When you have enough research, STOP calling tools and reply with a short "
            f"bulleted digest of the key data you gathered. Do NOT write the ideas yet."
        )
        gather_messages: list[dict] = [
            {"role": "system", "content": research_system_prefix},
            {"role": "user", "content": (
                ("Author guidance for the ideas: " + user_instruction + chr(10) if user_instruction else "")
                + (scope + chr(10) if scope else "")
                + (f"Existing idea titles to avoid duplicating:\n{existing_titles}" if existing_titles != "(none yet)" else "")
                + "\nNow research what's interesting."
            ).strip()},
        ]
        try:
            full_messages, _research_tokens = await _run_research_loop(
                engine, db, gather_messages, max_turns=7, timeout=300.0,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("article_ideas research failed for %s", sport)
            raise HTTPException(status_code=500, detail=f"Idea research failed: {exc}")

        trace = _capture_research(full_messages)
        research_trace = trace.get("tool_calls") or []
        research_brief = _deterministic_research_brief(research_trace)

    # ---- Phase B: grounded ideation --------------------------------------- #
    sys_prompt = (
        "You are an expert sports editor who generates compelling, original article ideas "
        f"for {sport_label}. You know deep stats, storylines, injuries, trends, and betting angles. "
        "You MUST return valid JSON. Return ONLY a JSON object with a single key 'ideas' whose value "
        "is an array of idea objects. No prose, no markdown fences, nothing outside the JSON. "
        "Each idea object must have EXACTLY these keys:\n"
        '{"title": string, "description": string, "team_id": number|null, "team_abbr": string|null, "team_name": string|null}\n'
        "- title: a punchy, specific article headline (15 words or fewer).\n"
        "- description: 2-4 sentences describing the angle, why it's interesting now, and the key data/storyline to explore.\n"
        "- team_*: only set when the idea is clearly about one team; otherwise null.\n"
        "- CRITICAL: Every idea MUST be grounded in the RESEARCH DATA below (real stats, standings, injuries, storylines). "
        "Do not invent facts. Reference the real numbers/trends you researched so the idea is timely and defensible.\n"
        "Make ideas diverse: mix player storylines, team trends, betting/stat angles, and matchup previews. "
        "Avoid generic filler; every idea must be data-backed and timely."
    )
    user_instruction_block = (
        f"Author guidance: {user_instruction}\n\n" if user_instruction else ""
    )
    scope_block = f"{scope}\n\n" if scope else ""
    user_prompt_parts = [
        user_instruction_block,
        scope_block,
        research_brief + "\n\n" if research_brief else "",
        f"Existing idea titles for {sport_label} (do NOT repeat these):\n{existing_titles}\n\n",
    ]
    if research_brief:
        user_prompt_parts.append(f"Generate {req.count} fresh article ideas for {sport_label}, grounded in the research above.")
    else:
        user_prompt_parts.append(f"Generate {req.count} fresh article ideas for {sport_label} (you may not have run research tools, so lean on your training knowledge, but keep ideas specific and timely).")
    user_prompt = "".join(user_prompt_parts)

    raw = await _chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=6000,
        json_mode=True,
    )
    try:
        parsed = _extract_json(raw)
        ideas = parsed.get("ideas") if isinstance(parsed, dict) else parsed
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=f"LLM returned malformed JSON: {exc}")

    if not isinstance(ideas, list):
        raise HTTPException(status_code=502, detail="LLM did not return an ideas array.")

    cleaned = []
    for idea in ideas[: req.count]:
        if not isinstance(idea, dict) or not idea.get("title"):
            continue
        cleaned.append(
            {
                "title": str(idea.get("title")).strip(),
                "description": (str(idea.get("description") or "")).strip() or None,
                "team_id": idea.get("team_id") if isinstance(idea.get("team_id"), int) else None,
                "team_abbr": idea.get("team_abbr") or None,
                "team_name": idea.get("team_name") or None,
            }
        )
    return {"ideas": cleaned}


# --------------------------------------------------------------------------- #
# Store ideas (persist generated ideas)
# --------------------------------------------------------------------------- #
@admin_router.post("/{sport}/store")
async def store_ideas(sport: str, req: StoreIdeasRequest, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    stored = []
    now = datetime.now(timezone.utc)
    for idea in req.ideas:
        res = await db.execute(
            text(
                "INSERT INTO public.article_ideas "
                "(sport, title, description, team_id, team_abbr, team_name, status, created_at, updated_at) "
                "VALUES (:sport, :title, :description, :team_id, :team_abbr, :team_name, 'active', :now, :now) "
                "RETURNING id"
            ),
            {
                "sport": sport,
                "title": idea.title,
                "description": idea.description,
                "team_id": idea.team_id,
                "team_abbr": idea.team_abbr,
                "team_name": idea.team_name,
                "now": now,
            },
        )
        stored.append(res.scalar_one())
    await db.commit()
    return {"stored_ids": stored, "count": len(stored)}


# --------------------------------------------------------------------------- #
# Build a full article prompt from an idea
# --------------------------------------------------------------------------- #
@admin_router.post("/{sport}/build-prompt")
async def build_prompt(sport: str, req: BuildPromptRequest, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    row = (
        await db.execute(
            text(
                "SELECT id, sport, title, description, team_name FROM public.article_ideas "
                "WHERE id=:id"
            ),
            {"id": req.idea_id},
        )
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Idea not found.")
    if row["sport"] != sport:
        raise HTTPException(status_code=400, detail="Idea belongs to a different sport.")

    team_ctx = f" (team: {row['team_name']})" if row["team_name"] else ""

    sys_prompt = (
        "You are a senior sportswriter for a sports-handicapping site. You write detailed, "
        "data-rich analytical articles. Return ONLY the article prompt as plain text (no surrounding prose, "
        "no markdown fences). The prompt will be pasted into an article-generation tool as its "
        "instruction box, so it must be a self-contained, explicit creative brief."
    )
    user_prompt = (
        f"Write a complete article prompt based on this idea{team_ctx}:\n\n"
        f"TITLE: {row['title']}\n"
        f"DESCRIPTION: {row['description'] or 'N/A'}\n\n"
        "The prompt must instruct the writer to:\n"
        "- Open with a strong hook then clearly state the thesis.\n"
        "- Ground every claim in statistics and current data (injuries, form, betting lines, trends).\n"
        "- Include a 'Why it matters / what to watch' section.\n"
        "- Cover both sides of the argument fairly before landing on a conclusion.\n"
        f"- Target the sport: {sport.upper()}.\n"
        "Give the writer concrete direction on structure, tone, and sections. Do NOT write the article itself."
    )

    prompt_text = (await _chat(
        [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2500,
    )).strip()

    # Persist the prompt onto the idea for reuse.
    now = datetime.now(timezone.utc)
    await db.execute(
        text(
            "UPDATE public.article_ideas SET prompt=:p, updated_at=:now WHERE id=:id"
        ),
        {"p": prompt_text, "now": now, "id": req.idea_id},
    )
    await db.commit()

    return {"idea_id": req.idea_id, "prompt": prompt_text}


# --------------------------------------------------------------------------- #
# List / edit / delete ideas
# --------------------------------------------------------------------------- #
@admin_router.get("/{sport}")
async def list_ideas(
    sport: str,
    include_archived: bool = False,
    db: AsyncSession = Depends(get_db),
):
    sport = _validate_sport(sport)
    rows = (
        await db.execute(
            text(
                "SELECT * FROM public.article_ideas WHERE sport=:s "
                "AND (:arch = TRUE OR status != 'archived') "
                "ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'used' THEN 1 ELSE 2 END, "
                "COALESCE(used_at, created_at) DESC"
            ),
            {"s": sport, "arch": include_archived},
        )
    ).fetchall()
    return [_row(r) for r in rows]


@admin_router.patch("/{sport}/{idea_id}")
async def edit_idea(sport: str, idea_id: int, req: EditIdeaRequest, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    row = (
        await db.execute(
            text("SELECT * FROM public.article_ideas WHERE id=:id AND sport=:s"),
            {"id": idea_id, "s": sport},
        )
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Idea not found.")

    now = datetime.now(timezone.utc)
    sets = ["updated_at=:now"]
    params: dict[str, Any] = {"now": now, "id": idea_id}

    for field in ("title", "description", "prompt", "team_id", "team_abbr", "team_name"):
        val = getattr(req, field)
        if val is not None:
            sets.append(f"{field}=:{field}")
            params[field] = val

    if req.status is not None:
        sets.append("status=:status")
        params["status"] = req.status
        if req.status == "used":
            sets.append("used_at=:used_at")
            params["used_at"] = now
        elif req.status in ("active", "archived"):
            # Only clear used_at when moving back to active; keep it for archived trace.
            if req.status == "active":
                sets.append("used_at=NULL")
                sets.append("used_article_id=NULL")

    if req.used_article_id is not None:
        if req.used_article_id == 0:
            sets.append("used_article_id=NULL")
        else:
            # Validate the referenced article exists and belongs to this sport.
            art = (
                await db.execute(
                    text("SELECT id FROM public.original_articles WHERE id=:aid AND sport=:s"),
                    {"aid": req.used_article_id, "s": sport},
                )
            ).fetchone()
            if not art:
                raise HTTPException(status_code=400, detail="Referenced article not found for this sport.")
            sets.append("used_article_id=:used_article_id")
            params["used_article_id"] = req.used_article_id
            # Marking used is implied by attaching an article.
            sets.append("status='used'")
            sets.append("used_at=:used_at")
            params["used_at"] = now

    await db.execute(
        text(f"UPDATE public.article_ideas SET {', '.join(sets)} WHERE id=:id"),
        params,
    )
    await db.commit()

    updated = (await db.execute(
        text("SELECT * FROM public.article_ideas WHERE id=:id"),
        {"id": idea_id},
    )).fetchone()
    return _row(updated)


@admin_router.delete("/{sport}/{idea_id}")
async def delete_idea(sport: str, idea_id: int, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    res = await db.execute(
        text("DELETE FROM public.article_ideas WHERE id=:id AND sport=:s"),
        {"id": idea_id, "s": sport},
    )
    await db.commit()
    if res.rowcount == 0:
        raise HTTPException(status_code=404, detail="Idea not found.")
    return {"deleted": True}
