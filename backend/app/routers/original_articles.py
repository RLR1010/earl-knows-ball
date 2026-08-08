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


# Shared system message used VERBATIM by BOTH the write call and the accuracy
# check for original articles. DeepSeek's input cache requires byte-identity
# from position 0 (including the system message), so the write and accuracy
# requests must start with the exact same system block for the research brief
# to come from cache. Task-specific instructions live in the USER message tail
# AFTER the shared research brief, never in the system block.
ORIGINAL_ARTICLE_SHARED_SYSTEM = (
    "You are assisting with original editorial content for Earl Knows Ball, "
    "a sports handicapping site. You write for and fact-check the sports "
    "sections (NFL, NBA, MLB).\n\n"
    "RESEARCH DATA is provided inside each request and contains the tool "
    "results (stats, standings, lines, injuries, context) gathered for the "
    "article. Every claim you write or verify must be grounded in that data. "
    "You never invent facts, numbers, or names that are not supported by the "
    "research.\n\n"
    "Follow the specific instructions in the request that follow the research "
    "data."
)


def _visibility_instructions(visibility: str) -> str:
    """Return the audience/betting-advice guidance block for article writing.
    Public articles must NEVER give betting advice or picks; premium articles may.
    """
    vis = (visibility or "public").lower()
    if vis == "premium":
        return (
            "\n\nAUDIENCE: This is a PREMIUM article for paying members. "
            "You may reference Earl's picks on specific games and give clear "
            "betting advice and opinions on bets (side, over/under, moneyline, "
            "props). You may make a case for a bet. You still must base every "
            "claim on the data you retrieve."
        )
    return (
        "\n\nAUDIENCE & CRITICAL RULE: This is a FREE public article. "
        "It must NEVER give betting advice, betting opinions, or picks — no "
        "recommendations to bet a side, the over/under, the moneyline, props, "
        "or any wager. You MAY reference odds, betting lines, and implied "
        "probabilities, and you MAY discuss how a factor could be detrimental "
        "or beneficial to a team's chances. But you must NOT tell readers what "
        "to bet on or predict a payout. Keep it analytical, not advisory."
    )


class GenerateRequest(BaseModel):
    instructions: str = Field(..., min_length=1, max_length=4000)
    model: Optional[str] = Field(None)  # optional override
    author: Optional[str] = Field(None, min_length=1, max_length=100)
    tokens_used: Optional[int] = Field(None, ge=0)
    reasoning: Optional[str] = Field(None)  # minimal | low | medium | high | xhigh
    word_count: Optional[tuple[int, int]] = Field(None)  # (min_words, max_words)
    visibility: str = Field("public", pattern="^(public|premium)$")


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
    visibility: str = Field("public", pattern="^(public|premium)$")


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
    visibility: Optional[str] = Field(None, pattern="^(public|premium)$")


def _validate_sport(sport: str) -> str:
    sport = sport.lower()
    if sport not in SPORTS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported sport '{sport}'. Must be one of {list(SPORTS)}.",
        )
    return sport


