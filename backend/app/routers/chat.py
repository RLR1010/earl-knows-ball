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
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models import User
from app.models.chat_history import ChatHistory
from app.chat_tools import ToolChatEngine, NFL_TOOL_DEFINITIONS, execute_nfl_tool
from app.services.token_tracker import check_token_limit, save_token_usage
from app.services.app_settings import get_free_chat_config
from app.chat_tools.free_mode import FREE_SYSTEM_PROMPT_TEMPLATE, filter_tools
from app.chat_status import get_chat_status, clear_chat_status, set_chat_status

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# System prompt extras
# ---------------------------------------------------------------------------

NFL_SYSTEM_EXTRA = """You cover all 32 NFL teams. The current NFL season is in progress.

SEASONS & TIME: The user message begins with the current Central US date/time AND an explicit
season line ("Current NFL season: <Y> ... Most recently completed season: <Y-1>"). Always
refer to seasons by their exact year (e.g. "the 2024 season"). NEVER use relative phrases like
"last season" or "this season" — name the exact year instead, using the season line above to
disambiguate. Do not assume an older season is "last season" just because it is the one being
discussed.

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

You have canonical NFL situational tools (data 1999-present):
- get_team_situational_splits: a team's W-L, points, EPA/play, success rate, ATS and over/under in
  a situation (home/away, division, dome/outdoor, grass/turf, cold/mild/warm, windy/calm,
  rest_short/normal/long, primetime, favorite/underdog), per season or career.
- get_player_situational_splits: a player's passing/rushing/receiving/defence + EPA/CPOE +
  fantasy points in the SAME splits, per season or career.
- get_defense_vs_position: production a defense ALLOWED to each position group (QB/RB/WR/TE),
  with a per-season generosity rank (rank 1 = most generous).
- get_situational_leaders: league leaders WITHIN a split (team level by EPA/ATS/etc., or player
  level by yards/TD/fantasy) — e.g. "best cold-weather teams", "most receiving yards in primetime".

USE THESE for questions like "is Mahomes worse in cold weather?", "how does Jefferson do at home
vs on the road?", "which defense gives up the most to RBs?", "who covers best as an underdog?",
or "does he produce less against the division?" — quote the actual per-split numbers rather than
speculating. Weather splits are temperature/wind based (we do NOT track precipitation).

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

# Free-tier engine: same brain, but pick/betting tools are stripped and a hard
# "no picks / no betting advice" addendum is drilled in (see chat_tools/free_mode).
nfl_free_chat_engine = ToolChatEngine(
    sport="nfl",
    sport_display="NFL",
    data_description=(
        "team stats, standings, injury reports, depth charts, "
        "head-to-head results, and player stats"
    ),
    tools=filter_tools("nfl", NFL_TOOL_DEFINITIONS),
    executor=execute_nfl_tool,
    model=settings.deepseek_model,
    system_prompt_template=FREE_SYSTEM_PROMPT_TEMPLATE,
)

# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class ChatNFLRequest(BaseModel):
    message: str = Field(..., description="The user's question about the NFL")
    conversation_id: str | None = Field(None, description="Conversation ID for follow-ups")
    include_enrichment: bool = Field(False, description="Whether to include article enrichment")
    request_id: str | None = Field(None, description="Client-generated ID used to poll live status via GET /chat/status/{request_id}")
    system_context: str | None = Field(None, description="Optional game context (matchup, lines, Earl picks) injected into the LLM prompt without storing it in chat history.")


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

    # --- Access control: tier-aware (free vs premium) ---
    is_free = current_user.subscription_tier not in ("premium", "premium_yearly")
    free_cfg = await get_free_chat_config(db) if is_free else {"enabled": True, "monthly_tokens": 0}
    _sse_headers = {"Cache-Control": "no-cache, no-store, must-revalidate", "Connection": "keep-alive"}

    if is_free and not free_cfg["enabled"]:
        async def free_disabled_stream():
            yield {"data": json.dumps({
                "type": "answer",
                "content": "Free chat is currently unavailable. Premium members get Earl's full chat — including picks, predictions, and game writeups.",
            }, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}
        return EventSourceResponse(free_disabled_stream(), headers=_sse_headers, ping=5)

    # Free users are capped by the admin-configured global grant; premium users by
    # their plan (or purchased extra tokens).
    default_limit = free_cfg["monthly_tokens"] if is_free else None
    allowed, _ = await check_token_limit(current_user, db, default_limit=default_limit)
    if not allowed:
        limit_msg = (
            f"You've used all {default_limit:,} free chat tokens for this month. "
            "Upgrade to premium for a much larger monthly allowance, or buy extra tokens "
            "from your profile page (earlknowsball.com/profile)."
            if is_free else
            "You've reached your chat token limit for this billing period — your monthly "
            "allotment is used up and you have no extra tokens left. You can buy more tokens "
            "from your profile page (earlknowsball.com/profile), which roll over to future "
            "months if unused."
        )

        async def limit_error_stream():
            yield {"data": json.dumps({"type": "answer", "content": limit_msg}, ensure_ascii=False)}
            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}

        return EventSourceResponse(limit_error_stream(), headers=_sse_headers, ping=5)

    chat_engine = nfl_free_chat_engine if is_free else nfl_chat_engine
    async def event_stream():
        answer = ""
        total_tokens = 0
        try:
            # --- Build message history ---
            # The engine's system prompt MUST be the first message; without it the
            # model gets no persona/rules at all (and no free-tier restriction).
            messages: list[dict] = [
                {"role": "system", "content": chat_engine.system_prompt},
            ]

            if request.conversation_id:
                result = await db.execute(
                    select(ChatHistory)
                    .where(
                        ChatHistory.conversation_id == request.conversation_id,
                        ChatHistory.user_id == current_user.id,
                    )
                    .order_by(ChatHistory.created_at.asc())
                    .limit(20)
                )
                history = result.scalars().all()
                for h in history:
                    messages.append({"role": h.role, "content": h.message})
                # Cross-sport continuity (prototype B): if this thread was started in another
                # sport, adopt it here so continuing it stays one clean conversation/list.
                if any(h.sport != "nfl" for h in history):
                    await db.execute(
                        update(ChatHistory)
                        .where(
                            ChatHistory.conversation_id == request.conversation_id,
                            ChatHistory.user_id == current_user.id,
                        )
                        .values(sport="nfl")
                    )
                    await db.commit()
                conv_id = request.conversation_id
            else:
                conv_id = str(uuid4())

            # Add time-contextualized question
            central_now = datetime.now(ZoneInfo("America/Chicago"))
            time_context = central_now.strftime("%A, %B %d, %Y at %I:%M %p %Z").replace(" 0", " ")
            # NFL season year: a season labelled Y runs Sep(Y)-Feb(Y+1); the league year
            # rolls over in March. So Jan/Feb still belong to the previous year's season.
            season_year = central_now.year if central_now.month >= 3 else central_now.year - 1
            messages.append({
                "role": "user",
                "content": (
                    f"[Central US time: {time_context} | Current NFL season: {season_year} "
                    f"(in progress) · Most recently completed season: {season_year - 1}]\n\n"
                    f"{request.message}"
                ),
            })

            # Optional game context (from a game-card chat): inject as a system
            # instruction so Earl knows the matchup/lines/picks — the stored user
            # message stays clean (no [GAME CONTEXT] prefix) in chat history.
            # Skipped for free users because it can carry lines/picks.
            if getattr(request, "system_context", None) and not is_free:
                messages.append({
                    "role": "system",
                    "content": request.system_context,
                })

            user_id = current_user.id

            logger.info(
                "NFL chat: user=%s conv=%s msg=%s",
                current_user.id, conv_id, request.message[:80],
            )

            # Send conv_id so the frontend can track the conversation
            yield {"data": json.dumps({"type": "conv_id", "id": conv_id, "user_id": str(user_id)}, ensure_ascii=False)}

            # --- Research phase (streaming status) ---
            async for event_type, data in chat_engine.research_and_answer_stream(
                db, messages, max_turns=6
            ):
                if event_type == "status":
                    if request.request_id:
                        await set_chat_status(request.request_id, data)
                    yield {"data": json.dumps({"type": "status", "message": data}, ensure_ascii=False)}
                elif event_type == "usage":
                    total_tokens += data.get("total_tokens", 0)
                    continue
                elif event_type == "answer":
                    answer = data

            enrichment_tokens = 0

            # --- Enrichment phase ---
            if request.include_enrichment:
                if request.request_id:
                    await set_chat_status(request.request_id, "Searching for relevant articles...")
                yield {"data": json.dumps({"type": "status", "message": "Searching for relevant articles..."}, ensure_ascii=False)}
                enrichment_text, enrichment_tokens = await ToolChatEngine.run_enrichment(
                    db=db,
                    question=request.message,
                    sport="nfl",
                    top_k=8,
                )
                if enrichment_text and "No relevant information" not in enrichment_text:
                    if request.request_id:
                        await set_chat_status(request.request_id, "Polishing with article insights...")
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
                    enriched_answer, enriched_tokens = await chat_engine.research_and_answer(
                        db, enriched_messages, max_turns=2,
                    )
                    total_tokens += enriched_tokens
                    if enriched_answer and len(enriched_answer) > len(answer):
                        answer = enriched_answer

            total_tokens += enrichment_tokens

            # --- Save conversation history ---
            await db.rollback()
            # Rollback expired all ORM objects, incl. current_user. Reload it so
            # save_token_usage() below doesn't trigger a sync lazy-load (MissingGreenlet).
            await db.refresh(current_user)
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
                tokens_used=total_tokens,
                created_at=now,
            ))
            await db.commit()

            # --- Send final answer ---
            yield {"data": json.dumps({"type": "answer", "content": answer}, ensure_ascii=False)}
            await save_token_usage(current_user, db, total_tokens, default_limit=default_limit)

            yield {"data": json.dumps({"type": "done"}, ensure_ascii=False)}
            if request.request_id:
                await clear_chat_status(request.request_id)

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



@router.get("/chat/free-access")
async def free_access(db: AsyncSession = Depends(get_db)):
    """Public: is the free chat tier open, and its monthly token grant?

    The client uses this to choose between the login gate, the free-chat notice,
    or the upgrade wall. No auth required."""
    cfg = await get_free_chat_config(db)
    return {"free_chat_enabled": cfg["enabled"], "free_monthly_tokens": cfg["monthly_tokens"]}


@router.get("/chat/status/{request_id}")
async def chat_status(
    request_id: str,
    current_user: User = Depends(get_current_user),
):
    """Poll the latest live research status for a chat request.

    Caddy gzip-buffers SSE, so progress statuses don't reach the browser
    incrementally. The frontend polls this lightweight endpoint instead to
    show live status updates during research. Returns 204 once the status
    expires (i.e. the request finished and was cleared).
    """
    status = await get_chat_status(request_id)
    if status is None:
        return JSONResponse(content={"status": None}, status_code=204)
    return {"status": status}


__all__ = ["router"]
