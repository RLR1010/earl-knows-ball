"""MLB chat endpoint — Earl answers MLB questions as a handicapper + DFS expert."""

import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.database import get_db
from app.models.mlb import MLBTeam, MLBPlayer, MLBGames, MLBSeason
from app.core.config import settings
from app.core.security import get_current_user
from app.models import User, ChatHistory
from app.context_processor import process_context
from app.ingestion.pgvector_search import search_articles_chat as search_articles_pgvector

router = APIRouter()

SYSTEM_PROMPT = """You are Earl, an MLB handicapper and DFS expert. You have access to real MLB data including team info, player stats, game results, and betting information.

Your specialty is helping users make money through betting and daily fantasy sports on MLB games. Lead with gambling angles.

Rules:
- Answer naturally — never mention the context itself. Don't say phrases like "based on the data provided" or "the context shows". Just give the answer.
- Use plain text only. Do NOT use markdown formatting. No asterisks around names or numbers.
- Use specific stats and numbers in your answers
- Don't give generic takes without data
- Be confident in your opinions but acknowledge uncertainty when data is limited
- Lead with gambling angles first: moneyline, run line, O/U, pitcher props
- For DFS questions: mention salary, value plays, stacking opportunities against weak pitchers
- For betting questions: reference pitcher matchups, bullpen strength, park factors, splits, weather
- Keep responses concise — a few paragraphs max
- If you don't have data for something, say so honestly
- NEVER recommend parlays or same-game parlays — they're sucker bets with terrible expected value
- NEVER suggest chasing losses or increasing bet size after a loss

The current MLB season is 2026. The 2025 season is the most recent completed season."""


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


async def retrieve_context(db: AsyncSession, message: str) -> str:
    """Gather MLB context from the database."""
    context_parts = []
    lower = message.lower()

    # Teams
    teams_r = await db.execute(select(MLBTeam).order_by(MLBTeam.name))
    all_teams = teams_r.scalars().all()
    teams_dict = {t.id: t for t in all_teams}

    # Detect mentioned teams
    for t in all_teams:
        if t.name.lower() in lower or t.abbreviation.lower() in lower:
            context_parts.append(f"TEAM: {t.name} ({t.abbreviation}) — {t.league} League, {t.division} Division")
            games_r = await db.execute(
                select(MLBGames).where(
                    (MLBGames.home_team_id == t.id) | (MLBGames.away_team_id == t.id)
                ).order_by(MLBGames.date.desc()).limit(5)
            )
            for g in games_r.scalars().all():
                home = teams_dict.get(g.home_team_id)
                away = teams_dict.get(g.away_team_id)
                if home and away:
                    score = f"{g.away_score}-{g.home_score}" if g.home_score is not None else "TBD"
                    context_parts.append(f"  {away.abbreviation} @ {home.abbreviation}: {score}")

    # Detect mentioned players
    words = [w.strip(".,!?;:'\"()[]{}") for w in lower.split()]
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)]

    for bigram in bigrams:
        words_in_bigram = bigram.split()
        if len(words_in_bigram) == 2 and all(len(w) > 2 for w in words_in_bigram):
            r = await db.execute(
                select(MLBPlayer).where(MLBPlayer.name.ilike(f"%{bigram}%")).limit(3)
            )
            for p in r.scalars().all():
                team_name = teams_dict.get(p.team_id).name if p.team_id and p.team_id in teams_dict else "FA"
                bats = f" Bats: {p.bats}" if p.bats else ""
                throws = f" Throws: {p.throws}" if p.throws else ""
                context_parts.append(f"PLAYER: {p.name} ({p.position}) — {team_name}{bats}{throws}")

    if not context_parts:
        games_r = await db.execute(
            select(MLBGames).where(MLBGames.status == "scheduled").order_by(MLBGames.date).limit(5)
        )
        games = games_r.scalars().all()
        if games:
            context_parts.append("UPCOMING GAMES:")
            for g in games:
                home = teams_dict.get(g.home_team_id)
                away = teams_dict.get(g.away_team_id)
                if home and away:
                    context_parts.append(f"  {away.abbreviation} @ {home.abbreviation}")

    # ── pgvector article search (semantic context) ──
    import logging
    logger = logging.getLogger("earl.chat_mlb")
    try:
        articles = await search_articles_pgvector(
            db=db,
            message=message,
            top_k=5,
            sport="mlb",
        )
        if articles:
            article_lines = ["\nRELEVANT ARTICLES/ANALYSIS:"]
            for i, a in enumerate(articles, 1):
                text = a["text"][:800] if len(a["text"]) > 800 else a["text"]
                article_lines.append(f"  [{i}] {text}")
            context_parts.append("\n".join(article_lines))
    except Exception as e:
        logger.warning(f"Article search failed (MLB): {e}")

    return "\n".join(context_parts) if context_parts else "No MLB data available yet."


@router.post("/chat/mlb")
async def chat_mlb(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.subscription_tier != "premium":
        raise HTTPException(status_code=403, detail="Premium subscription required")

    conv_id = req.conversation_id or str(uuid.uuid4())

    prev_result = await db.execute(
        select(ChatHistory)
        .where(
            ChatHistory.conversation_id == conv_id,
            ChatHistory.user_id == user.id,
            ChatHistory.sport == "mlb",
        )
        .order_by(ChatHistory.created_at.asc())
        .limit(10)
    )
    prev_messages = prev_result.scalars().all()

    context = await retrieve_context(db, req.message)
    context = await process_context(req.message, context)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in prev_messages:
        messages.append({"role": m.role, "content": m.message})
    messages.append({
        "role": "user",
        "content": f"Context:\n{context}\n\nQuestion: {req.message}",
    })

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.deepseek_model,
                    "messages": messages,
                    "temperature": 0.7,
                    "max_tokens": 1024,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            answer = data["choices"][0]["message"]["content"]
            tokens = data["usage"]["total_tokens"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI service error: {str(e)}")

    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="mlb",
        role="user", message=req.message, model=settings.deepseek_model
    ))
    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="mlb",
        role="assistant", message=answer, model=settings.deepseek_model, tokens_used=tokens
    ))
    await db.commit()

    return {
        "conversation_id": conv_id,
        "response": answer,
        "tokens_used": tokens,
    }
