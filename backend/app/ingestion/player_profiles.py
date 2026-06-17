"""
Player profile generator.
Compiles player data into a Wikipedia-style narrative with Bio + Draft sections,
then an LLM-generated prose career summary. Pushes to Cognee-NFL for semantic search.
"""
import hashlib
import json
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, PlayerWeeklyStats, DepthChart, Team, Season

from app.core.config import settings

logger = logging.getLogger("earl.player_profiles")


async def _generate_career_summary(player: Player, db: AsyncSession) -> str:
    """Generate 2-3 paragraphs of Wikipedia-style prose summarizing the player's career."""
    bio_data = {
        "name": player.name,
        "position": player.position,
        "college": player.college or "unknown",
    }

    # Gather career stats
    r = await db.execute(
        select(
            func.count(PlayerWeeklyStats.id).label("games"),
            func.sum(PlayerWeeklyStats.pass_yards).label("pass_yds"),
            func.sum(PlayerWeeklyStats.pass_tds).label("pass_tds"),
            func.sum(PlayerWeeklyStats.rush_yards).label("rush_yds"),
            func.sum(PlayerWeeklyStats.rush_tds).label("rush_tds"),
            func.sum(PlayerWeeklyStats.receptions).label("rec"),
            func.sum(PlayerWeeklyStats.receiving_yards).label("rec_yds"),
            func.sum(PlayerWeeklyStats.receiving_tds).label("rec_tds"),
            func.min(Season.year).label("first_year"),
            func.max(Season.year).label("last_year"),
        )
        .join(Season, PlayerWeeklyStats.season_id == Season.id)
        .where(PlayerWeeklyStats.player_id == player.id)
    )
    s = r.one()
    has_stats = s.games and s.games > 0

    # Team name
    team_name = ""
    if player.team_id:
        rt = await db.execute(select(Team).where(Team.id == player.team_id))
        t = rt.scalar_one_or_none()
        if t:
            team_name = t.name

    # Build the data we'll feed DeepSeek
    data = f"""Player: {player.name}
Position: {player.position}
College: {bio_data['college']}
Draft: {'Round ' + str(player.draft_round) + ', Pick ' + str(player.draft_pick) + ' (' + str(player.draft_year) + ')' if player.draft_year and player.draft_round and player.draft_pick else 'Undrafted/Unknown'}
Draft Team: {player.draft_team or 'N/A'}
Current Team: {team_name or 'Free Agent'}
"""
    if has_stats:
        data += f"""Games Played: {s.games} ({s.first_year}-{s.last_year})
Passing Yards: {int(s.pass_yds or 0):,}
Passing TDs: {int(s.pass_tds or 0)}
Rushing Yards: {int(s.rush_yds or 0):,}
Rushing TDs: {int(s.rush_tds or 0)}
Receptions: {int(s.rec or 0)}
Receiving Yards: {int(s.rec_yds or 0):,}
Receiving TDs: {int(s.rec_tds or 0)}
"""

    system_prompt = (
        "You are a sports biographer. Write a 2-3 paragraph Wikipedia-style summary "
        "of the player's football career. Cover their college career, draft, and NFL career. "
        "Use specific numbers and stats from the data provided. "
        "Write in third person. Use a professional, encyclopedic tone. "
        "Do NOT use markdown headings or bullet points — just flowing paragraphs. "
        "Keep it to 150-250 words."
    )

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                "https://api.deepseek.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "deepseek-chat",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": f"Write a career summary for this player:\n\n{data}"},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 512,
                },
            )
            if resp.status_code == 200:
                result = resp.json()
                summary = result["choices"][0]["message"]["content"].strip()
                if summary:
                    return summary
    except Exception as e:
        logger.warning(f"DeepSeek summary failed for {player.name}: {e}")

    # Fallback
    if has_stats:
        return f"{player.name} played {s.games} games from {s.first_year} to {s.last_year}, recording {int(s.pass_yds or 0):,} passing yards, {int(s.rush_yds or 0):,} rushing yards, and {int(s.rec or 0)} receptions for {int(s.rec_yds or 0):,} receiving yards."
    return f"{player.name} is a {player.position} from {bio_data['college']}."