def _original_normalize_json(v):
    """Normalize a JSON/JSONB value (dict, list, or JSON string) to Python."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:  # noqa: BLE001
            return v
    return v


def _original_acc_to_inaccuracy(v) -> bool:
    """Return True if a stored accuracy_check flags remaining findings."""
    ac = _original_normalize_json(v)
    if not isinstance(ac, dict):
        return False
    if ac.get("skipped"):
        return False
    if "has_inaccuracy" in ac:
        return bool(ac.get("has_inaccuracy"))
    return bool(ac.get("findings")) and not bool(ac.get("passed"))


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
    """Return {seo_description, seo_keywords} using the DeepSeek LLM.

    DeepSeek intermittently returns empty/unparseable output, so we retry up to
    MAX_SEO_ATTEMPTS (default 3) attempts when the extracted result is empty.
    """
    if not settings.deepseek_api_key:
        return {}
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=60.0,
    )
    body = (content or "")[:4000]
    max_attempts = int(getattr(settings, "MAX_SEO_ATTEMPTS", 3) or 3)
    for attempt in range(1, max_attempts + 1):
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
            logger.warning(
                "SEO generation failed for %r (attempt %d/%d): %s",
                title, attempt, max_attempts, e,
            )
            if attempt == max_attempts:
                return {}
            continue
        data = _extract_seo_json(raw)
        result = {
            "seo_description": (data.get("seo_description") or "").strip()[:500],
            "seo_keywords": (data.get("seo_keywords") or "").strip()[:500],
        }
        if result["seo_description"] and result["seo_keywords"]:
            return result
        if attempt < max_attempts:
            logger.info(
                "SEO output empty for %r (attempt %d/%d); retrying",
                title, attempt, max_attempts,
            )
    return {"seo_description": "", "seo_keywords": ""}


ORIGINAL_ARTICLE_ACCURACY_SYSTEM_PROMPT = (
    "You are the final fact-checker for a sports editorial article. Your ONLY "
    "job is to verify the article against the RESEARCH DATA provided. You do "
    "NOT rewrite, edit, or improve it.\n\n"
    "Check EVERY factual claim, statistic, player, coach, team, venue, date, "
    "and specific number against the research. A claim is a problem if EITHER:\n"
    "  1. FACT-NOT-IN-RESEARCH — the article asserts a specific fact, stat, or "
    "     names a person/team that does not appear anywhere in the research.\n"
    "  2. CONTRADICTS-RESEARCH — the article states a number or name that "
    "     conflicts with the research.\n"
    "Only flag claims the article treats as true facts. Do not flag general "
    "prose or reasonable color. Do not flag facts that ARE present in the "
    "research.\n\n"
    "NO-PREDICTIONS RULE:\n"
    "This is an editorial article that goes on the public site. It must NOT "
    "make any betting prediction, pick, or recommendation. Verify the article "
    "contains NO: point-spread or ATS pick, moneyline pick, over/under pick, "
    "projected or final score, betting recommendation, or any statement telling "
    "a reader to bet or that a specific team will win.\n\n"
    "Return a JSON object ONLY (no markdown) with this shape:\n"
    "{\"passed\": true|false, \"findings\": "
    "[{\"type\": \"fact-not-in-research\" | \"contradicts-research\" | "
    "\"prediction\", \"claim\": \"exact text from article\", "
    "\"section\": \"body\" | \"title\" | \"summary\", "
    "\"detail\": \"explain which research field it should match or rule it broke\"}]}\n"
    "passed=true only if there are NO findings."
)


ORIGINAL_ARTICLE_ACCURACY_SYSTEM_PROMPT_PREMIUM = (
    "You are the final fact-checker for a sports editorial article. Your ONLY "
    "job is to verify the article against the RESEARCH DATA provided. You do "
    "NOT rewrite, edit, or improve it.\n\n"
    "Check EVERY factual claim, statistic, player, coach, team, venue, date, "
    "and specific number against the research. A claim is a problem if EITHER:\n"
    "  1. FACT-NOT-IN-RESEARCH — the article asserts a specific fact, stat, or "
    "     names a person/team that does not appear anywhere in the research.\n"
    "  2. CONTRADICTS-RESEARCH — the article states a number or name that "
    "     conflicts with the research.\n"
    "Only flag claims the article treats as true facts. Do not flag general "
    "prose or reasonable color. Do not flag facts that ARE present in the "
    "research.\n\n"
    "This is a PREMIUM article for paying members. It MAY reference Earl's "
    "picks on games and give betting advice and opinions. Therefore betting "
    "picks, predictions, and recommendations are ALLOWED and are NOT a finding. "
    "A premium article is still fact-checked ONLY for factual accuracy against "
    "the research.\n\n"
    "Return a JSON object ONLY (no markdown) with this shape:\n"
    "{\"passed\": true|false, \"findings\": "
    "[{\"type\": \"fact-not-in-research\" | \"contradicts-research\", "
    "\"claim\": \"exact text from article\", "
    "\"section\": \"body\" | \"title\" | \"summary\", "
    "\"detail\": \"explain which research field it should match or rule it broke\"}]}\n"
    "passed=true only if there are NO factual findings."
)


def _accuracy_task_tail(visibility: str) -> str:
    """Return the accuracy-check instructions (the part that differs from the
    write task) to append AFTER the shared research brief in the user message.

    This keeps the shared system + research prefix byte-identical between the
    write call and the accuracy call so DeepSeek serves the research from input
    cache. The public and premium variants preserve the betting-language rules.
    """
    if (visibility or "public").lower() == "premium":
        return (
            "\n\n=== TASK: FACT-CHECK THE ARTICLE ===\n"
            "You are the final fact-checker for a sports editorial article. Your ONLY "
            "job is to verify the article against the RESEARCH DATA provided above. You do "
            "NOT rewrite, edit, or improve it.\n\n"
            "Check EVERY factual claim, statistic, player, coach, team, venue, date, "
            "and specific number against the research. A claim is a problem if EITHER:\n"
            "  1. FACT-NOT-IN-RESEARCH — the article asserts a specific fact, stat, or "
            "     names a person/team that does not appear anywhere in the research.\n"
            "  2. CONTRADICTS-RESEARCH — the article states a number or name that "
            "     conflicts with the research.\n"
            "Only flag claims the article treats as true facts. Do not flag general "
            "prose or reasonable color. Do not flag facts that ARE present in the "
            "research.\n\n"
            "This is a PREMIUM article for paying members. It MAY reference Earl's "
            "picks on games and give betting advice and opinions. Therefore betting "
            "picks, predictions, and recommendations are ALLOWED and are NOT a finding. "
            "A premium article is still fact-checked ONLY for factual accuracy against "
            "the research.\n\n"
            "Return a JSON object ONLY (no markdown) with this shape:\n"
            '{"passed": true|false, "findings": '
            '[{"type": "fact-not-in-research" | "contradicts-research", '
            '"claim": "exact text from article", "section": "body" | "title" | "summary", '
            '"detail": "explain which research field it should match"}]}\n'
            "passed=true only if there are NO factual findings."
        )
    return (
        "\n\n=== TASK: FACT-CHECK THE ARTICLE ===\n"
        "You are the final fact-checker for a sports editorial article. Your ONLY "
        "job is to verify the article against the RESEARCH DATA provided above. You do "
        "NOT rewrite, edit, or improve it.\n\n"
        "Check EVERY factual claim, statistic, player, coach, team, venue, date, "
        "and specific number against the research. A claim is a problem if EITHER:\n"
        "  1. FACT-NOT-IN-RESEARCH — the article asserts a specific fact, stat, or "
        "     names a person/team that does not appear anywhere in the research.\n"
        "  2. CONTRADICTS-RESEARCH — the article states a number or name that "
        "     conflicts with the research.\n"
        "Only flag claims the article treats as true facts. Do not flag general "
        "prose or reasonable color. Do not flag facts that ARE present in the "
        "research.\n\n"
        "NO-PREDICTIONS RULE:\n"
        "This is an editorial article that goes on the public site. It must NOT "
        "make any betting prediction, pick, or recommendation. Verify the article "
        "contains NO: point-spread or ATS pick, moneyline pick, over/under pick, "
        "projected or final score, betting recommendation, or any statement telling "
        "a reader to bet or that a specific team will win.\n\n"
        "Return a JSON object ONLY (no markdown) with this shape:\n"
        '{"passed": true|false, "findings": '
        '[{"type": "fact-not-in-research" | "contradicts-research" | "prediction", '
        '"claim": "exact text from article", "section": "body" | "title" | "summary", '
        '"detail": "explain which research field it should match or rule it broke"}]}\n'
        "passed=true only if there are NO findings."
    )


def _correction_task_tail(visibility: str) -> str:
    """Return the correction instructions (the part that differs from the write
    and accuracy tasks) to append AFTER the shared research brief in the user
    message, preserving the shared cache prefix.
    """
    common = (
        "\n\n=== TASK: FIX THE FLAGGED FINDINGS ===\n"
        "You are a careful sports editor making minimal corrections to an editorial "
        "article. You only fix the EXACT problems listed below. Do not rewrite, "
        "embellish, or change anything else. Do not invent new facts, numbers, "
        "names, or teams. Output ONLY the corrected article as a JSON object with "
        "the same keys you were given (e.g. {\"title\": \"...\", \"summary\": "
        "\"...\", \"content\": \"...\"}), no markdown.\n\n"
        "RULES BY FINDING TYPE:\n"
        "- 'fact-not-in-research': the claim cannot be traced to the research. "
        "  DELETE the claim (the whole sentence is best). Do NOT keep, rephrase, "
        "  or soften it — a fact that isn't in the research cannot be made true by "
        "  rewording. Remove it outright.\n"
        "- 'contradicts-research': the claim conflicts with the research. REWRITE "
        "  it to match the research exactly.\n"
    )
    if (visibility or "public").lower() == "premium":
        return common + (
            "This is a premium article: betting advice, picks, and recommendations are "
            "ALLOWED. Never remove or soften betting language — it is not a problem.\n"
        )
    return common + (
        "- 'prediction': REMOVE the betting pick/score/prediction entirely. This is "
        "  a public editorial article — no betting advice may remain.\n"
    )


def _deterministic_research_brief(research_trace: list) -> str:
    """Build a stable, deterministic research brief string from the tool trace.

    The order and formatting depend only on the content of research_trace, so
    the same research always produces byte-identical output. This is what lets
    the write call and the accuracy call share an identical leading prefix and
    get DeepSeek input-cache hits on all the research tokens.

    Args:
        research_trace: list of dicts with 'tool', 'arguments', 'result'.

    Returns:
        A flat "=== RESEARCH DATA ===\n..." string (deterministic).
    """
    parts: list[str] = ["=== RESEARCH DATA ==="]
    for i, step in enumerate(research_trace):
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool") or step.get("name") or f"tool_{i}")
        args = step.get("arguments") or step.get("args") or {}
        result = step.get("result")
        parts.append(f"[{i}] {tool}")
        if isinstance(args, dict) and args:
            parts.append(f"    args: {json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}")
        if result is not None:
            if isinstance(result, (dict, list)):
                parts.append(f"    result: {json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)}")
            else:
                parts.append(f"    result: {result}")
    return "\n".join(parts)


async def _write_original_article(
    sport: str,
    engine: Any,
    instructions: str,
    research_brief: str,
    visibility: str = "public",
    reasoning: str | None = None,
    word_count: tuple | None = None,
) -> tuple[str, str, int]:
    """Write an original article using the deterministic research brief.

    The user message starts with the SAME {research_brief} string that the
    accuracy check reuses, so DeepSeek serves all the research tokens from its
    input cache (matching bytes after the system message) on the accuracy call.

    Returns (title, content, tokens_used).
    """
    if not settings.deepseek_api_key:
        return f"{sport.upper()} Original Article", "", 0
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=300.0,
    )
    length_clause = ""
    if word_count is not None and word_count[0] and word_count[1]:
        lo, hi = word_count
        length_clause = f" Aim for approximately {lo}–{hi} words."

    # Shared system is byte-identical to the accuracy check's system. The full
    # writing task (article instructions + visibility rules) lives in the user
    # message tail AFTER the research brief so the shared prefix (system +
    # research) is identical between write and accuracy -> DeepSeek cache hit.
    system = ORIGINAL_ARTICLE_SHARED_SYSTEM
    write_tail = (
        f"\n\n=== ARTICLE TO WRITE ===\n{instructions}\n\n"
        f"Write for the {sport.upper()} section of the site."
        f"{length_clause}"
        f"{_visibility_instructions(visibility)}"
        f"\n\nReturn the article. If you include a `# Heading`, put it on its "
        f"own first line."
    )
    user = f"{research_brief}{write_tail}\n"

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    # Match the engine's reasoning semantics: thinking enabled + reasoning_effort.
    # research_and_answer uses reasoning_effort (reasoning override or "low").
    extra_body: dict[str, Any] = {
        "thinking": {"type": "enabled"},
        "reasoning_effort": reasoning or "low",
    }

    resp = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=messages,
        temperature=0.3,
        max_tokens=16000,
        extra_body=extra_body,
    )
    raw = resp.choices[0].message.content or ""
    tokens = resp.usage.total_tokens if resp.usage else 0

    title = ""
    m = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
    if m:
        title = m.group(1).strip()
    if not title:
        title = f"{sport.upper()} Original Article"
    return title, raw, tokens


async def _verify_original_accuracy(
    title: str, content: str, research_trace: list, visibility: str = "public", research_brief: str | None = None
) -> tuple[dict, int]:
    """Post-draft fact-check of an original article against its research trace.

    Returns (accuracy_check_dict, tokens_used). If the check can't run (no
    API key / failure), returns ({'passed': False, 'error': ...}, 0).
    public and premium articles are checked differently: premium articles are
    allowed to discuss/give betting advice, so the no-predictions rule only
    applies to public articles.

    Args:
        title: Article title.
        content: Article body.
        research_trace: list of tool steps (tool/arguments/result).
        visibility: 'public' or 'premium'.
        research_brief: optional deterministic research string. When supplied,
            it is used verbatim as the leading \"=== RESEARCH DATA ===\" block
            so the accuracy request shares a byte-identical prefix with the
            write request that produced this article. This gives DeepSeek
            input-cache hits on all research tokens. If None, a fresh brief is
            built from research_trace (still deterministic, but only useful for
            cache hits if the write call built it the same way).
    """
    if not settings.deepseek_api_key:
        return ({"passed": False, "skipped": True, "error": "no api key"}, 0)
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=90.0,
    )
    research_text = research_brief if research_brief is not None else _deterministic_research_brief(research_trace)
    if not research_text.strip():
        research_text = "=== RESEARCH DATA ===\n(no research captured)"
    article_text = f"TITLE:\n{title}\n\nBODY:\n{(content or '')[:28000]}"
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {
                    "role": "system",
                    "content": ORIGINAL_ARTICLE_SHARED_SYSTEM,
                },
                {
                    "role": "user",
                    "content": f"{research_text}\n\n=== ARTICLE TO VERIFY ===\n{article_text}"
                    f"{_accuracy_task_tail(visibility)}",
                },
            ],
            temperature=0.0,
            max_tokens=1600,
        )
        raw = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0
    except Exception as e:  # noqa: BLE001
        logger.warning("Original-article accuracy check failed: %s", e)
        return ({"passed": False, "error": str(e)[:300]}, 0)

    data = _extract_seo_json(raw)
    if not isinstance(data, dict):
        data = {}
    passed = bool(data.get("passed"))
    findings = data.get("findings")
    if not isinstance(findings, list):
        # Fallback: run a lightweight local no-predictions test if parse failed.
        # This fallback only flags betting/prediction language for PUBLIC
        # articles. Premium articles are allowed to discuss and recommend bets.
        artifacts = [
            "spread", "against the spread", "ats ", "moneyline", "over/under",
            "over under", "pick:", "pick: ", "bet ", "to cover", "projected score",
            "final score", "win outright", "fade ", "tail "
        ]
        findings = []
        if (visibility or "public").lower() != "premium":
            lowered = (title + " " + (content or "")).lower()
            for art in artifacts:
                if art.strip() in lowered:
                    findings.append(
                        {
                            "type": "prediction",
                            "claim": "",
                            "section": "body",
                            "detail": f"Possible betting/prediction language detected: \"{art}\"",
                        }
                    )
        passed = len(findings) == 0 and bool(data.get("passed", True))
    return (
        {"passed": passed, "findings": findings, "raw": raw, "tokens": tokens},
        tokens,
    )


ORIGINAL_ARTICLE_CORRECT_SYSTEM_PROMPT = (
    "You are a careful sports editor making minimal corrections to an editorial "
    "article. You only fix the EXACT problems listed below. Do not rewrite, "
    "embellish, or change anything else. Do not invent new facts, numbers, "
    "names, or teams. Output ONLY the corrected article as a JSON object with "
    "the same keys you were given (e.g. {\"title\": \"...\", \"summary\": "
    "\"...\", \"content\": \"...\"}), no markdown.\n\n"
    "RULES BY FINDING TYPE:\n"
    "- 'fact-not-in-research': the claim cannot be traced to the research. "
    "  DELETE the claim (the whole sentence is best). Do NOT keep, rephrase, "
    "  or soften it — a fact that isn't in the research cannot be made true by "
    "  rewording. Remove it outright.\n"
    "- 'contradicts-research': the claim conflicts with the research. REWRITE "
    "  it to match the research exactly.\n"
    "- 'prediction': REMOVE the betting pick/score/prediction entirely. This is "
    "  a public editorial article — no betting advice may remain.\n"
)


ORIGINAL_ARTICLE_CORRECT_SYSTEM_PROMPT_PREMIUM = (
    "You are a careful sports editor making minimal corrections to a premium "
    "editorial article. You only fix the EXACT problems listed below. Do not "
    "rewrite, embellish, or change anything else. Do not invent new facts, "
    "numbers, names, or teams. Output ONLY the corrected article as a JSON "
    "object with the same keys you were given (e.g. {\"title\": \"...\", "
    "\"summary\": \"...\", \"content\": \"...\"}), no markdown.\n\n"
    "RULES BY FINDING TYPE:\n"
    "- 'fact-not-in-research': the claim cannot be traced to the research. "
    "  DELETE the claim (the whole sentence is best). Do NOT keep, rephrase, "
    "  or soften it — a fact that isn't in the research cannot be made true by "
    "  rewording. Remove it outright.\n"
    "- 'contradicts-research': the claim conflicts with the research. REWRITE "
    "  it to match the research exactly.\n"
    "This is a premium article: betting advice, picks, and recommendations are "
    "ALLOWED. Never remove or soften betting language — it is not a problem.\n"
)


async def _correct_original_article(
    title: str,
    content: str,
    summary: str,
    research_trace: list,
    findings: list,
    visibility: str = "public",
    research_brief: str | None = None,
) -> dict | None:
    """Ask the LLM to fix the flagged accuracy findings in a draft article.

    Returns {"title", "summary", "content"} or None on failure.
    """
    if not settings.deepseek_api_key:
        return None
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=120.0,
    )
    visibility = (visibility or "public").lower()
    research_text = research_brief if research_brief is not None else _deterministic_research_brief(research_trace)
    if not research_text.strip():
        research_text = "=== RESEARCH DATA ===\n(no research captured)"
    findings_text = json.dumps(findings, default=str)
    article_json = json.dumps(
        {"title": title, "summary": summary, "content": content},
        default=str,
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {
                    "role": "system",
                    "content": ORIGINAL_ARTICLE_SHARED_SYSTEM,
                },
                {
                    "role": "user",
                    "content": f"{research_text}"
                    f"{_correction_task_tail(visibility)}\n\n"
                    "=== PROBLEMS TO FIX ===\n"
                    f"{findings_text}\n\n"
                    "=== CURRENT ARTICLE (correct this) ===\n"
                    f"{article_json}",
                },
            ],
            temperature=0.0,
            max_tokens=6000,
        )
        raw = resp.choices[0].message.content or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("Original-article correction failed: %s", e)
        return None

    data = _extract_seo_json(raw)
    if not isinstance(data, dict):
        return None
    corrected_title = (str(data.get("title") or "").strip()) or title
    corrected_summary = (str(data.get("summary") or "").strip()) or summary
    corrected_content = str(data.get("content") or "").strip() or content
    return {
        "title": corrected_title,
        "summary": corrected_summary,
        "content": corrected_content,
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

    research_system_prefix = (
        f"{engine.system_prompt}\n\n---\n\n{ARTICLE_SYSTEM_PROMPT}\n\n"
        f"Research for the {sport.upper()} section of the site."
        f"{_visibility_instructions(req.visibility)}"
        f"\n\nCRITICAL: You are in RESEARCH-ONLY mode. Use the available tools to "
        f"gather all data and facts needed to write the requested article. Call "
        f"as many tools as needed to fully cover the topic. When you have enough "
        f"research, STOP calling tools and reply with a short bulleted digest of "
        f"the key data you gathered. Do NOT write the full article yet."
    )
    gather_messages: list[dict[str, Any]] = [
        {"role": "system", "content": research_system_prefix},
        {"role": "user", "content": req.instructions},
    ]

    try:
        # Phase A: research-only tool loop. Returns (digest, tokens, full_messages).
        _digest, research_tokens, full_messages = await engine.research_and_answer(
            db, gather_messages, max_turns=15,
            reasoning=reasoning, timeout=300.0,
            return_full_messages=True, research_only=True,
        )
        tokens = research_tokens
    except Exception as e:  # noqa: BLE001
        logger.exception("Original-article research failed for %s", sport)
        raise HTTPException(status_code=500, detail=f"Article research failed: {e}")

    trace = _capture_research(full_messages)
    research_trace = trace.get("tool_calls") or []
    # One deterministic research brief used verbatim by BOTH the write call and
    # every accuracy/correction call, so DeepSeek serves the research from input
    # cache (byte-identical user-message prefix) instead of re-billing it.
    research_brief = _deterministic_research_brief(research_trace)

    try:
        # Phase B: single write call whose user message starts with the brief.
        title, answer, write_tokens = await _write_original_article(
            sport, engine, req.instructions, research_brief,
            req.visibility, reasoning, req.word_count,
        )
        tokens += write_tokens
    except Exception as e:  # noqa: BLE001
        logger.exception("Original-article write failed for %s", sport)
        raise HTTPException(status_code=500, detail=f"Article writing failed: {e}")

    if not answer or not answer.strip():
        raise HTTPException(status_code=502, detail="The model returned an empty article.")

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

    # Post-draft accuracy verification + correction loop.
    # If the accuracy check flags findings, run up to MAX_ORIGINAL_CORRECTION_PASSES
    # correction passes, re-verifying each time. Every rejected draft is
    # snapshotted into rejection_history for audit. No programmatic stripping.
    # (research_trace + research_brief were already built from the Phase A call
    # above; the same brief is reused for accuracy + every correction call so
    # DeepSeek serves the research from input cache.)
    accuracy_check, accuracy_tokens = await _verify_original_accuracy(
        title, answer, research_trace, req.visibility, research_brief=research_brief
    )
    rejection_history = []
    retries_used = 0
    summary = _guess_summary(answer)
    max_passes = int(getattr(settings, "MAX_ORIGINAL_CORRECTION_PASSES", 2) or 2)
    has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")
    while has_findings and retries_used < max_passes:
        rejection_history.append({
            "attempt": retries_used + 1,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "accuracy_check": accuracy_check,
            "title": title,
            "content": answer,
            "summary": summary,
        })
        corrected = await _correct_original_article(
            title, answer, summary, research_trace, accuracy_check.get("findings") or [], req.visibility, research_brief
        )
        retries_used += 1
        if not corrected:
            break
        title = corrected["title"]
        answer = corrected["content"]
        summary = corrected["summary"]
        accuracy_check, accuracy_tokens = await _verify_original_accuracy(
            title, answer, research_trace, req.visibility, research_brief=research_brief
        )
        has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")
    accuracy_check["retries_used"] = retries_used
    accuracy_check["has_inaccuracy"] = has_findings
    accuracy_check["accuracy_pass"] = not has_findings

    # Build SEO meta for the final (post-correction) article and persist it now,
    # so every generated article has seo_description/seo_keywords saved (not just
    # after a later publish). _generate_seo retries up to MAX_SEO_ATTEMPTS.
    seo_meta = await _generate_seo(title, summary, answer)

    insert = await db.execute(
        text(
            """
            INSERT INTO public.original_articles
                (sport, title, summary, content, instructions, status, slug,
                 created_at, updated_at, prompt_json, research_json, author, tokens_used,
                 reasoning, word_min, word_max, word_count, teams,
                 accuracy_check, accuracy_check_tokens, rejection_history, visibility,
                 seo_description, seo_keywords)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'draft', :slug,
                 :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used, :reasoning, :word_lo, :word_hi, :word_count,
                 CAST(:teams AS jsonb),
                 CAST(:accuracy AS jsonb), :accuracy_tokens, CAST(:rej AS jsonb), :visibility,
                 :seo_description, :seo_keywords)
            RETURNING id, sport, title, summary, content, instructions, status, slug,
                      created_at, author, tokens_used, visibility,
                      seo_description, seo_keywords
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
            "accuracy": json.dumps(accuracy_check if isinstance(accuracy_check, dict) else {}),
            "accuracy_tokens": accuracy_tokens,
            "rej": json.dumps(rejection_history or []),
            "visibility": (req.visibility or "public").lower(),
            "seo_description": (seo_meta.get("seo_description") or "")[:500],
            "seo_keywords": (seo_meta.get("seo_keywords") or "")[:500],
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
        "visibility": (req.visibility or "public").lower(),
        "tokens_used": draft_row["tokens_used"],
        "tokens": tokens,
        "accuracy_check": accuracy_check,
        "accuracy_check_tokens": accuracy_tokens,
        "rejection_history": rejection_history or [],
        "seo_description": draft_row["seo_description"],
        "seo_keywords": draft_row["seo_keywords"],
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
                 seo_description, seo_keywords, visibility)
            VALUES
                (:sport, :title, :summary, :content, :instructions, 'published', :slug,
                 :now, :now, :now, CAST(:prompt AS jsonb), CAST(:research AS jsonb),
                 :author, :tokens_used, :reasoning, :word_lo, :word_hi, :word_count,
                 :seo_desc, :seo_kw, :visibility)
            RETURNING id, sport, title, summary, content, instructions, status, slug,
                      published_at, created_at, author, tokens_used,
                      seo_description, seo_keywords, visibility
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
            "visibility": (req.visibility or "public").lower(),
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
                   seo_description, seo_keywords, teams, visibility
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
                       seo_description, seo_keywords, teams, visibility
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
                       seo_description, seo_keywords, teams, visibility
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
                   seo_description, seo_keywords, visibility
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
        f"{_visibility_instructions(row['visibility'] or 'public')}"
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
        answer, tokens = await engine.research_and_answer(db, messages, max_turns=15, timeout=300.0)
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


