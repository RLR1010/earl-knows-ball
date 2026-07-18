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
- Point spread with odds (standard -110 juice unless noted)
- Moneyline with prices
- Game total (over/under)

Always use the tools to research before answering. Get specific stats and numbers."""

# ---------------------------------------------------------------------------
# Chat engine singleton
# ---------------------------------------------------------------------------

nfl_chat_engine = ToolChatEngine(
    sport="nfl",
    sport_display="NFL",
    data_description=(
        "team stats, player stats, standings, "
        "today's games, weekly games, game details, injury reports, "
        "depth charts, head-to-head results, model predictions, "
        "DFS salaries, team schedules, and news articles"
    ),
    tools=NFL_TOOL_DEFINITIONS,
    executor=execute_nfl_tool,
    system_prompt_extra=NFL_SYSTEM_EXTRA,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatNFLRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None
    include_enrichment: bool = True


class ChatNFLResponse(BaseModel):
    response: str
    conversation_id: str
    tokens_used: int = 0


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/chat", response_model=ChatNFLResponse)
async def chat_nfl(
    request: ChatNFLRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """NFL chat endpoint — tool-calling research + DeepSeek answer."""
    try:
        # --- Step 1: Build message history ---
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

        # Add the current question with timezone context
        central_now = datetime.now(ZoneInfo("America/Chicago"))
        time_context = central_now.strftime("%A, %B %d, %Y at %I:%M %p %Z").replace(" 0", " ")
        user_msg = {
            "role": "user",
            "content": f"[Central US time: {time_context}]\n\n{request.message}",
        }
        messages.append(user_msg)

        # Capture user id early before any DB errors can corrupt the session
        user_id = current_user.id

        # --- Step 2: Research & Answer via tool calling ---
        logger.info(
            "NFL chat: user=%s conv=%s msg=%s",
            current_user.id, conv_id, request.message[:80],
        )

        answer = await nfl_chat_engine.research_and_answer(db, messages, max_turns=6)

        # --- Step 3: Enrichment (if enabled) ---
        if request.include_enrichment:
            enrichment_text = await ToolChatEngine.run_enrichment(
                db=db,
                question=request.message,
                sport="nfl",
                top_k=8,
            )
            if enrichment_text and "No relevant information" not in enrichment_text:
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

        # --- Step 4: Save conversation history ---
        # Rollback first to clear any aborted transaction state from tool-calling errors
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

        return ChatNFLResponse(
            response=answer,
            conversation_id=conv_id,
            tokens_used=0,
        )

    except Exception as e:
        logger.exception("NFL chat error: %s", e)
        raise HTTPException(status_code=500, detail="An error occurred processing your request.")


__all__ = ["router"]
