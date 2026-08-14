"""Customer Service chat endpoint.

A strict, support-only AI assistant grounded in the Earl Knows Ball knowledge base
(FAQ + Terms & Conditions + Privacy Statement). Auth required.

- Saves every user + assistant message to cs_messages (permanent support transcripts).
- Enforces a per-user, per-calendar-month token budget (default 200,000),
  adjustable via env EARL_CS_MONTHLY_TOKEN_LIMIT.
- Model answers ONLY from the active knowledge base, never from world knowledge,
  and escalates to a human when it cannot answer.
"""

import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.models.customer_service import CSMessage, CSKnowledge

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cs", tags=["customer-service"])

# Per-user, per-calendar-month CS token budget (tunable during testing).
MONTHLY_TOKEN_LIMIT = int(os.environ.get("EARL_CS_MONTHLY_TOKEN_LIMIT", "200000"))
# How many most-relevant knowledge entries to include in the system context.
MAX_KB_CHUNKS = 10
MODEL = getattr(settings, "deepseek_model", "deepseek-chat")

SYSTEM_PROMPT = """You are the customer service assistant for Earl Knows Ball, an AI-powered sports handicapping service covering the NFL, MLB, and NBA. Your job is to help users with account, billing, subscription, product, and support questions — not to provide betting picks or gambling advice.

STRICT RULES — follow these without exception:
1. Answer ONLY from the "KNOWLEDGE BASE" provided below. Never use outside knowledge or guess.
2. If the user's question is not covered by the knowledge base, do NOT invent an answer. Instead, tell them you're not certain and offer to escalate to a human support agent.
3. Never make promises about refunds, cancellation timelines, or outcomes unless the knowledge base explicitly states it.
4. Keep answers concise, clear, and professional. Use short paragraphs or bullets when helpful.
5. Never give betting predictions, picks, probabilities, or gambling advice. If asked, politely redirect to the product FAQ or say Earl's picks are for informational purposes only and that wagering involves risk.
6. Do not claim to be a human. You are an automated assistant, and you may say an administrator will follow up if the issue needs a person.
7. Respond only in the user's language. If asked about anything outside your role (general trivia, coding, etc.), politely decline and steer back to support.

KNOWLEDGE BASE (use this as the sole source of truth):
{knowledge}
"""


class CSMessageIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class ChatReply(BaseModel):
    reply: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    monthly_tokens: int
    monthly_limit: int
    escalated: bool = False


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _monthly_usage(db: AsyncSession, user_id: str) -> int:
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(func.coalesce(func.sum(CSMessage.tokens_used), 0)).where(
            CSMessage.user_id == str(user_id),
            CSMessage.role.isnot(None),
            CSMessage.created_at >= _month_start(now),
        )
    )
    return int(result.scalar_one())


def _score(entry_title: str, entry_content: str, query: str) -> float:
    """Simple relevance scoring: token overlap, weighted toward exact phrase hits."""
    q = query.lower()
    title_l = entry_title.lower()
    content_l = entry_content.lower()
    score = 0.0
    # Exact phrase in query
    for word in q.split()[:8]:
        if len(word) > 3:
            if word in title_l:
                score += 3.0
            elif word in content_l:
                score += 1.0
    # Whole query substring match is very strong signal
    if q in content_l or q in title_l:
        score += 5.0
    return score


async def _retrieve_knowledge(db: AsyncSession, query: str) -> list[CSKnowledge]:
    entries = (
        await db.execute(
            select(CSKnowledge).where(CSKnowledge.active.is_(True)).order_by(CSKnowledge.category, CSKnowledge.id)
        )
    ).scalars().all()
    scored = sorted(entries, key=lambda e: _score(e.title, e.content, query), reverse=True)
    return scored[:MAX_KB_CHUNKS]


def _format_knowledge(entries: list[CSKnowledge]) -> str:
    blocks = []
    for i, e in enumerate(entries, 1):
        blocks.append(f"[{i}] Category: {e.category}\nTitle: {e.title}\nContent: {e.content}")
    return "\n\n".join(blocks)


