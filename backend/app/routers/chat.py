"""AI chat endpoint — Earl answers NFL questions using our database context."""

import json
import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload
from pydantic import BaseModel
from app.database import get_db
from app.models import Player, PlayerWeeklyStats, Game, Team, User, DepthChart, Transaction
from app.core.config import settings
from app.core.security import get_current_user
from app.ingestion.pgvector_search import search_articles_chat as search_articles_pgvector
from app.context_processor import process_context

router = APIRouter()

SYSTEM_PROMPT = """You are Earl, an NFL handicapper and DFS expert. You have access to real NFL data including player stats, game results, team info, betting lines, DFS salaries, and expert analysis from articles.

Your specialty is helping users make money through betting and daily fantasy sports. Lead with gambling angles.

Rules:
- Answer naturally — never mention the context itself. Don't say phrases like "based on the data provided" or "the context shows". Just give the answer.
- Use plain text only. Do NOT use markdown formatting. No asterisks around names or numbers.
- Use specific stats and numbers in your answers
- Don't give generic takes without data
- Be confident in your opinions but acknowledge uncertainty when data is limited
- Lead with gambling angles first, fantasy second
- For DFS questions: mention salary, value plays, stacking opportunities, ownership projections
- For betting questions: reference spreads, O/U, moneylines, line movement, situational factors
- When citing articles, say things naturally like "ESPN noted..." or "Recent analysis suggests..."
- Keep responses concise — a few paragraphs max
- If you don't have data for something, say so honestly
- NEVER recommend parlays or same-game parlays — they're sucker bets with terrible expected value
- NEVER suggest chasing losses or increasing bet size after a loss

Current year: 2026. The most recent completed season is 2025. We have full 2025 data including playoffs. The 2026 regular season schedule is loaded.

IMPORTANT - Draft/Rookie Context: The 2026 NFL Draft has already happened (April 2026). When the user asks about "this year's rookies" or "rookies" in general, they are asking about the 2026 draft class. Do NOT discuss the 2025 rookie class unless specifically asked about "last year's rookies" or "2025 rookies." The 2026 draft class information is in the articles context below.

When discussing rookies, ALWAYS use the player's actual name from the draft context if available. Do NOT use vague descriptions like 'the rookie signal-caller' or 'the kid the Broncos took' — say the player's name."""


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None  # for future multi-turn