class TitleRegenRequest(BaseModel):
    include_research: bool = True
    extra: str = Field("", max_length=1000)  # optional user guidance for the new title


@admin_router.post("/original-articles/{sport}/{article_id}/regenerate-title")
async def regenerate_original_article_title(
    sport: str,
    article_id: int,
    req: TitleRegenRequest,
    db: AsyncSession = Depends(get_db),
):
    """Regenerate ONLY the article's title via the DeepSeek LLM.

    Uses the existing content (and optionally the stored research) to craft a
    fresh title. Returns just {title}. The client persists it via PATCH so no
    other columns are touched.
    """
    sport = _validate_sport(sport)
    engine = ENGINES[sport]

    result = await db.execute(
        text(
            """
            SELECT id, sport, title, content, summary, research_json, visibility
            FROM public.original_articles
            WHERE id = :id AND sport = :sport
            """
        ),
        {"id": article_id, "sport": sport},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found.")

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
    transcript = _format_research_steps(steps)[:80000]

    body = (row["content"] or "").strip()
    system_block = (
        f"You are a headline writer for the {sport.upper()} section of a sports "
        f"site. Given an existing article, write ONE strong, specific, engaging "
        f"news headline. Follow this critically:\n"
        f"- It must accurately reflect the article's content.\n"
        f"- It must be factually grounded in the article and research.\n"
        f"{_visibility_instructions(row['visibility'] or 'public')}"
        f"\nReturn ONLY the headline text with no markdown, no quotes, and no "
        f"explanation."
    )
    user_content = (
        f"EXISTING TITLE:\n{row['title']}\n\n"
        f"ARTICLE CONTENT:\n{body[:8000]}\n\n"
    )
    if req.extra.strip():
        user_content += f"\nTITLE GUIDANCE FROM WRITER:\n{req.extra.strip()}\n\n"
    user_content += "Write the new headline now."
    if transcript:
        user_content += (
            f"\n\nPRIOR RESEARCH (for reference only):\n{transcript}"
        )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_block},
        {"role": "user", "content": user_content},
    ]

    # Simple direct completion (no tool loop needed for a headline).
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=45.0,
    )
    if not settings.deepseek_api_key:
        raise HTTPException(status_code=503, detail="DeepSeek API key not configured.")

    title = None
    for attempt in range(1, 4):
        try:
            resp = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=messages,
                temperature=0.7,
                max_tokens=120,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("Title regen failed (attempt %d/3): %s", attempt, e)
            if attempt == 3:
                raise HTTPException(status_code=502, detail=f"Title generation failed: {e}")
            continue
        candidate = re.sub(r"^#{1,6}\s+", "", raw.strip(), flags=re.MULTILINE).strip()
        candidate = candidate.strip("\"'").strip()
        candidate = re.sub(r"\s+", " ", candidate)
        if candidate:
            title = candidate[:300]
            break
    if not title:
        raise HTTPException(status_code=502, detail="The model returned an empty title.")

    return {"article_id": article_id, "sport": sport, "title": title}



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
                   visibility,
                   (prompt_json IS NOT NULL) AS has_prompt,
                   (research_json IS NOT NULL) AS has_research,
                   jsonb_array_length(COALESCE(research_json, '[]'::jsonb)) AS research_steps,
                   accuracy_check
            FROM public.original_articles
            WHERE sport = :sport
            ORDER BY created_at DESC
            LIMIT :limit
            """
        ),
        {"sport": sport, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings()]
    for row in rows:
        ac = row.pop("accuracy_check", None)
        row["has_inaccuracy"] = _original_acc_to_inaccuracy(ac)
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
                   seo_description, seo_keywords, visibility,
                   accuracy_check, accuracy_check_tokens, rejection_history
            FROM public.original_articles
            WHERE id = :aid AND sport = :sport
            """
        ),
        {"sport": sport, "aid": article_id},
    )
    row = result.mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    data = dict(row)
    data["accuracy_check"] = _original_normalize_json(data.get("accuracy_check"))
    data["rejection_history"] = _original_normalize_json(data.get("rejection_history")) or []
    return {"article": data}


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
    row = dict(row)

    new_status = req.status if req.status is not None else row["status"]
    new_title = (req.title if req.title is not None else row["title"]).strip()
    new_summary = req.summary if req.summary is not None else row["summary"]
    new_content = req.content if req.content is not None else row["content"]
    new_author = (req.author if req.author is not None else row["author"] or "Earl").strip()
    if new_summary is None:
        new_summary = _guess_summary(new_content)
    now = datetime.now(timezone.utc)

    published_at = row["published_at"]
    if new_status == "published" and (row["status"] != "published" or published_at is None):
        published_at = now
    elif new_status == "draft":
        published_at = None

    # Regenerate the SEO slug when title or publish date changes.
    slug_changed = (
        (req.title is not None and req.title.strip() != row["title"])
        or (published_at != row["published_at"])
    )
    if slug_changed:
        await _assign_unique_slug(db, sport, article_id, _article_slug(published_at, new_title))

    # SEO: persist explicit values, else (re)generate on publish/content/title change.
    seo_desc = req.seo_description if req.seo_description is not None else None
    seo_kw = req.seo_keywords if req.seo_keywords is not None else None
    content_or_title_changed = (
        (req.content is not None and req.content != row["content"])
        or (req.title is not None and req.title.strip() != row["title"])
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
    if req.content is not None and req.content != row["content"]:
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
                visibility = COALESCE(:visibility, visibility),
                teams = COALESCE(CAST(:teams AS jsonb), teams)
            WHERE id = :aid AND sport = :sport
            RETURNING id, sport, title, summary, content, status, published_at,
                      updated_at, author, tokens_used,
                      seo_description, seo_keywords, visibility
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
            "visibility": (req.visibility or None),
            "teams": json.dumps(new_teams) if new_teams is not None else None,
            "aid": article_id,
            "sport": sport,
        },
    )
    updated = result.mappings().first()
    await db.commit()
    return {"article": dict(updated)}
