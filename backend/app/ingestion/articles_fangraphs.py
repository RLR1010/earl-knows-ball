"""
Fangraphs historical article scraper.

Fangraphs (blogs.fangraphs.com) is a WordPress site where:
  - Monthly RSS feeds: /{year}/{month:02d}/feed/?paged={n}
  - Each page returns 10 articles with full body in content[0].value
  - Full article body HTML + plain text available via RSS (no JS rendering needed)
  - Archives go back to ~2009

Strategy: crawl newest→oldest, paginate each month until empty.
Stores in mlb.articles using MLBArticle model.
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import feedparser
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import MLBArticle

logger = logging.getLogger("earl.fangraphs")

BASE_URL = "https://blogs.fangraphs.com"
FEED_URL_TEMPLATE = f"{BASE_URL}/{{year}}/{{month:02d}}/feed/"
DELAY = 0.75  # seconds between articles


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\- ]+", " ", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:200]


def _map_category(title: str, body: str = "") -> str:
    """Map Fangraphs article content to standardized category."""
    t = (title + " " + body[:500]).lower()
    if any(w in t for w in ["prospect", "top prospect", "farm system", "scouting report"]):
        return "team_analysis"
    if any(w in t for w in ["fantasy", "roto", "faab", "waiver", "dynasty"]):
        return "fantasy_advice"
    if any(w in t for w in ["betting", "odds", "pick", "over/under", "props"]):
        return "betting_pick"
    if any(w in t for w in ["game recap", "recap", "yesterday"]):
        return "game_recap"
    if any(w in t for w in ["preview", "preview", "coming up"]):
        return "game_preview"
    if any(w in t for w in ["draft", "mock draft", "draft prospect"]):
        return "team_analysis"
    if any(w in t for w in ["statcast", "sabermetrics", "analysis", "leaderboard", "percentile"]):
        return "team_analysis"
    if any(w in t for w in ["trade", "rumor", "signing", "contract", "extension"]):
        return "news"
    if any(w in t for w in ["chat", "mailbag", "q&a"]):
        return "general"
    return "general"


def _html_to_text(html: str) -> str:
    """Strip HTML tags while preserving paragraph breaks."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#8217;", "'", text)
    text = re.sub(r"&#8211;", "-", text)
    text = re.sub(r"&#8212;", "—", text)
    text = re.sub(r"&#8230;", "...", text)
    text = re.sub(r"&#038;|&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-z]+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


async def extract_article_body(entry: dict) -> Optional[str]:
    """Extract plain-text article body from RSS feed entry."""
    # Try content:encoded first (WordPress full body)
    content = entry.get("content", [])
    if content:
        raw = content[0].get("value", "")
        if len(raw) > 200:
            return _html_to_text(raw)

    # Fall back to summary
    summary = entry.get("summary", "")
    if summary and len(summary) > 200:
        return _html_to_text(summary)

    return None


async def scrape_fangraphs_month(
    db: AsyncSession,
    year: int,
    month: int,
    delay: float = DELAY,
    max_articles: Optional[int] = None,
    existing_urls: Optional[set] = None,
) -> dict:
    """
    Scrape one month of Fangraphs articles via paginated RSS.

    Returns stats dict.
    """
    base_url = FEED_URL_TEMPLATE.format(year=year, month=month)
    stats = {"articles_scraped": 0, "duplicates": 0, "errors": 0, "pages_scanned": 0}
    local_existing = set(existing_urls or [])

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        page = 1
        while True:
            if max_articles and stats["articles_scraped"] >= max_articles:
                break

            url = f"{base_url}?paged={page}"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    break
            except Exception:
                break

            stats["pages_scanned"] += 1
            feed = feedparser.parse(BytesIO(resp.content))

            if not feed.entries:
                break  # No more pages

            for entry in feed.entries:
                if max_articles and stats["articles_scraped"] >= max_articles:
                    break

                link = entry.get("link", "").strip()
                if not link:
                    continue

                # Dedup by URL
                if link in local_existing:
                    stats["duplicates"] += 1
                    continue

                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Extract body
                body = await extract_article_body(entry)
                if not body or len(body) < 100:
                    stats["errors"] += 1
                    continue

                # Build unique slug
                title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
                slug = f"{_slugify(title)}-{title_hash}"

                # Check for duplicate by slug
                dup = await db.execute(
                    select(MLBArticle.id).where(MLBArticle.slug == slug)
                )
                if dup.scalar_one_or_none():
                    stats["duplicates"] += 1
                    local_existing.add(link)
                    continue

                # Get date
                pub_struct = entry.get("published_parsed")
                pub_date = (
                    datetime(*pub_struct[:6], tzinfo=timezone.utc)
                    if pub_struct
                    else datetime(year, month, 1, tzinfo=timezone.utc)
                )

                # Author
                author = None
                if hasattr(entry, "author"):
                    author = entry.author
                elif hasattr(entry, "authors") and entry.authors:
                    author = entry.authors[0].get("name")
                author = author or "FanGraphs Staff"

                # Category
                category = _map_category(title, body)

                article = MLBArticle(
                    title=title,
                    slug=slug,
                    body=body,
                    excerpt=body[:500],
                    category=category,
                    tier="free",
                    published=True,
                    published_at=pub_date,
                    author=author,
                    source_url=link,
                    source_name="FanGraphs",
                    source_type="rss",
                )
                db.add(article)
                local_existing.add(link)
                stats["articles_scraped"] += 1

                import asyncio
                await asyncio.sleep(delay)

            page += 1

    return stats


async def scrape_fangraphs_all(
    db: AsyncSession,
    start_year: int = 2026,
    end_year: Optional[int] = None,
    delay: float = DELAY,
    max_articles: Optional[int] = None,
) -> dict:
    """
    Scrape all Fangraphs articles from start_year to end_year, newest first.

    Pre-fetches existing URLs from mlb.articles for dedup.
    """
    if end_year is None:
        end_year = 2010  # Fangraphs blog started ~2009

    # Pre-fetch existing source_urls
    result = await db.execute(
        select(MLBArticle.source_url).where(MLBArticle.source_name == "FanGraphs")
    )
    existing_urls = set(row[0] for row in result.fetchall() if row[0])
    logger.info(f"Pre-fetched {len(existing_urls)} existing FanGraphs URLs")

    total = 0
    results_by_year = {}

    for year in range(start_year, end_year - 1, -1):  # newest first
        logger.info(f"\n{'='*60}\nYear: {year}\n{'='*60}")
        year_total = 0
        for month in range(1, 13):
            logger.info(f"  Month: {year}-{month:02d}")
            stats = await scrape_fangraphs_month(
                db=db,
                year=year,
                month=month,
                delay=delay,
                max_articles=max_articles - total if max_articles else None,
                existing_urls=existing_urls,
            )
            year_total += stats["articles_scraped"]
            total += stats["articles_scraped"]

            if stats["articles_scraped"] > 0:
                logger.info(
                    f"    +{stats['articles_scraped']} articles "
                    f"({stats['duplicates']} dupes, {stats['errors']} errors, "
                    f"{stats['pages_scanned']} pages)"
                )

            # Commit after each month
            try:
                await db.commit()
            except Exception:
                await db.rollback()

            if max_articles and total >= max_articles:
                break

        results_by_year[year] = year_total
        logger.info(f"  Year {year} total: {year_total} articles")

        # If a year had zero articles, older years probably will too
        if year_total == 0:
            logger.info(f"  No articles in {year}, stopping (older years likely empty)")
            break

        if max_articles and total >= max_articles:
            break

    return {
        "total_scraped": total,
        "years": results_by_year,
    }
