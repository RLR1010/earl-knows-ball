"""
ProFootballTalk (NBC Sports) archive scraper.

Uses NBC Sports sitemap index to discover PFT article URLs,
then fetches each page and extracts the body from RichTextArticleBody.

Designed to run from newest articles backward.
"""
import hashlib
import logging
import re
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article
from app.ingestion.articles import _slugify, _try_insert_article, _clean_html

logger = logging.getLogger("earl.pft_archives")

PFT_RSS = "https://www.nbcsports.com/profootballtalk.rss"
SITEMAP_INDEX = "https://www.nbcsports.com/sitemap.xml"
BASE_URL = "https://www.nbcsports.com"

HEADERS = {"User-Agent": "Mozilla/5.0 (EarlKnowsBall/1.0)"}


async def _get_sitemap_list(client: httpx.AsyncClient) -> list[str]:
    """Fetch the sitemap index and return all individual sitemap URLs, newest first."""
    resp = await client.get(SITEMAP_INDEX, headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    urls = re.findall(r'<loc>(https://www\.nbcsports\.com/sitemap-\d+\.xml)</loc>', resp.text)
    # Sort descending (newest first)
    urls.sort(reverse=True)
    return urls


async def _get_pft_urls(client: httpx.AsyncClient, sitemap_url: str, max_urls: Optional[int] = None) -> list[str]:
    """Extract all PFT article URLs from a single sitemap."""
    resp = await client.get(sitemap_url, headers=HEADERS, timeout=15.0)
    resp.raise_for_status()
    urls = re.findall(
        r'<loc>(https://www\.nbcsports\.com/nfl/profootballtalk/rumor-mill/news/[^<]+)</loc>',
        resp.text,
    )
    if max_urls:
        urls = urls[:max_urls]
    return urls


def _extract_body(html: str) -> Optional[str]:
    """Extract the article body from a PFT page."""
    # Try RichTextArticleBody (newer Brightspot template)
    m = re.search(
        r'class="RichTextArticleBody[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>\s*</div>\s*</div>',
        html,
        re.DOTALL,
    )
    if m:
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 100:
            return text

    # Try the Page-body content
    m = re.search(r'class="Page-body"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
    if m:
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'\s+', ' ', text).strip()
        if len(text) > 100:
            return text

    return None


def _extract_meta(html: str) -> dict:
    """Extract title and published date from HTML meta tags."""
    result = {"title": "", "published_at": None}
    m = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]*)"', html)
    if m:
        result["title"] = m.group(1)
    m = re.search(r'<meta[^>]*property="article:published_time"[^>]*content="([^"]*)"', html)
    if m:
        try:
            result["published_at"] = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    m = re.search(r'<meta[^>]*name="author"[^>]*content="([^"]*)"', html)
    if m:
        result["author"] = m.group(1)
    return result


async def _fetch_and_save(
    db: AsyncSession,
    client: httpx.AsyncClient,
    url: str,
    source_name: str,
) -> Optional[int]:
    """Fetch a PFT article page, extract content, and save to DB. Returns article ID or None."""
    try:
        resp = await client.get(url, headers=HEADERS, timeout=30.0, follow_redirects=True)
        if resp.status_code != 200:
            logger.warning(f"HTTP {resp.status_code} for {url[:80]}")
            return None

        html = resp.text
        body = _extract_body(html)
        if not body or len(body) < 50:
            logger.warning(f"No body extracted from {url[:80]}")
            return None

        meta = _extract_meta(html)
        title = meta.get("title", "") or url.split("/")[-1].replace("-", " ").title()
        published_at = meta.get("published_at")
        author = meta.get("author", "ProFootballTalk")

        if not title:
            return None

        title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
        slug = f"{_slugify(title)}-{title_hash}"

        # Clean truncated title (PFT often has trailing | ProFootballTalk)
        title = re.sub(r'\s*\|.*$', '', title).strip()
        # Also clean HTML entities in title
        title = title.replace('&#8217;', "'").replace('&amp;', '&')

        article = Article(
            title=title,
            slug=slug,
            body=body,
            excerpt=body[:500],
            category=_guess_category(title, body),
            tier="free",
            published=True,
            published_at=published_at or datetime.now(timezone.utc),
            author=author,
            source_url=url,
            source_name=source_name,
            source_type="pft",
        )

        inserted = await _try_insert_article(db, article)
        if inserted:
            return article.id
        return None

    except Exception as e:
        logger.error(f"Error fetching {url[:80]}: {e}")
        return None


