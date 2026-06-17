"""
HoopsRumors historical article scraper.

HoopsRumors (hoopsrumors.com) is a WordPress site where:
  - Main RSS feed: /feed/?paged={n}
  - Each page returns 15 articles with full body in <content:encoded>
  - The feed doesn't distinguish years/months — it's one chronological feed
  - Archive goes back to ~2011 (about 5,000+ pages)

Strategy: crawl newest→oldest pages until we hit articles we've already seen
or the feed runs out. Dedup by source_url.

Stores in nba.articles using NBAArticle model.
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

from app.models.nba import NBAArticle

logger = logging.getLogger("earl.hoopsrumors")

FEED_URL = "https://www.hoopsrumors.com/feed"
DELAY = 0.75  # seconds between pages


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\- ]+", " ", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:200]


def _map_category(title: str, body: str = "") -> str:
    """Map HoopsRumors content to standardized category."""
    t = (title + " " + body[:500]).lower()
    if any(w in t for w in ["trade", "rumor", "free agent", "signing", "contract", "extension", "option"]):
        return "news"
    if any(w in t for w in ["draft", "lottery", "pick", "prospect"]):
        return "team_analysis"
    if any(w in t for w in ["fantasy", "roto", "dfs"]):
        return "fantasy_advice"
    if any(w in t for w in ["betting", "odds", "over/under", "spread"]):
        return "betting_pick"
    if any(w in t for w in ["injury", "out", "return", "surgery"]):
        return "injury_report"
    if any(w in t for w in ["salary", "cap", "luxury tax", "payroll", "apron"]):
        return "team_analysis"
    if any(w in t for w in ["interview", "mailbag", "q&a", "chat"]):
        return "general"
    if any(w in t for w in ["g league", "two-way", "exhibit 10"]):
        return "news"
    if any(w in t for w in ["coach", "firing", "hiring", "executive", "gm"]):
        return "news"
    return "general"


def _html_to_text(html: str) -> str:
    """Strip HTML tags while preserving paragraph breaks."""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#8217;", "'", text)
    text = re.sub(r"&#8216;", "'", text)
    text = re.sub(r"&#8220;", '"', text)
    text = re.sub(r"&#8221;", '"', text)
    text = re.sub(r"&#8211;", "-", text)
    text = re.sub(r"&#8212;", "—", text)
    text = re.sub(r"&#8230;", "...", text)
    text = re.sub(r"&#038;|&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", " ", text)
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


async def scrape_hoopsrumors_all(
    db: AsyncSession,
    max_pages: Optional[int] = None,
    max_articles: Optional[int] = None,
    delay: float = DELAY,
) -> dict:
    """
    Scrape all HoopsRumors articles via paginated RSS.

    Crawls newest→oldest, stops when all articles on a page are already in DB
    or the feed runs out.

    Returns stats dict.
    """
    # Pre-fetch existing HoopsRumors source_urls for dedup
    result = await db.execute(
        select(NBAArticle.source_url).where(
            NBAArticle.source_name == "HoopsRumors"
        )
    )
    existing_urls = set(row[0] for row in result.fetchall() if row[0])
    logger.info(f"Pre-fetched {len(existing_urls)} existing HoopsRumors URLs")

    stats = {"articles_scraped": 0, "duplicates": 0, "errors": 0, "pages_scanned": 0}
    total_pages_to_scan = max_pages if max_pages else float("inf")

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        page = 1
        while page <= total_pages_to_scan:
            if max_articles and stats["articles_scraped"] >= max_articles:
                break

            url = f"{FEED_URL}/?paged={page}"
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.info(f"  HTTP {resp.status_code} at page {page}, stopping")
                    break
            except Exception as e:
                logger.warning(f"  Request failed at page {page}: {e}")
                break

            stats["pages_scanned"] += 1
            feed = feedparser.parse(BytesIO(resp.content))

            if not feed.entries:
                logger.info(f"  No entries at page {page}, reached end of archive")
                break

            page_new = 0
            for entry in feed.entries:
                if max_articles and stats["articles_scraped"] >= max_articles:
                    break

                link = entry.get("link", "").strip()
                if not link:
                    continue

                # Dedup by URL
                if link in existing_urls:
                    stats["duplicates"] += 1
                    continue

                title = entry.get("title", "").strip()
                if not title:
                    continue

                # Extract body
                body = await extract_article_body(entry)
                if not body or len(body) < 100:
                    stats["errors"] += 1
                    existing_urls.add(link)  # mark as seen to avoid retry
                    continue

                # Build unique slug
                title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
                slug = f"{_slugify(title)}-{title_hash}"

                # Check for duplicate by slug
                dup = await db.execute(
                    select(NBAArticle.id).where(NBAArticle.slug == slug)
                )
                if dup.scalar_one_or_none():
                    stats["duplicates"] += 1
                    existing_urls.add(link)
                    continue

                # Get date
                pub_struct = entry.get("published_parsed")
                pub_date = (
                    datetime(*pub_struct[:6], tzinfo=timezone.utc)
                    if pub_struct
                    else datetime.now(timezone.utc)
                )

                # Author
                author = None
                if hasattr(entry, "author"):
                    author = entry.author
                elif hasattr(entry, "authors") and entry.authors:
                    author = entry.authors[0].get("name")
                author = author or "HoopsRumors Staff"

                # Category
                category = _map_category(title, body)

                article = NBAArticle(
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
                    source_name="HoopsRumors",
                    source_type="rss",
                )
                db.add(article)
                existing_urls.add(link)
                stats["articles_scraped"] += 1
                page_new += 1

            # Commit after each page
            try:
                await db.commit()
            except Exception as e:
                logger.error(f"  Commit error at page {page}: {e}")
                await db.rollback()
                break

            logger.info(
                f"  Page {page}: +{page_new} articles "
                f"(dupes: {stats['duplicates']}, errors: {stats['errors']}) "
                f"| Total: {stats['articles_scraped']}"
            )

            # If every article on this page was a duplicate, we've caught up
            if page_new == 0 and len(feed.entries) > 0:
                all_dupes = all(
                    entry.get("link", "").strip() in existing_urls
                    for entry in feed.entries
                )
                if all_dupes:
                    logger.info(
                        "  All articles on this page are already in DB, archive is caught up"
                    )
                    break

            page += 1
            import asyncio
            await asyncio.sleep(delay)

    return stats