async def _get_or_create_llm() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
    )


@router.post("/chat", response_model=ChatReply)
async def cs_chat(
    body: CSMessageIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = str(user.id)
    now = datetime.now(timezone.utc)

    # ── Enforce per-month token budget ─────────────────────────────
    monthly = await _monthly_usage(db, uid)
    if monthly >= MONTHLY_TOKEN_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                "You've reached your monthly customer service message limit. "
                "Please email us directly or try again next month."
            ),
        )

    # ── Save the user's message first (durable) ────────────────────
    user_msg = CSMessage(
        user_id=uid, role="user", content=body.message, tokens_used=0, model=MODEL, created_at=now
    )
    db.add(user_msg)
    await db.commit()
    await db.refresh(user_msg)

    # ── Retrieve + build the grounded context ──────────────────────
    kb = await _retrieve_knowledge(db, body.message)
    system = SYSTEM_PROMPT.format(knowledge=_format_knowledge(kb))

    # ── History (last ~12 messages for continuity) ─────────────────
    history_rows = (
        await db.execute(
            select(CSMessage)
            .where(CSMessage.user_id == uid, CSMessage.id != user_msg.id)
            .order_by(CSMessage.created_at.desc(), CSMessage.id.desc())
            .limit(12)
        )
    ).scalars().all()
    history = [
        {"role": m.role, "content": m.content}
        for m in sorted(history_rows, key=lambda m: (m.created_at, m.id))
    ]

    messages = [{"role": "system", "content": system}] + history + [{"role": "user", "content": body.message}]

    # ── Call the model (no tools — strict grounding) ───────────────
    try:
        client = await _get_or_create_llm()
        response = await client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.2,
            max_tokens=500,
        )
        reply = response.choices[0].message.content or ""
        prompt_tokens = response.usage.prompt_tokens if response.usage and response.usage.prompt_tokens else 0
        completion_tokens = response.usage.completion_tokens if response.usage and response.usage.completion_tokens else 0
        tokens = prompt_tokens + completion_tokens
    except Exception:
        logger.exception("CS chat LLM call failed for user %s", uid)
        # Don't lose the user's message; fall back to a safe reply.
        reply = (
            "I'm sorry, I'm having trouble reaching my model right now. "
            "Please try again in a moment, or reach out and an administrator will follow up."
        )
        prompt_tokens = 0
        completion_tokens = 0
        tokens = 0

    new_monthly = monthly + tokens
    escalated = _detect_escalation(reply, body.message)

    # ── Save the assistant reply with real token count ─────────────
    assistant_msg = CSMessage(
        user_id=uid,
        role="assistant",
        content=reply,
        tokens_used=tokens,
        model=MODEL,
        created_at=datetime.now(timezone.utc),
    )
    db.add(assistant_msg)
    await db.commit()

    return ChatReply(
        reply=reply,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=tokens,
        monthly_tokens=new_monthly,
        monthly_limit=MONTHLY_TOKEN_LIMIT,
        escalated=escalated,
    )


def _detect_escalation(reply: str, question: str) -> bool:
    markers = ("human", "administrator", "agent", "escalate", "not certain", "reach out", "follow up")
    lower = reply.lower()
    return any(m in lower for m in markers)


@router.get("/history")
async def cs_history(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = str(user.id)
    rows = (
        await db.execute(
            select(CSMessage)
            .where(CSMessage.user_id == uid)
            .order_by(CSMessage.created_at.asc(), CSMessage.id.asc())
        )
    ).scalars().all()
    monthly = await _monthly_usage(db, uid)
    return {
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "tokens_used": m.tokens_used,
                "model": m.model,
                "created_at": m.created_at,
            }
            for m in rows
        ],
        "monthly_tokens": monthly,
        "monthly_limit": MONTHLY_TOKEN_LIMIT,
    }
