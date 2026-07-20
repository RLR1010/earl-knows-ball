"""AI chat endpoint — Earl answers NBA questions using tool-calling + DeepSeek.

Uses the same ToolChatEngine pattern as NFL (/chat) and MLB (/chat/mlb),
with NBA-specific tools for querying the nba schema.
"""

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.models.chat_history import ChatHistory
from app.chat_tools import ToolChatEngine, NBA_TOOL_DEFINITIONS, execute_nba_tool
from app.services.token_tracker import check_token_limit, save_token_usage

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# System prompt extras
# ---------------------------------------------------------------------------

NBA_SYSTEM_EXTRA = """You cover all 30 NBA teams. The current NBA season is in progress.

Key NBA handicapping angles:
- Star player availability is the single most important factor — who's playing and who's out changes everything
- Back-to-back games create rest disparity that affects scoring and pace
- Home court advantage is real but varies by team (altitude in Denver, crowd in Boston/OKC, etc.)
- Pace of play differential between opponents drives total scoring
- Three-point shooting variance is the biggest source of game-to-game volatility
- Defense matters: defensive rating, opponent FG%, and paint protection
- Foul trouble and free throw rate are key for player props and team totals
- Coaching matters: offensive/defensive schemes, rotation patterns, timeout management
- Division and conference games tend to be more competitive and lower scoring
- The NBA has more variance than other sports — single games are noisy, edges are smaller
- Rest advantage (extra day, no travel, home stand vs road trip) is an edge
- Closing line value (CLV) is the true measure of sharp vs public money

When discussing betting lines, always reference:
- Point spread with odds
- Moneyline with prices
- Game total (over/under)

Format responses with clean Markdown for readability: use **bold** for emphasis,
# or ## for section headers, | tables | for structured data, --- for section
breaks, lists for bullets, and use emojis as section markers.
NEVER use *** (triple asterisks). Use ** (double asterisks) instead.
Be direct and opinionated, but back it up with data."""

# ---------------------------------------------------------------------------
# Engine (singleton per worker)
# ---------------------------------------------------------------------------

nba_chat_engine = ToolChatEngine(
    sport="nba",
    sport_display="NBA",
    data_description=(
        "team stats, standings, injury reports, player stats, "
        "head-to-head results, and model predictions"
    ),
    tools=NBA_TOOL_DEFINITIONS,
    executor=execute_nba_tool,
    model=settings.deepseek_model,
    system_prompt_extra=NBA_SYSTEM_EXTRA,
)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatNBARequest(BaseModel):
    message: str = Field(..., description="The user's question about the NBA")
    conversation_id: str | None = Field(None, description="Conversation ID for follow-ups")
    include_enrichment: bool = Field(False, description="Whether to include article enrichment")


class ChatNBAResponse(BaseModel):
    response: str = Field(..., description="Earl's response")
    conversation_id: str = Field(..., description="Conversation ID for follow-ups")
    tokens_used: int = Field(0, description="Approximate token usage")


# ---------------------------------------------------------------------------
# Endpoint (SSE streaming)
# ---------------------------------------------------------------------------





