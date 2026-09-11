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


async def verify_claims_with_reasons(candidates: list[str], grounding: str) -> list[dict]:
    """Grounding check pass. For each candidate post text, ask the model whether EVERY concrete
    factual claim (team/player/score/record/odds/date/stat) is SUPPORTED by the grounding brief.
    Returns a list of {"supported": bool, "reason": str} aligned to `candidates`.

    Fail-open: any error / unparseable output conservatively marks candidates as supported so we
    never silently drop good options when the verifier itself is unavailable.
    """
    n = len(candidates)
    if n == 0:
        return []
    # No grounding = nothing to verify against; can't flag unsupported claims.
    if not (grounding or "").strip():
        return [{"supported": True, "reason": "no grounding"} for _ in candidates]
    numbered = "\n".join(f'{i + 1}. {"" if c else "(empty)"}{c}' for i, c in enumerate(candidates))
    system = (
        "You are a strict fact-checker for @earl_knows_ball social posts. You are given a GROUNDING "
        "brief (authoritative researched facts, INCLUDING relevant article excerpts) and a numbered "
        "list of proposed post texts. For EACH post, decide if EVERY concrete, verifiable claim it "
        "makes (team, player, score, record, odds/line, date, ranking, season stat, matchup) is "
        "SUPPORTED by the grounding brief.\n"
        "RULES:\n"
        "- A claim is unsupported if it asserts a specific fact that is absent from, or contradicts, "
        "the grounding brief.\n"
        "- Vague/hype language, opinions, and clearly rhetorical statements are NOT claims; do not "
        "flag them.\n"
        "- Be strict with numbers, names, and dates: an invented figure or wrong team is unsupported.\n"
        "- If a post makes no concrete claims, it is supported.\n"
        "- For unsupported posts, `reason` must NAME the exact unsupported claim (e.g. 'claims a 7-2 "
        "score not present in the brief').\n"
        'Return ONLY JSON: {"results":[{"i":<number>,"supported":true|false,"reason":"<short>"}]}'
    )
    user = (
        f"<grounding brief>\n{grounding}\n</grounding brief>\n\n"
        f"<proposed posts>\n{numbered}\n</proposed posts>\n\n"
        'Return the JSON verdict for all posts.'
    )
    try:
        raw = await _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=900, json_mode=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("verify_claims call failed; failing open")
        return [{"supported": True, "reason": "verifier error"} for _ in candidates]
    raw = (raw or "").strip()
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
    results = parsed.get("results") or []
    verdicts = [{"supported": True, "reason": ""} for _ in candidates]
    seen = False
    for r in results:
        try:
            idx = int(r.get("i")) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n:
            seen = True
            verdicts[idx] = {
                "supported": bool(r.get("supported", True)),
                "reason": str(r.get("reason", "") or ""),
            }
    if not seen:
        logger.warning("verify_claims returned no usable verdicts; failing open")
        return [{"supported": True, "reason": "no verdicts"} for _ in candidates]
    return verdicts


async def verify_claims(candidates: list[str], grounding: str) -> list[bool]:
    """Boolean convenience wrapper over verify_claims_with_reasons (True = supported)."""
    return [v["supported"] for v in await verify_claims_with_reasons(candidates, grounding)]


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
    # Pick the right per-sport research engine from the post itself; fall back to the cross-sport
    # engine if detection is unavailable or the topic is ambiguous.
    engine = ENGINES.get("all")
    post_text = (post.get("text") or "").strip()
    try:
        from app.routers.social_x import _detect_sport
        _detected = await _detect_sport(post_text)
        if _detected in ENGINES:
            engine = ENGINES[_detected]
            logger.info("x_reply using engine=%s for post %s", _detected, post.get("id"))
    except Exception:  # noqa: BLE001
        logger.exception("x_reply sport detection failed; using cross-sport engine")
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
            max_turns=7, timeout=240.0,
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

    # ---- Phase C: grounding verification + ONE regeneration backfill ------------- #
    try:
        verdicts = await verify_claims_with_reasons([o["body"] for o in out], brief)
        kept = [o for o, v in zip(out, verdicts) if v["supported"]]
        failed = [o for o, v in zip(out, verdicts) if not v["supported"]]
        dropped = len(failed)
        if dropped:
            logger.info("x_reply verification dropped %d/%d unsupported reply option(s) for post %s",
                        dropped, len(out), post.get("id"))
            # ---- ONE regeneration attempt to backfill the failures --------------------- #
            reasons = "; ".join(
                (v.get("reason") or "unsupported claim")
                for o, v in zip(out, verdicts) if not v["supported"]
            )
            regen = await _regen_replies(
                post, author, post_text, brief, n=len(failed), avoid=[o["body"] for o in out],
                reasons=reasons,
            )
            if regen:
                rv = await verify_claims_with_reasons([r["body"] for r in regen], brief)
                kept += [r for r, v in zip(regen, rv) if v["supported"]]
                logger.info("x_reply regeneration backfilled %d reply option(s) for post %s",
                            len(kept) - (len(out) - dropped), post.get("id"))
        if kept:
            out = kept[:n]
        else:
            # Nothing survived even after regeneration: return originals flagged so the UI can warn.
            for o in out:
                o["unverified"] = True
    except Exception:  # noqa: BLE001
        logger.exception("x_reply verification pass failed for post %s; keeping unverified options",
                         post.get("id"))
    return out


async def _regen_replies(post: dict, author: str, post_text: str, brief: str, n: int,
                         avoid: list[str], reasons: str) -> list[dict]:
    """One-shot regeneration of `n` reply options that AVOID the flagged unsupported claims.
    Returns [] on any failure (caller simply keeps fewer options)."""
    if n <= 0:
        return []
    avoid_block = "\n".join(f"- {a}" for a in avoid[:5])
    system = (
        f"You are drafting X REPLY SUGGESTIONS for @earl_knows_ball replying to @{author}'s tweet. "
        f"Voice: sharp, sports-culture betting-takes, witty, a touch crusty, never mean. Each reply "
        f"<= 280 chars, feels human, and NEVER gives a free pick.\n"
        f"CRITICAL: the previous drafts failed fact-checking. Do NOT repeat those claims, and do NOT "
        f"state any specific fact (score, record, stat, date, injury, odds, matchup result) unless it "
        f"is SUPPORTED by the research brief below. If unsure of a number, write the take WITHOUT the "
        f"number instead of guessing.\n"
        f"Return ONLY valid JSON:\n"
        f'{{"replies":[{{"body":"<reply text>","rationale":"<1 line why this works>"}}]}}'
    )
    user = (
        f"@{author} tweeted:\n\n\"{post_text}\"\n"
        + (f"\nResearch brief (grounded - use it):\n{brief}\n" if brief else "")
        + f"\nThese earlier drafts FAILED fact-check: {reasons}\n"
        + f"Avoid claims like:\n{avoid_block}\n"
        + f"\nDraft {n} NEW reply option(s) as JSON."
    )
    try:
        raw = await _chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=1000, json_mode=True,
        )
    except Exception:  # noqa: BLE001
        logger.exception("x_reply regeneration failed for post %s", post.get("id"))
        return []
    raw = (raw or "").strip()
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
    out = []
    for r in (parsed.get("replies") or []):
        body = (r.get("body") or "").strip()
        if body:
            body = re.sub(r"\s+", " ", body)
            out.append({"body": body[:280], "rationale": (r.get("rationale") or "")[:400]})
    return out
