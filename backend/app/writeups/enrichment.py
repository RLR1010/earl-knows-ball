"""Article enrichment pipeline — vector search + DeepSeek summarization.

Searches the pgvector article DB for recent articles about game teams
and starting pitchers, then calls DeepSeek to extract a concise summary
of what's pertinent for a game write-up.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.ingestion.pgvector_search import search_articles

logger = logging.getLogger("writeups.enrichment")

DEEPSEEK_MODEL = "deepseek-v4-flash"
MAX_RETURN_WORDS = 300  # keep the final summary tight


async def enrich_writeup_context(
    db: AsyncSession,
    sport: str,
    home_team: str,
    away_team: str,
    game_date: datetime,
    starting_pitchers: Optional[list[str]] = None,
    pitching_matchup: Optional[dict] = None,
    retry: bool = False,
) -> dict[str, Any]:
    """Search recent articles + have DeepSeek extract writeup-relevant context.

    Args:
        sport: "nfl", "nba", or "mlb"
        game_date: The game date (articles from 7 days before are searched)
        starting_pitchers: For MLB — names of both SPs
        retry: If True, uses broader generic queries (used after first pass returned empty)

    Returns:
        Dict with keys:
          enriched_summary: str — concise writeup-relevant context (or "" if nothing found)
          article_count: int — how many articles were found
          search_queries: list[str] — what queries were run
    """
    # ── 1. Search window: articles published before game time ──
    # Only use pre-game articles — no recaps or post-game analysis
    if isinstance(game_date, datetime):
        date_to = game_date  # Only articles published BEFORE game start
    else:
        # Combine with end of day — no tz info, but game_date was date-only
        date_to = datetime.combine(game_date, datetime.max.time())
    # Ensure timezone-aware (use UTC as reference)
    if isinstance(date_to, datetime) and date_to.tzinfo is None:
        date_to = date_to.replace(tzinfo=timezone.utc)
    date_from = date_to - timedelta(days=7)

    # ── 2. Build search queries ─────────────────────────────
    if retry:
        # Broader generic queries — catch articles missed on first pass
        queries = [
            f"{home_team} {sport.upper()} 2026",
            f"{away_team} {sport.upper()} 2026",
            f"{home_team} baseball latest",
            f"{away_team} baseball latest",
        ]
    else:
        date_query = ""
        if isinstance(game_date, (datetime,)):
            date_query = game_date.strftime(" %B %Y")
        elif isinstance(game_date, str):
            date_query = f" {game_date[:7]}"

        queries = [
            f"{home_team} {away_team}{date_query} game preview",
            f"{home_team} {away_team}{date_query} injury report",
            f"{home_team} {away_team}{date_query} betting odds",
            f"{home_team} news",
            f"{away_team} news",
        ]
        if starting_pitchers:
            for sp in starting_pitchers:
                if sp:
                    queries.append(f"{sp}")

    all_articles: list[dict] = []
    seen_urls: set[str] = set()

    for query in queries:
        results = await search_articles(
            db=db,
            query=query,
            top_k=5,
            sport=sport,
            date_to=date_to,
            date_from=date_from,
        )
        for article in results:
            uid = article.get("title", "")
            if uid and uid not in seen_urls:
                seen_urls.add(uid)
                all_articles.append(article)

        # Don't hammer; small delay between queries
        import asyncio
        await asyncio.sleep(0.1)

    # Sort by proximity to game time — closest published first
    def _sort_key(article: dict) -> float:
        pub_str = article.get("published_at", "")
        if pub_str:
            try:
                pub_dt = datetime.fromisoformat(pub_str)
            except (ValueError, TypeError):
                return float("inf")
            # Ensure both are tz-aware for comparison
            pub_dt = pub_dt.replace(tzinfo=timezone.utc) if pub_dt.tzinfo is None else pub_dt
            return abs((pub_dt - date_to).total_seconds())
        return float("inf")

    all_articles.sort(key=_sort_key)
    all_articles = all_articles[:12]  # cap at 12 articles

    if not all_articles:
        return {
            "enriched_summary": "",
            "article_count": 0,
            "search_queries": queries,
        }

    # ── 3. Send to DeepSeek for relevant-context extraction ──
    summary = await _call_deepseek_enrichment(
        home_team=home_team,
        away_team=away_team,
        game_date=game_date,
        articles=all_articles,
        pitching_matchup=pitching_matchup,
    )

    return {
        "enriched_summary": summary,
        "article_count": len(all_articles),
        "search_queries": queries,
    }


def _format_pitching_block(pitching_matchup: Optional[dict]) -> str:
    """Build a readable pitching matchup block for DeepSeek prompts."""
    if not pitching_matchup:
        return ""
    lines = []
    for side in ("home", "away"):
        p = pitching_matchup.get(side, {})
        name = p.get("name", "TBD")
        lines.append(f"  {side.title()} starter: {name}")
        ss = p.get("season_stats")
        if ss:
            parts = []
            for k in ("era", "whip", "k_per_9", "bb_per_9", "fip", "wins", "losses"):
                if k in ss:
                    parts.append(f"{k}={ss[k]}")
            if parts:
                lines.append(f"    Season: {', '.join(parts)}")
        rs = p.get("recent_starts", [])
        if rs:
            lines.append(f"    Recent starts ({len(rs)}):")
            for start in rs[:5]:
                date = start.get("game_date", "") or start.get("pitcher_date", "")
                ip = start.get("ip", 0)
                er = start.get("er", "?")
                k = start.get("k", "?")
                bb = start.get("bb", "?")
                result = start.get("result", start.get("outcome", ""))
                lines.append(f"      {date}: {ip} IP, {er} ER, {k} K, {bb} BB [{result}]")
    return "\n".join(lines)


async def _call_deepseek_enrichment(
    home_team: str,
    away_team: str,
    game_date: datetime,
    articles: list[dict],
    pitching_matchup: Optional[dict] = None,
) -> str:
    """Send articles to DeepSeek and get back a concise writeup-relevant summary."""
    # Build the articles block
    article_blocks = []
    for i, a in enumerate(articles, 1):
        article_blocks.append(
            f"[Article {i} — {a.get('source_name', 'Unknown')}]\n"
            f"Title: {a.get('title', '')}\n"
            f"Content: {a.get('text', '')[:800]}\n"
        )
    articles_text = "\n\n".join(article_blocks)

    # Build pitching matchup block (always, even if no articles)
    pitching_block = _format_pitching_block(pitching_matchup)

    system_prompt = (
        "You are a sports research analyst. Your job is to scan recent articles "
        "about two teams and find information that would be genuinely useful for "
        "writing a game preview article.\n\n"
        "ARTICLE TIMING: These articles were all published BEFORE this game. "
        "They are sorted by proximity to game time (closest first). "
        "Prioritize articles published closest to game time as they have the most "
        "current information.\n\n"
        "PLAYOFF/SERIES CONTEXT: If these teams face each other multiple times "
        "(e.g. a playoff series), use the article's content and date to determine "
        "which specific game it refers to. Only include information that pertains "
        "to THIS game on THIS date — not earlier games in the series.\n\n"
        "Focus on:\n"
        "- Key injuries or lineup changes\n"
        "- Recent form / streaks (wins, losses, hot/cold players)\n"
        "- Pitching matchup context (MLB only)\n"
        "- Team morale / clubhouse news\n"
        "- Any narrative angles (revenge game, debut, milestone chase)\n"
        "- Weather impact if outdoor (but skip if dome/indoor)\n\n"
        "IGNORE:\n"
        "- Generic preview content already obvious from team stats\n"
        "- Fluff or clickbait headlines without substance\n"
        "- Speculative trade rumors\n\n"
        "If there's genuinely useful context, provide a concise summary "
        "(max {MAX_RETURN_WORDS} words). "
        "If nothing substantial is found, still write a brief summary "
        "like 'No newsworthy developments found for this matchup.' "
        "Do NOT respond with NO_RELEVANT_INFO — always write something useful."
    )

    user_lines = [
        f"Game: {home_team} vs {away_team}",
        f"Date: {game_date.strftime('%Y-%m-%d')}",
    ]
    if pitching_block:
        user_lines.append("")
        user_lines.append("=== PITCHING MATCHUP ===")
        user_lines.append(pitching_block)
    if articles_text:
        user_lines.append("")
        user_lines.append("Recent articles found about these teams:")
        user_lines.append("")
        user_lines.append(articles_text)
    user_lines.append("")
    user_lines.append(
        "What information from these articles (and the pitching matchup above) "
        "is actually useful for writing a game preview? Be concise and specific."
    )
    user_prompt = "\n".join(user_lines)

    try:
        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=f"{settings.deepseek_base_url}/v1",
        )
        response = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=1024,
            timeout=120.0,  # 2 min — DeepSeek sometimes needs extra time
        )

        content = (response.choices[0].message.content or "").strip()
        if content in ("NO_RELEVANT_INFO", ""):
            logger.info("DeepSeek enrichment: no relevant info — building fallback from article text")
            seen_sources = set()
            fallback_parts = []
            for a in (articles or [])[:8]:
                source = a.get("source_name", "")
                if source in seen_sources:
                    continue
                seen_sources.add(source)
                title = a.get("title", "") or ""
                pub_date = a.get("published_at")
                snippet = (a.get("body", "") or "")[:200].strip()
                if isinstance(pub_date, datetime):
                    date_tag = f" ({pub_date.strftime('%b %d, %Y')})"
                else:
                    date_tag = f" ({pub_date})" if pub_date else ""
                if snippet:
                    fallback_parts.append(f"• {title}{date_tag} — {source}\n  {snippet}")
                else:
                    fallback_parts.append(f"• {title}{date_tag} — {source}")
            if fallback_parts:
                result = "Recent articles:\n" + "\n\n".join(fallback_parts)
                logger.info("Fallback enrichment: built %d chars from %d articles", len(result), len(fallback_parts))
                return result[:1500]
            return ""

        logger.info(
            "DeepSeek enrichment: found %d chars of context",
            len(content),
        )
        return content[:1500]

    except Exception as e:
        logger.warning("DeepSeek enrichment call failed: %s", e)
        return ""
