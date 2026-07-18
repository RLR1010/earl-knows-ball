"""AI chat endpoint — Earl answers NFL questions using tool-calling + DeepSeek.

Uses the same ToolChatEngine pattern as the MLB chat (/chat/mlb), with NFL-specific
tools for querying the nfl schema.
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
from app.chat_tools import ToolChatEngine, NFL_TOOL_DEFINITIONS, execute_nfl_tool

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# System prompt extras
# ---------------------------------------------------------------------------

NFL_SYSTEM_EXTRA = """You cover all 32 NFL teams. The current NFL season is in progress.

Key NFL handicapping angles:
- Quarterback play is the single most important factor — who's under center matters more than anything
- Offensive line health directly impacts QB performance and run game efficiency
- Defensive matchups matter: pass rush vs offensive line, secondary vs WR corps
- Turnover differential often predicts game outcomes more reliably than yardage
- Weather (wind, cold, precipitation) significantly affects passing games and scoring — especially outdoors
- Dome vs outdoor splits are real — some teams play dramatically different on the road in cold weather
- Divisional games tend to be tighter and more unpredictable
- Rest advantage (extra days off, Thursday night games, bye weeks) is a real edge
- Home field advantage varies dramatically by stadium and crowd noise
- Short week (Thursday) games favor defenses and running games
- Look-ahead and letdown spots matter — teams looking past weak opponents often struggle
- A team's record in close games (one-score games) reveals something about their coaching and luck
- Primetime games can amplify home/road advantages

When discussing betting lines, always reference:
- Current market lines vs opening lines (show line movement)
- Whether a line has moved toward or away from the public betting percentage
- Key numbers (3, 7, 10) and whether the spread crosses them
- Historical cover rates for similar lines and situations"""

# ---------------------------------------------------------------------------
# Engine (singleton per worker)
# ---------------------------------------------------------------------------

nfl_chat_engine = ToolChatEngine(
    sport="nfl",
    sport_display="NFL",
    data_description=(
        "team stats, standings, injury reports, depth charts, "
        "head-to-head results, player stats, and model predictions"
    ),
    tools=NFL_TOOL_DEFINITIONS,
    executor=execute_nfl_tool,
    model=settings.deepseek_model,
    system_prompt_extra=NFL_SYSTEM_EXTRA,
)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatNFLRequest(BaseModel):
    message: str = Field(..., description="The user's question about the NFL")
    conversation_id: str | None = Field(None, description="Conversation ID for follow-ups")
    include_enrichment: bool = Field(False, description="Whether to include article enrichment")


class ChatNFLResponse(BaseModel):
    response: str = Field(..., description="Earl's response")
    conversation_id: str = Field(..., description="Conversation ID for follow-ups")
    tokens_used: int = Field(0, description="Approximate token usage")


# ---------------------------------------------------------------------------
# Endpoint (SSE streaming)
# ---------------------------------------------------------------------------





@router.post("/chat")
async def chat_nfl(
    request: ChatNFLRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    NFL chat endpoint — SSE streaming.
    Yields status events ('status') during research, then the final answer ('answer').
    """
    async def event_stream():
        answer = ""
        try:
            # --- Build message history ---
            messages: list[dict] = []

            if request.conversation_id:
                result = await db.execute(
                    select(ChatHistory)
                    .where(
                        ChatHistory.conversation_id == request.conversation_id,
                        ChatHistory.user_id == current_user.id,
                        ChatHistory.sport == "nfl",
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
                "NFL chat: user=%s conv=%s msg=%s",
                current_user.id, conv_id, request.message[:80],
            )

            # Send conv_id so the frontend can track the conversation
            yield {"data": json.dumps({"type": "conv_id", "id": conv_id, "user_id": str(user_id)}, ensure_ascii=False)}

            # --- Research phase (streaming status) ---
            async for event_type, data in nfl_chat_engine.research_and_answer_stream(
                db, messages, max_turns=6
            ):
                if event_type == "status":
                    yield {"data": json.dumps({"type": "status", "message": data}, ensure_ascii=False)}
                elif event_type == "answer":
                    answer = data

            # --- Enrichment phase ---
            if request.include_enrichment:
                yield {"data": json.dumps({"type": "status", "message": "Searching for relevant articles..."}, ensure_ascii=False)}
                enrichment_text = await ToolChatEngine.run_enrichment(
                    db=db,
                    question=request.message,
                    sport="nfl",
                    top_k=8,
                )
                if enrichment_text and "No relevant information" not in enrichment_text:
                    yield {"data": json.dumps({"type": "status", "message": "Polishing with article insights..."}, ensure_ascii=False)}
                    enriched_messages = messages.copy()
                    enriched_messages.append({"role": "assistant", "content": answer})
                    enriched_messages.append({
                        "role": "system",
                        "content": (
                            f"Additional context from recent NFL articles: {enrichment_text}\n\n"
                            "Incorporate any relevant information from this into your answer "
                            "to provide the most up-to-date response."
                        ),
                    })
                    enriched_answer = await nfl_chat_engine.research_and_answer(
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
                sport="nfl",
                role="user",
                message=request.message,
                created_at=now,
            ))
            db.add(ChatHistory(
                conversation_id=conv_id,
                user_id=user_id,
                sport="nfl",
                role="assistant",
                message=answer,
                created_at=now,
            ))
            await db.commit()

            # --- Send final answer ---
            yield {"data": json.dumps({"type": "answer", "content": answer}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}

        except Exception as e:
            logger.exception("NFL chat error: %s", e)
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
