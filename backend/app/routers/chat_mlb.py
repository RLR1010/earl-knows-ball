"""MLB chat endpoint with tool-calling research and enrichment.

This replaces the old httpx-based chat with a DeepSeek function-calling approach:
1. User sends a question with conversation history.
2. DeepSeek researches by calling MLB database tools (team stats, standings, etc.).
3. After research, enrichment searches pgvector articles and gets a relevance summary.
4. If enrichment found useful info, DeepSeek generates the final answer with everything.

Uses AsyncOpenAI (OpenAI Python SDK) like the writeup system, not raw httpx.
"""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.config import settings
from app.core.security import get_current_user
from app.models import User
from app.models.chat_history import ChatHistory
from app.chat_tools.base import ToolChatEngine
from app.chat_tools.mlb import TOOL_DEFINITIONS, execute_mlb_tool

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# System prompt extras
# ---------------------------------------------------------------------------

MLB_SYSTEM_EXTRA = """You cover all 30 MLB teams. The current 2026 season is in progress.

Key MLB handicapping angles:
- Starting pitcher matchup is the single most important factor for any game
- Bullpen usage and fatigue matter, especially in series
- Park factors affect scoring (Coors Field, Yankee Stadium short porch, etc.)
- Weather (wind, temperature) affects the over/under significantly in outdoor parks
- Division rivalries tend to produce tighter, lower-scoring games
- Day games after night games create rest and travel considerations
- Home/road splits can be dramatic for some teams
- A team's record in 1-run games and extra-innings tells you about their bullpen and luck

When discussing betting lines, always reference:
- Run line (spread) with odds
- Moneyline with prices
- Game total (over/under)

Always use the tools to research before answering. Get specific stats and numbers."""

# ---------------------------------------------------------------------------
# Chat engine singleton
# ---------------------------------------------------------------------------

mlb_chat_engine = ToolChatEngine(
    sport="mlb",
    sport_display="MLB",
    data_description=(
        "team stats, batting stats, pitching stats, standings, "
        "today's games, game details, injury reports, player stats, "
        "head-to-head results, model predictions, team splits, and news articles"
    ),
    tools=TOOL_DEFINITIONS,
    executor=execute_mlb_tool,
    system_prompt_extra=MLB_SYSTEM_EXTRA,
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ChatMLBRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    conversation_id: str | None = None
    include_enrichment: bool = True


class ChatMLBResponse(BaseModel):
    response: str
    conversation_id: str
    tokens_used: int = 0


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/chat/mlb", response_model=ChatMLBResponse)
async def chat_mlb(
    request: ChatMLBRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Chat with Earl about MLB. Research is handled via tool calling so
    DeepSeek decides what data to look up before answering."""

    conv_id = request.conversation_id
    start_time = datetime.now(timezone.utc)

    try:
        # --- Step 1: Build messages with conversation history ---
        messages = [
            {"role": "system", "content": mlb_chat_engine.system_prompt},
        ]

        if conv_id:
            # Load last N messages for context
            stmt = (
                select(ChatHistory)
                .where(
                    ChatHistory.conversation_id == conv_id,
                    ChatHistory.user_id == current_user.id,
                )
                .order_by(ChatHistory.created_at.asc())
            )
            result = await db.execute(stmt)
            history = result.scalars().all()
            # Include only user/assistant messages (not tool calls)
            for h in history[-20:]:  # last 20 messages
                if h.role in ("user", "assistant"):
                    messages.append({"role": h.role, "content": h.message})
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

        # --- Step 2: Research & Answer via tool calling ---
        logger.info(
            "MLB chat: user=%s conv=%s msg=%s",
            current_user.id, conv_id, request.message[:80],
        )

        answer = await mlb_chat_engine.research_and_answer(db, messages, max_turns=6)

        # --- Step 3: Enrichment (if enabled) ---
        if request.include_enrichment:
            enrichment_text = await ToolChatEngine.run_enrichment(
                db=db,
                question=request.message,
                sport="mlb",
                top_k=8,
            )
            if enrichment_text and "No relevant information" not in enrichment_text:
                logger.info("Enrichment found relevant info — generating refined answer")
                try:
                    from openai import AsyncOpenAI
                    client = AsyncOpenAI(
                        api_key=settings.deepseek_api_key,
                        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
                        timeout=30.0,
                    )
                    final_response = await client.chat.completions.create(
                        model=settings.deepseek_model,
                        messages=[
                            {"role": "system", "content": mlb_chat_engine.system_prompt},
                            {"role": "user", "content": (
                                f"Original question: {request.message}\n\n"
                                f"--- KEY DATA FROM TOOL RESEARCH ---\n"
                                f"{answer}\n\n"
                                f"--- ADDITIONAL CONTEXT FROM RECENT ARTICLES ---\n"
                                f"{enrichment_text}\n\n"
                                f"Using the researched data AND the article enrichment above, "
                                f"provide your final handicapping answer. Be concise and specific."
                            )},
                        ],
                        temperature=0.3,
                        max_tokens=2048,
                    )
                    refined = final_response.choices[0].message.content or ""
                    if refined:
                        answer = refined
                        logger.info("Enrichment refinement completed successfully")
                    else:
                        logger.warning("Enrichment refinement returned empty")
                except Exception as enrich_err:
                    logger.exception("Enrichment final generation failed: %s", enrich_err)
                    # Fall back to the research-only answer

        # --- Step 4: Save conversation history ---
        now = datetime.now(timezone.utc)

        # Save the user message
        db.add(ChatHistory(
            conversation_id=conv_id,
            user_id=current_user.id,
            sport="mlb",
            role="user",
            message=request.message,
            created_at=now,
        ))
        # Save the assistant response
        db.add(ChatHistory(
            conversation_id=conv_id,
            user_id=current_user.id,
            sport="mlb",
            role="assistant",
            message=answer,
            created_at=now,
        ))
        await db.commit()

        # Rough token estimate
        total_tokens = len(request.message.split()) + len(answer.split())
        total_tokens *= 2  # rough multiplier for tool call tokens

        return ChatMLBResponse(
            response=answer,
            conversation_id=conv_id,
            tokens_used=total_tokens,
        )

    except Exception as e:
        logger.exception("MLB chat error for user %s: %s", current_user.id, e)
        raise HTTPException(status_code=500, detail="Internal error processing chat.")
