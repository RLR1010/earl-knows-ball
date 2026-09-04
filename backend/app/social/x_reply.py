"""Earl reply-drafting for X posts (@earl_knows_ball).

Given a stored x_posts tweet that WE might engage with, Earl researches its context (using the
same grounded research stack as public content: vector/news retrieval, stats, standings) and
drafts 2-3 ON-BRAND X reply options. Replies are suggestions only - Rich reviews + approves in
the admin UI before anything could be posted.

Reuses the exact generation helpers from app/routers/article_ideas (_chat + _run_research_loop)
so behavior stays consistent with Earl's other content gen. Earl stays ON-BRAND (sports-culture,
betting-takes voice, hints + analysis, NO free picks, <= 280 chars/reply).
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.article_ideas import _chat, _run_research_loop
from app.routers.original_articles import (
    ENGINES,
    _capture_research,
    _deterministic_research_brief,
)

logger = logging.getLogger("x_reply")


async def load_post(db: AsyncSession, post_row_id: int) -> dict | None:
    r = (
        await db.execute(
            text(
                "SELECT id, tweet_id, author_user_id, author_username, text, created_at, "
                "likes, retweets, replies FROM public.x_posts WHERE id=:id"
            ),
            {"id": post_row_id},
        )
    ).mappings().first()
    return dict(r) if r else None


async def draft_replies_for_post(db: AsyncSession, post: dict, n: int = 3) -> list[dict]:
    """Research the post's context then draft up to n on-brand reply options.

    Returns list of {body, rationale}. Pure generation - caller persists.
    """
    engine = ENGINES["all"]
    post_text = (post.get("text") or "").strip()
    author = post.get("author_username") or "X"
    created = post.get("created_at")

    # ---- Phase A: grounded research so the reply is accurate ------------------- #
    research_system = (
        f"{engine.system_prompt}\n\n---\n\n"
        f"You are helping @earl_knows_ball decide how to REACT to a tweet from @{author}. "
        f"The account is a sharp, witty sports-culture / betting-takes voice (hints + analysis, "
        f"NEVER free picks, always <= 280 characters).\n\n"
        f"CRITICAL RESEARCH-ONLY MODE: use the tools to learn this tweet's SUBJECT - the teams/"
        f"players/games/league it touches, current standings, trends, injuries or matchups - so any "
        f"reply is accurate and not embarrassing. Gather what you need, then STOP calling tools and "
        f"reply with a short bulleted digest of the key facts. Do NOT write the actual reply yet."
    )
    research_user = (
        f"Tweet to react to (author @{author}):\n\n\"{post_text}\"\n\n"
        + (f"Posted: {created}\n\n" if created else "\n")
        + "Research what this is about and the smartest on-brand angle for a comeback/agreement."
    )
    brief = ""
    try:
        full_msgs, _ = await _run_research_loop(
            engine, db,
            [
                {"role": "system", "content": research_system},
                {"role": "user", "content": research_user},
            ],
            max_turns=5, timeout=240.0,
        )
        trace = _capture_research(full_msgs)
        brief = _deterministic_research_brief(trace.get("tool_calls") or [])
    except Exception as exc:  # noqa: BLE001
        logger.exception("x_reply research failed for post %s", post.get("id"))
        raise HTTPException(status_code=500, detail=f"Reply research failed: {exc}")

    # ---- Phase B: draft reply options (light gen, thinking off) ----------------- #
    brief_block = f"\nResearch brief (grounded - use it):\n{brief}\n" if brief else ""
    draft_system = (
        f"You are drafting X REPLY SUGGESTIONS for @earl_knows_ball replying to @{author}'s tweet "
        f"below. Voice: sharp, sports-culture betting-takes, witty, a touch crusty, never mean, "
        f"genuinely smart about the analysis/angle. Each reply <= 280 chars, feels human (not a bot), "
        f"adds value or a fun angle, and NEVER gives a free pick (may hint at a lean or tease analysis).\n"
        f"Return ONLY valid JSON:\n"
        f'{{"replies":[{{"body":"<reply text>","rationale":"<1 line why this works>"}}]}}\n'
        f"Provide {n} replies. Vary the tone across them (one witty/comedic, one sharp-analysis, "
        f"one agreement-and-add)."
    )
    draft_user = (
        f"@{author} tweeted:\n\n\"{post_text}\"\n"
        + brief_block
        + f"\nDraft {n} reply options as JSON."
    )
    raw = ""
    try:
        raw = await _chat(
            [
                {"role": "system", "content": draft_system},
                {"role": "user", "content": draft_user},
            ],
            max_tokens=1000, json_mode=True,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("x_reply generation failed for post %s", post.get("id"))
        raise HTTPException(status_code=502, detail=f"Reply generation failed: {exc}")

    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    parsed = {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                parsed = {}
    replies = parsed.get("replies") or []
    out = []
    for r in replies:
        body = (r.get("body") or "").strip()
        if body:
            body = re.sub(r"\s+", " ", body)
            out.append({"body": body[:280], "rationale": (r.get("rationale") or "")[:400]})
    if not out:
        logger.warning("x_reply empty replies for post %s: raw=%.300s", post.get("id"), raw)
        raise HTTPException(status_code=502, detail="Model returned no usable replies.")
    return out
