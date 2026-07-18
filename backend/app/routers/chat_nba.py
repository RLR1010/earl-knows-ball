"""AI chat endpoint — Earl answers NBA questions using tool-calling + DeepSeek.

Uses the same ToolChatEngine pattern as MLB (/chat/mlb) and NFL (/chat),
with NBA-specific tools for querying the nba schema.
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
from app.chat_tools import ToolChatEngine, NBA_TOOL_DEFINITIONS, execute_nba_tool

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
- Player props when relevant (points, rebounds, assists, 3PM)

Always use the tools to research before answering. Get specific stats and numbers."""

# ---------------------------------------------------------------------------
# Chat engine singleton
# ---------------------------------------------------------------------------

nba_chat_engine = ToolChatEngine(
    sport="nba",
    sport_display="NBA",
    data_description=(
        "team stats, player season stats, player game logs, standings, "
        "today's games, game details, injury reports, "
        "head-to-head results, model predictions, "
        "DFS salaries, team schedules, and news articles"
    ),
    tools=NBA_TOOL_DEFINITIONS,
    executor=execute_nba_tool,
    system_prompt_extra=NBA_SYSTEM_EXTRA,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatNBARequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None
    include_enrichment: bool = True


class ChatNBAResponse(BaseModel):
    response: str
    conversation_id: str
    tokens_used: int = 0


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/chat/nba", response_model=ChatNBAResponse)
async def chat_nba(
    request: ChatNBARequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """NBA chat endpoint — tool-calling research + DeepSeek answer."""
    try:
        # --- Step 1: Build message history ---
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
            "NBA chat: user=%s conv=%s msg=%s",
            current_user.id, conv_id, request.message[:80],
        )

        answer = await nba_chat_engine.research_and_answer(db, messages, max_turns=6)

        # --- Step 3: Enrichment (if enabled) ---
        if request.include_enrichment:
            enrichment_text = await ToolChatEngine.run_enrichment(
                db=db,
                question=request.message,
                sport="nba",
                top_k=8,
            )
            if enrichment_text and "No relevant information" not in enrichment_text:
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

        # --- Step 4: Save conversation history ---
        # Rollback first to clear any aborted transaction state from tool-calling errors
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

        return ChatNBAResponse(
            response=answer,
            conversation_id=conv_id,
            tokens_used=0,
        )

    except Exception as e:
        logger.exception("NBA chat error: %s", e)
        raise HTTPException(status_code=500, detail="An error occurred processing your request.")


__all__ = ["router"]
