"""NBA chat endpoint — Earl answers NBA questions as a handicapper + DFS expert."""

import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from app.database import get_db
from app.models.nba import NBATeam, NBAPlayer, NBAGame, NBASeason
from app.core.config import settings
from app.core.security import get_current_user
from app.models import User, ChatHistory
from app.context_processor import process_context
from app.ingestion.pgvector_search import search_articles_chat as search_articles_pgvector

router = APIRouter()

SYSTEM_PROMPT = """You are Earl, an NBA handicapper and DFS expert. You have access to real NBA data including team info, player stats, game results, and betting information.

Your specialty is helping users make money through betting and daily fantasy sports on NBA games. Lead with gambling angles.

Rules:
- Answer naturally — never mention the context itself. Don't say phrases like "based on the data provided" or "the context shows". Just give the answer.
- Use plain text only. Do NOT use markdown formatting. No asterisks around names or numbers.
- Use specific stats and numbers in your answers
- Don't give generic takes without data
- Be confident in your opinions but acknowledge uncertainty when data is limited
- Lead with gambling angles first: spreads, O/U, player props, moneyline
- For DFS questions: mention salary, value plays, stacking opportunities
- For betting questions: reference spreads, O/U, line movement, situational factors (back-to-backs, rest days, travel)
- Keep responses concise — a few paragraphs max
- If you don't have data for something, say so honestly
- NEVER recommend parlays or same-game parlays — they're sucker bets with terrible expected value
- NEVER suggest chasing losses or increasing bet size after a loss

The current NBA season is 2025-26. The 2024-25 season (year 2025) is the most recent completed season. NBA seasons are labeled by their starting calendar year."""


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


async def retrieve_context(db: AsyncSession, message: str) -> str:
    """Gather NBA context from the database."""
    context_parts = []
    lower = message.lower()

    # Teams
    teams_r = await db.execute(select(NBATeam).order_by(NBATeam.name))
    all_teams = teams_r.scalars().all()
    teams_dict = {t.id: t for t in all_teams}

    # Detect mentioned teams
    for t in all_teams:
        if t.name.lower() in lower or t.abbreviation.lower() in lower:
            context_parts.append(f"TEAM: {t.name} ({t.abbreviation}) — {t.conference}ern Conference, {t.division} Division")
            # Get recent games
            games_r = await db.execute(
                select(NBAGame).where(
                    (NBAGame.home_team_id == t.id) | (NBAGame.away_team_id == t.id)
                ).order_by(NBAGame.date.desc()).limit(5)
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
    skip_words = {"how","what","when","where","why","will","can","did","does","the","a","an","is","are","to","for","of","in","on","at","with","and","or","this","that","going","about","from","his","her","their","its","been","has","had","play","plays","playing","season","game","games","like","just","get","got"}

    for bigram in bigrams:
        words_in_bigram = bigram.split()
        if len(words_in_bigram) == 2 and all(len(w) > 2 for w in words_in_bigram):
            r = await db.execute(
                select(NBAPlayer).where(NBAPlayer.name.ilike(f"%{bigram}%")).limit(3)
            )
            for p in r.scalars().all():
                team_name = teams_dict.get(p.team_id).name if p.team_id and p.team_id in teams_dict else "FA"
                context_parts.append(f"PLAYER: {p.name} ({p.position}) — {team_name}")

    if not context_parts:
        # Default context: show today's/upcoming games
        games_r = await db.execute(
            select(NBAGame).where(NBAGame.status == "scheduled").order_by(NBAGame.date).limit(5)
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
    logger = logging.getLogger("earl.chat_nba")
    try:
        articles = await search_articles_pgvector(
            db=db,
            message=message,
            top_k=5,
            sport="nba",
        )
        if articles:
            article_lines = ["\nRELEVANT ARTICLES/ANALYSIS:"]
            for i, a in enumerate(articles, 1):
                text = a["text"][:800] if len(a["text"]) > 800 else a["text"]
                article_lines.append(f"  [{i}] {text}")
            context_parts.append("\n".join(article_lines))
    except Exception as e:
        logger.warning(f"Article search failed (NBA): {e}")

    return "\n".join(context_parts) if context_parts else "No NBA data available yet."


@router.post("/chat/nba")
async def chat_nba(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.subscription_tier != "premium":
        raise HTTPException(status_code=403, detail="Premium subscription required")

    conv_id = req.conversation_id or str(uuid.uuid4())

    # Conversation history scoped to NBA
    prev_result = await db.execute(
        select(ChatHistory)
        .where(
            ChatHistory.conversation_id == conv_id,
            ChatHistory.user_id == user.id,
            ChatHistory.sport == "nba",
        )
        .order_by(ChatHistory.created_at.asc())
        .limit(10)
    )
    prev_messages = prev_result.scalars().all()

    context = await retrieve_context(db, req.message)
    context = await process_context(req.message, context)

    # Build messages
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in prev_messages:
        messages.append({"role": m.role, "content": m.message})
    messages.append({
        "role": "user",
        "content": f"Context:\n{context}\n\nQuestion: {req.message}",
    })

    # Call DeepSeek
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

    # Save history
    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="nba",
        role="user", message=req.message, model=settings.deepseek_model
    ))
    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="nba",
        role="assistant", message=answer, model=settings.deepseek_model, tokens_used=tokens
    ))
    await db.commit()

    return {
        "conversation_id": conv_id,
        "response": answer,
        "tokens_used": tokens,
    }