async def retrieve_context(db: AsyncSession, message: str, prev_teams: list[str] | None = None) -> str:
    """Search the database for relevant context based on the user's question.
    prev_teams: team names from the previous conversation turn, used to scope follow-ups.
    """
    context_parts = []
    lower = message.lower()
    prev_teams = prev_teams or []

    # ── Season overview ──
    context_parts.append("SEASON: 2025 is the most recent completed season (272 REG + 13 POST games). The 2026 season is upcoming.")

    # ── Pre-load teams for lookup ──
    teams_dict = {}
    for t in (await db.execute(select(Team))).scalars().all():
        teams_dict[t.id] = t

    async def _fmt_game(g) -> str:
        home = teams_dict.get(g.home_team_id)
        away = teams_dict.get(g.away_team_id)
        home_abbr = home.abbreviation if home else f"T{g.home_team_id}"
        away_abbr = away.abbreviation if away else f"T{g.away_team_id}"
        score = f"{g.away_score}-{g.home_score}" if g.home_score is not None else "TBD"
        status = str(g.status).replace("GameStatus.", "").replace("GameStatus", "").upper()
        return f"  {away_abbr} @ {home_abbr}: {score} ({status})"

    async def _add_player_context(ctx_parts: list, player: Player, t_dict: dict, session):
        """Add player info, stats, and team context to the context parts."""
        team_name = "Free Agent"
        if player.team_id and player.team_id in t_dict:
            team_name = t_dict[player.team_id].name
        
        ctx_parts.append(f"PLAYER: {player.name} ({player.position}) — {team_name}")
        
        # Get 2025 season stats
        stats_r = await session.execute(
            select(
                func.sum(PlayerWeeklyStats.pass_yards).label("pass_yds"),
                func.sum(PlayerWeeklyStats.pass_tds).label("pass_tds"),
                func.sum(PlayerWeeklyStats.pass_int).label("pass_int"),
                func.sum(PlayerWeeklyStats.rush_yards).label("rush_yds"),
                func.sum(PlayerWeeklyStats.rush_tds).label("rush_tds"),
                func.sum(PlayerWeeklyStats.receiving_yards).label("rec_yds"),
                func.sum(PlayerWeeklyStats.receiving_tds).label("rec_tds"),
                func.sum(PlayerWeeklyStats.receptions).label("rec"),
                func.sum(PlayerWeeklyStats.fantasy_points_ppr).label("fantasy_ppr"),
                func.count(PlayerWeeklyStats.id).label("games_played"),
            ).where(
                PlayerWeeklyStats.player_id == player.id,
                PlayerWeeklyStats.season_id == 3,  # season_id=3 = 2025
            )
        )
        stats = stats_r.one()
        
        if stats.games_played and stats.games_played > 0:
            stat_line = (
                f"  2025 stats ({stats.games_played} games): "
                f"Rush: {stats.rush_yds or 0} yds / {stats.rush_tds or 0} TD | "
                f"Rec: {stats.rec or 0} catches / {stats.rec_yds or 0} yds / {stats.rec_tds or 0} TD | "
                f"Pass: {stats.pass_yds or 0} yds / {stats.pass_tds or 0} TD / {stats.pass_int or 0} INT | "
                f"Fantasy (PPR): {stats.fantasy_ppr or 0:.1f} pts"
            )
            ctx_parts.append(stat_line)
        
        # Depth chart position for this player
        if player.team_id:
            dc_r = await session.execute(
                select(DepthChart).where(
                    DepthChart.player_id == player.id
                ).order_by(DepthChart.slot).limit(1)
            )
            dc_entry = dc_r.scalar_one_or_none()
            if dc_entry:
                ctx_parts.append(f"  Depth chart: {dc_entry.position} #{dc_entry.slot} ({dc_entry.status})")

        # Include player's team's recent games
        if player.team_id and player.team_id in t_dict:
            t = t_dict[player.team_id]
            r = await session.execute(
                select(Game).where(
                    (Game.home_team_id == player.team_id) | (Game.away_team_id == player.team_id)
                ).order_by(Game.date.desc()).limit(6)
            )
            tg = r.scalars().all()
            if tg:
                lines = [f"  {t.name} last 6 games:"]
                for g in tg:
                    lines.append(await _fmt_game(g))
                ctx_parts.append("\n".join(lines))

    # ── Playoff / Championship detection ──
    if any(w in lower for w in ["championship", "super bowl", "playoff", "nfccg", "afccg"]):
        # Fetch ONLY post-season games (clean, always works)
        r = await db.execute(
            select(Game).where(Game.game_type == "POST").order_by(Game.week).limit(20)
        )
        games = r.scalars().all()
        if games:
            lines = ["2025 PLAYOFFS:"]
            for g in games:
                lines.append(await _fmt_game(g))
            context_parts.append("\n".join(lines))

    # ── Team mention detection ──
    team_matches = []
    matched_team_names = []
    query_tokens = set(lower.split())
    
    # Include team names from previous turn to scope follow-ups
    for pt in prev_teams:
        for t in teams_dict.values():
            if pt.lower() in t.name.lower() or pt.lower() == t.abbreviation.lower():
                if t not in team_matches:
                    team_matches.append(t)

    for t in teams_dict.values():
        name_lower = t.name.lower()
        abbr_lower = t.abbreviation.lower()
        
        # Match full team name as substring
        if name_lower in lower:
            team_matches.append(t)
            matched_team_names.append(t.name)
            continue
        
        # Match abbreviation as WHOLE WORD only (not substring of other words)
        if abbr_lower in query_tokens:
            team_matches.append(t)
            matched_team_names.append(t.name)
            continue
        
        # Match any query token that appears in team name
        if any(token in name_lower for token in query_tokens if len(token) > 3):
            team_matches.append(t)
            matched_team_names.append(t.name)

    for team in team_matches:
        context_parts.append(f"TEAM: {team.name} ({team.abbreviation})")
        r = await db.execute(
            select(Game).where(
                (Game.home_team_id == team.id) | (Game.away_team_id == team.id)
            ).order_by(Game.date.desc()).limit(6)
        )
        tg = r.scalars().all()
        if tg:
            for g in tg:
                context_parts.append(await _fmt_game(g))
        
        # Show depth chart starters for this team
        dc_r = await db.execute(
            select(DepthChart).where(
                DepthChart.team_id == team.id,
                DepthChart.slot == 1
            ).order_by(DepthChart.position)
        )
        starters = dc_r.scalars().all()
        if starters:
            dc_lines = [f"  {team.abbreviation} depth chart (starters):"]
            for dc in starters[:25]:
                dc_lines.append(f"    {dc.position}: {dc.player_name}")
            context_parts.append("\n".join(dc_lines))

    # ── Player mention detection ──
    # Tokenize the message into words
    words = [w.strip(".,!?;:'\"()[]{}") for w in lower.split()]
    # Generate bigrams (first + last name)
    bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)]
    
    # Skip common non-player words
    skip_words = {"how","what","when","where","why","will","can","did","does","was","were",
                  "the","a","an","is","are","to","for","of","in","on","at","with","and","or",
                  "this","that","going","about","from","his","her","their","its","been","has","had",
                  "play","plays","playing","season","game","games","like","just","get","got"}
    
    # First try: bigram (first + last name) — highest confidence
    matched_players = set()
    for bigram in bigrams:
        words_in_bigram = bigram.split()
        if len(words_in_bigram) == 2 and all(len(w) > 2 for w in words_in_bigram):
            r = await db.execute(
                select(Player).where(Player.name.ilike(f"%{bigram}%")).limit(3)
            )
            for p in r.scalars().all():
                if p.id not in matched_players:
                    matched_players.add(p.id)
                    await _add_player_context(context_parts, p, teams_dict, db)
    
    # If no bigram matches, try single last-name matches
    if not matched_players:
        for w in words:
            if w not in skip_words and len(w) > 3:
                r = await db.execute(
                    select(Player).where(Player.name.ilike(f"% {w}")).limit(2)
                )
                for p in r.scalars().all():
                    if p.id not in matched_players:
                        matched_players.add(p.id)
                        await _add_player_context(context_parts, p, teams_dict, db)

    # ── pgvector article search (semantic context) ──
    articles = await search_articles_pgvector(
        db=db,
        message=message,
        matched_team_names=matched_team_names,
        prev_teams=prev_teams,
        top_k=5,
    )
    if articles:
        article_lines = ["\nRELEVANT ARTICLES/ANALYSIS:"]
        for i, a in enumerate(articles, 1):
            text = a["text"][:800] if len(a["text"]) > 800 else a["text"]
            article_lines.append(f"  [{i}] {text}")
        context_parts.append("\n".join(article_lines))

    raw = "\n".join(context_parts)
    return raw


