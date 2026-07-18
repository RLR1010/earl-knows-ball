"""MLB chat endpoint with tool-calling research and SSE streaming.

1. User sends a question with conversation history.
2. DeepSeek researches by calling MLB database tools (team stats, standings, etc.),
   with status updates streamed to the client.
3. After research, enrichment searches pgvector articles for additional context.
4. The final answer is streamed as an SSE event.

Uses the same ToolChatEngine pattern as NFL (/chat) and NBA (/chat/nba).
"""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse
from openai import AsyncOpenAI
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
# System prompt extra
# ---------------------------------------------------------------------------

MLB_SYSTEM_EXTRA = """You cover all 30 MLB teams. The current MLB season is in progress.

Key MLB handicapping angles:
- Pitching matchup is the single most important factor — starting pitcher quality and bullpen depth drive game outcomes
- Home/away splits matter more in baseball than any other sport (ballpark factors, travel, familiarity)
- Ballpark factors significantly affect totals — some parks are hitter-friendly (Coors, Great American, Camden), others are pitcher-friendly (Petco, T-Mobile, Citi Field)
- Weather (wind direction/speed, temperature, humidity, rain) dramatically affects MLB totals — wind blowing out increases scoring, cold dampens offense, rain creates slicker conditions for fielders
- Bullpen usage and fatigue matter — teams that overuse their bullpen in consecutive games are vulnerable
- Left/righty splits are crucial for specific batter-pitcher matchups
- Days off affect pitcher rest and bullpen availability
- Divisional opponents face each other 19 times — familiarity helps hitters against known pitchers
- Day games after night games affect offense (hitters see the ball worse in afternoon sun)
- The run line (+1.5 for dogs, -1.5 for favorites) is often more valuable than moneyline for heavy favorites
- First five innings (F5) bets reduce bullpen volatility
- Streaks and hot/cold periods in baseball are real but overvalued by the market
- Umpire strike zone tendencies affect pitcher performance
- Temperature affects ball travel distance and pitcher grip (colder = lower scoring)

Follow the handicapper info mandate: every answer must cover both sides of the matchup
and include situational factors (rest, venue, weather, bullpen)."""

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
# Endpoint (SSE streaming)
# ---------------------------------------------------------------------------


@router.post("/chat/mlb")
async def chat_mlb(
    request: ChatMLBRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Chat with Earl about MLB — SSE streaming with status updates."""

    async def event_stream():
        answer = ""
        try:
            # --- Step 1: Build messages with conversation history ---
            messages = [
                {"role": "system", "content": mlb_chat_engine.system_prompt},
            ]

            if request.conversation_id:
                stmt = (
                    select(ChatHistory)
                    .where(
                        ChatHistory.conversation_id == request.conversation_id,
                        ChatHistory.user_id == current_user.id,
                    )
                    .order_by(ChatHistory.created_at.asc())
                )
                result = await db.execute(stmt)
                history = result.scalars().all()
                for h in history[-20:]:
                    if h.role in ("user", "assistant"):
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
                "MLB chat: user=%s conv=%s msg=%s",
                current_user.id, conv_id, request.message[:80],
            )

            # Send conv_id so the frontend can track the conversation
            yield {"data": json.dumps({"type": "conv_id", "id": conv_id, "user_id": str(user_id)}, ensure_ascii=False)}

            # --- Step 2: Research phase (streaming status) ---
            async for event_type, data in mlb_chat_engine.research_and_answer_stream(
                db, messages, max_turns=6
            ):
                if event_type == "status":
                    yield {"data": json.dumps({"type": "status", "message": data}, ensure_ascii=False)}
                elif event_type == "answer":
                    answer = data

            # --- Step 3: Enrichment (if enabled) ---
            if request.include_enrichment:
                yield {"data": json.dumps({"type": "status", "message": "Searching for relevant articles..."}, ensure_ascii=False)}
                enrichment_text = await ToolChatEngine.run_enrichment(
                    db=db,
                    question=request.message,
                    sport="mlb",
                    top_k=8,
                )
                if enrichment_text and "No relevant information" not in enrichment_text:
                    yield {"data": json.dumps({"type": "status", "message": "Polishing with article insights..."}, ensure_ascii=False)}
                    try:
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

            # --- Step 4: Save conversation history ---
            now = datetime.now(timezone.utc)
            db.add(ChatHistory(
                conversation_id=conv_id,
                user_id=user_id,
                sport="mlb",
                role="user",
                message=request.message,
                created_at=now,
            ))
            db.add(ChatHistory(
                conversation_id=conv_id,
                user_id=user_id,
                sport="mlb",
                role="assistant",
                message=answer,
                created_at=now,
            ))
            await db.commit()

            # --- Send final answer ---
            yield {"data": json.dumps({"type": "answer", "content": answer}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}

        except Exception as e:
            logger.exception("MLB chat error for user %s: %s", current_user.id, e)
            if not answer:
                yield {
                    "data": json.dumps({
                        "type": "answer",
                        "content": "I was researching your question but hit a snag. Could you try rephrasing?",
                    }, ensure_ascii=False),
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