def _guess_category(title: str, body: str) -> str:
    """Guess article category from title and body."""
    text = (title + " " + body).lower()
    if any(w in text for w in ["fantasy", "ppr", "rankings", "waiver"]):
        return "fantasy_advice"
    if any(w in text for w in ["betting", "spread", "over/under", "pick", "prop"]):
        return "betting_pick"
    if any(w in text for w in ["preview", "week ", "matchup", "schedule"]):
        return "game_preview"
    if any(w in text for w in ["recap", "result", "score", "highlights"]):
        return "game_recap"
    if any(w in text for w in ["draft", "rookie", "prospect"]):
        return "fantasy_advice"
    return "news"


async def scrape_from_sitemaps(
    db: AsyncSession,
    start_month: Optional[str] = None,
    max_sitemaps: int = 3,
    max_per_sitemap: Optional[int] = None,
    delay: float = 0.5,
) -> dict:
    """
    Scrape PFT articles from NBC Sports sitemaps, newest first.

    Args:
        start_month: e.g. "202404" — if set, skip sitemaps newer than this
        max_sitemaps: Max sitemaps to process (each ~1750 PFT articles)
        max_per_sitemap: Max articles per sitemap
        delay: Seconds between requests
    """
    stats = {
        "sitemaps_processed": 0,
        "total_urls": 0,
        "new_articles": 0,
        "errors": 0,
    }

    source_name = f"ProFootballTalk"

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get all sitemaps
        sitemaps = await _get_sitemap_list(client)

        # Parse sitemap dates to filter
        sitemap_dates = []
        for s in sitemaps:
            m = re.search(r'sitemap-(\d{6})\.xml$', s)
            if m:
                ym = m.group(1)  # e.g. "202512"
                if start_month and ym > start_month:
                    continue
                sitemap_dates.append((ym, s))

        logger.info(f"Found {len(sitemap_dates)} eligible sitemaps")

        # Process newest first
        sitemap_dates.sort(key=lambda x: x[0], reverse=True)
        sitemap_dates = sitemap_dates[:max_sitemaps]

        for ym, sitemap_url in sitemap_dates:
            logger.info(f"Processing sitemap {ym}: {sitemap_url}")
            urls = await _get_pft_urls(client, sitemap_url, max_urls=max_per_sitemap)
            stats["sitemaps_processed"] += 1
            stats["total_urls"] += len(urls)
            logger.info(f"  Found {len(urls)} PFT articles in {ym}")

            for i, url in enumerate(urls):
                article_id = await _fetch_and_save(db, client, url, source_name)
                if article_id:
                    stats["new_articles"] += 1
                else:
                    stats["errors"] += 1

                if (i + 1) % 20 == 0:
                    await db.commit()
                    logger.info(f"  {ym}: {i+1}/{len(urls)} processed ({stats['new_articles']} new so far)")

                await asyncio.sleep(delay)

            await db.commit()

    logger.info(
        f"PFT archive: {stats['new_articles']} new articles from "
        f"{stats['sitemaps_processed']} sitemaps ({stats['total_urls']} urls)"
    )
    return stats


async def scrape_latest_rss(
    db: AsyncSession,
    max_articles: int = 50,
) -> dict:
    """Scrape the most recent PFT articles from RSS feed (no sitemap needed)."""
    stats = {"new": 0, "errors": 0}

    source_name = "ProFootballTalk"

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(PFT_RSS, headers=HEADERS, follow_redirects=True)
        import feedparser
        feed = feedparser.parse(resp.text)

        entries = feed.entries[:max_articles]
        logger.info(f"PFT RSS: {len(entries)} articles available")

        for entry in entries:
            url = entry.get("link", "").strip()
            if not url:
                continue

            article_id = await _fetch_and_save(db, client, url, source_name)
            if article_id:
                stats["new"] += 1
            else:
                stats["errors"] += 1

            await asyncio.sleep(0.3)

        await db.commit()

    logger.info(f"PFT RSS: {stats['new']} new, {stats['errors']} errors")
    return stats