@router.post("/chat")
async def chat(
    req: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Ask Earl a question about NFL, fantasy, or betting."""
    from app.models import ChatHistory
    import uuid

    if user.subscription_tier != "premium":
        raise HTTPException(status_code=403, detail="Premium subscription required")

    # Conversation management
    conv_id = req.conversation_id or str(uuid.uuid4())

    # Load conversation history scoped to NFL (up to last 10 messages)
    prev_result = await db.execute(
        select(ChatHistory)
        .where(
            ChatHistory.conversation_id == conv_id,
            ChatHistory.user_id == user.id,
            ChatHistory.sport == "nfl",
        )
        .order_by(ChatHistory.created_at.asc())
        .limit(10)
    )
    prev_messages = prev_result.scalars().all()

    # Load conversation history to extract team context from previous turns
    prev_teams = []
    if prev_messages:
        for pm in reversed(prev_messages):
            if pm.role == "user":
                lower_msg = pm.message.lower()
                msg_words = set(lower_msg.split())
                teams_r = await db.execute(select(Team))
                all_teams = teams_r.scalars().all()
                for t in all_teams:
                    name_lower = t.name.lower()
                    abbr_lower = t.abbreviation.lower()
                    # Full team name in message
                    if name_lower in lower_msg:
                        prev_teams.append(t.name)
                    # Abbreviation as whole word
                    elif abbr_lower in msg_words:
                        prev_teams.append(t.name)
                    # Any message token appears in team name
                    elif any(token in name_lower for token in msg_words if len(token) > 3):
                        prev_teams.append(t.name)
                break

    # Fresh context based on the latest question, scoped to previous team
    context = await retrieve_context(db, req.message, prev_teams=prev_teams)

    # Rerank + deduplicate context using embedding-based processor
    context = await process_context(req.message, context)



    # Build messages for DeepSeek
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add conversation history
    for m in prev_messages:
        messages.append({"role": m.role, "content": m.message})

    # Add the new question with fresh context
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

    # Save with conversation_id
    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="nfl",
        role="user", message=req.message, model=settings.deepseek_model
    ))
    db.add(ChatHistory(
        user_id=user.id, conversation_id=conv_id, sport="nfl",
        role="assistant", message=answer, model=settings.deepseek_model, tokens_used=tokens
    ))
    await db.commit()

    return {
        "conversation_id": conv_id,
        "response": answer,
        "context_used": context,
        "tokens_used": tokens,
    }