async def build_profile(db: AsyncSession, player: Player) -> str:
    """Build a Wikipedia-style player profile with BIO, Draft, and narrative summary."""
    lines = []
    lines.append(f"# PLAYER PROFILE: {player.name}")
    lines.append("")

    # ── Bio ──
    lines.append("## BIO")
    lines.append(f"- **Position:** {_pos_group(player.position)} ({player.position})")
    if player.college:
        lines.append(f"- **College:** {player.college}")
    if player.birth_date:
        lines.append(f"- **Born:** {player.birth_date}")
    if player.years_exp is not None:
        lines.append(f"- **NFL Experience:** {player.years_exp} years")
    if player.height:
        ft = player.height // 12
        inc = player.height % 12
        lines.append(f"- **Height:** {ft}'{inc}\"")
    if player.weight:
        lines.append(f"- **Weight:** {player.weight} lbs")
    if player.team_id:
        r = await db.execute(select(Team).where(Team.id == player.team_id))
        team = r.scalar_one_or_none()
        if team:
            lines.append(f"- **Current Team:** {team.name} ({team.abbreviation})")

    r = await db.execute(
        select(DepthChart)
        .where(DepthChart.player_id == player.id)
        .order_by(DepthChart.slot).limit(1)
    )
    dc = r.scalar_one_or_none()
    if dc:
        slot_label = "Starter" if dc.slot == 1 else f"#{dc.slot}"
        lines.append(f"- **Depth Chart:** {dc.position} {slot_label} ({dc.status})")
    lines.append("")

    # ── Draft ──
    lines.append("## DRAFT")
    if player.draft_year and player.draft_round and player.draft_pick:
        dt = f" — {player.draft_team}" if player.draft_team else ""
        lines.append(f"- Round {player.draft_round}, Pick {player.draft_pick} ({player.draft_year}){dt}")
    elif player.draft_year:
        lines.append(f"- {player.draft_year} draft (details not available)")
    else:
        lines.append("_(Undrafted or unknown)_")
    lines.append("")

    # ── Career Summary (narrative prose) ──
    lines.append("## CAREER SUMMARY")
    summary = await _generate_career_summary(player, db)
    for para in summary.split("\n\n"):
        para = para.strip()
        if para:
            lines.append(para)
            lines.append("")

    return "\n".join(lines)


async def generate_all_profiles(
    db: AsyncSession,
    position_filter: str | None = None,
    limit: int | None = None,
) -> dict:
    query = select(Player)
    if position_filter:
        query = query.where(Player.position == position_filter.upper())
    query = query.order_by(Player.id).limit(limit or 5000)

    r = await db.execute(query)
    players = r.scalars().all()
    results = {"total": len(players), "generated": 0, "embedded": 0, "errors": 0}

    for player in players:
        try:
            profile = await build_profile(db, player)
            title = f"Player Profile: {player.name}"
            slug = hashlib.sha256(title.encode()).hexdigest()[:16]
            if embed:

                if ok:
                    results["embedded"] += 1
            results["generated"] += 1
            if results["generated"] % 50 == 0:
                logger.info(f"  Generated {results['generated']}/{len(players)} profiles")
        except Exception as e:
            logger.error(f"Error generating profile for player {player.id} ({player.name}): {e}")
            results["errors"] += 1
    return results


async def generate_profile_for_player(db: AsyncSession, player_id: int, embed: bool = True) -> dict:
    r = await db.execute(select(Player).where(Player.id == player_id))
    player = r.scalar_one_or_none()
    if not player:
        return {"error": "Player not found"}
    profile = await build_profile(db, player)
    title = f"Player Profile: {player.name}"
    slug = hashlib.sha256(title.encode()).hexdigest()[:16]
    embedded = 0
    if embed:

        embedded = 1 if ok else 0
    return {
        "player_id": player.id,
        "name": player.name,
        "profile_length": len(profile),
        "embedded": bool(embedded),
    }


def _pos_group(pos: str) -> str:
    return {
        "QB": "Quarterback", "RB": "Running Back", "WR": "Wide Receiver",
        "TE": "Tight End", "K": "Kicker", "DST": "Defense/Special Teams",
    }.get(pos, pos)