@router.post("/chat/nba")
async def chat_nba(
    request: ChatNBARequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    NBA chat endpoint — SSE streaming.
    Yields status events ('status') during research, then the final answer ('answer').
    """

    # Check token limit for premium/ultimate users
    if current_user.subscription_tier in ("premium", "ultimate"):
        allowed, _ = await check_token_limit(current_user, db)
        if not allowed:
            async def limit_error_stream():
                yield {"data": json.dumps({
                    "type": "answer",
                    "content": "You've reached your monthly chat token limit. Your usage will reset at the start of next month. Upgrade your plan if you need more tokens.",
                }, ensure_ascii=False)}
                yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}
            return EventSourceResponse(
                limit_error_stream(),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Connection": "keep-alive"},
                ping=5,
            )
    async def event_stream():
        answer = ""
        total_tokens = 0
        try:
            # --- Build message history ---
            messages: list[dict] = []

            if request.conversation_id:
                result = await db.execute(
                    select(ChatHistory)
                    .where(
                        ChatHistory.conversation_id == request.conversation_id,
                        ChatHistory.user_id == current_user.id,
                        ChatHistory.sport == "nba",
                    )
                    .order_by(ChatHistory.created_at.asc())
                    .limit(20)
                )
                history = result.scalars().all()
                for h in history:
                    messages.append({"role": h.role, "content": h.message})
                conv_id = request.conversation_id
            else:
                conv_id = str(uuid4())

            # Add time-contextualized question
            central_now = datetime.now(ZoneInfo("America/Chicago"))
            time_context = central_now.strftime("%A, %B %d, %Y at %I:%M %p %Z").replace(" 0", " ")
            messages.append({
                "role": "user",
                "content": f"[Central US time: {time_context}]\n\n{request.message}",
            })

            user_id = current_user.id

            logger.info(
                "NBA chat: user=%s conv=%s msg=%s",
                current_user.id, conv_id, request.message[:80],
            )

            # Send conv_id so the frontend can track the conversation
            yield {"data": json.dumps({"type": "conv_id", "id": conv_id, "user_id": str(user_id)}, ensure_ascii=False)}

            # --- Research phase (streaming status) ---
            async for event_type, data in nba_chat_engine.research_and_answer_stream(
                db, messages, max_turns=6
            ):
                if event_type == "status":
                    yield {"data": json.dumps({"type": "status", "message": data}, ensure_ascii=False)}
                elif event_type == "usage":
                    total_tokens += data.get("total_tokens", 0)
                    continue
                elif event_type == "answer":
                    answer = data

            # --- Enrichment phase ---
            if request.include_enrichment:
                yield {"data": json.dumps({"type": "status", "message": "Searching for relevant articles..."}, ensure_ascii=False)}
                enrichment_text, enrichment_tokens = await ToolChatEngine.run_enrichment(
                    db=db,
                    question=request.message,
                    sport="nba",
                    top_k=8,
                )
                if enrichment_text and "No relevant information" not in enrichment_text:
                    yield {"data": json.dumps({"type": "status", "message": "Polishing with article insights..."}, ensure_ascii=False)}
                    enriched_messages = messages.copy()
                    enriched_messages.append({"role": "assistant", "content": answer})
                    enriched_messages.append({
                        "role": "system",
                        "content": (
                            f"Additional context from recent NBA articles: {enrichment_text}\n\n"
                            "Incorporate any relevant information from this into your answer "
                            "to provide the most up-to-date response."
                        ),
                    })
                    enriched_answer = await nba_chat_engine.research_and_answer(
                        db, enriched_messages, max_turns=2,
                    )
                    if enriched_answer and len(enriched_answer) > len(answer):
                        answer = enriched_answer

            # --- Save conversation history ---
            await db.rollback()
            now = datetime.now(timezone.utc)
            db.add(ChatHistory(
                conversation_id=conv_id,
                user_id=user_id,
                sport="nba",
                role="user",
                message=request.message,
                created_at=now,
            ))
            db.add(ChatHistory(
                conversation_id=conv_id,
                user_id=user_id,
                sport="nba",
                role="assistant",
                message=answer,
                created_at=now,
            ))
            await db.commit()

            # --- Send final answer ---
            yield {"data": json.dumps({"type": "answer", "content": answer}, ensure_ascii=False)}
            await save_token_usage(current_user, db, total_tokens)

            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}

        except Exception as e:
            logger.exception("NBA chat error: %s", e)
            if not answer:
                yield {
                    "data": json.dumps({
                        "type": "answer",
                        "content": "I was researching your question but hit a snag. Could you try rephrasing?",
                    }, ensure_ascii=False)
                }
            else:
                yield {"data": json.dumps({"type": "answer", "content": answer}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}

    return EventSourceResponse(
        event_stream(),
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
        },
        ping=5,
    )


__all__ = ["router"]
