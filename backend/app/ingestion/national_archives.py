"""
National NFL news source scraper: paginated archives → article URLs → full text → DB + Cognee-NFL.

Targets sites with paginated article lists (newest → oldest).

Supported sources:
  - Last Word on Sports (lastwordonsports.com/nfl/): WordPress /page/N/ pagination
"""
import hashlib
import json
import logging
import re
import asyncio
from datetime import datetime, timezone
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import Article
from app.ingestion.articles import _slugify, _map_category

logger = logging.getLogger("earl.national_archives")

# ── Source definitions ────────────────────────────────────────────────

NATIONAL_SOURCES: dict[str, dict] = {
    "lastwordonsports": {
        "name": "Last Word on Sports",
        "base_url": "https://lastwordonsports.com/nfl",
        "start_year": 2016,
        "article_link_pattern": r'href="(https://lastwordonsports\.com/nfl/\d{4}/\d{2}/\d{2}/[^"]+)"',
    },
}


def _extract_article_urls(html: str, pattern: str, base_domain: str) -> list[str]:
    """Extract unique article URLs from an archive page."""
    urls = re.findall(pattern, html)
    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _extract_article_body(html: str) -> Optional[str]:
    """Extract article body from an article page using content div patterns."""
    # Try common WordPress content containers — broad class matching
    patterns = [
        r'<div class="[^"]*entry-content[^"]*"[^>]*>(.*?)</div>\s*</article>',
        r'<div class="[^"]*entry-content[^"]*"[^>]*>(.*?)</div>',
        r'<div class="[^"]*post-content[^"]*"[^>]*>(.*?)</div>',
        r'<div class="[^"]*article-content[^"]*"[^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
    ]
    
    for pat in patterns:
        matches = list(re.finditer(pat, html, re.DOTALL))
        for match in matches:
            content = match.group(1)
            # Strip HTML tags
            text = re.sub(r'<[^>]+>', ' ', content)
            text = re.sub(r'&[a-z]+;', ' ', text)
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) > 200:  # meaningful content
                return text
    
    return None


def _extract_metadata(html: str) -> dict:
    """Extract metadata from an article page."""
    meta = {"title": "", "author": "", "date": ""}
    
    # Title from Open Graph or title tag
    m = re.search(r'<meta[^>]*property="og:title"[^>]*content="([^"]+)"', html)
    if m:
        meta["title"] = m.group(1)
    if not meta["title"]:
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.DOTALL)
        if m:
            meta["title"] = m.group(1).split(" | ")[0].split(" - ")[0].strip()
    
    # Date from article:published_time or time tag
    m = re.search(r'<meta[^>]*property="article:published_time"[^>]*content="([^"]+)"', html)
    if m:
        meta["date"] = m.group(1)
    if not meta["date"]:
        m = re.search(r'<time[^>]*datetime="([^"]+)"', html)
        if m:
            meta["date"] = m.group(1)
    
    return meta


async def scrape_source(
    db: AsyncSession,
    source_key: str,
    max_articles: Optional[int] = None,
    delay: float = 1.0,
) -> dict:
    """
    Scrape a national news source from newest to oldest using month-by-month archives.
    """
    source = NATIONAL_SOURCES.get(source_key)
    if not source:
        return {"error": f"Unknown source: {source_key}"}
    
    source_name = source["name"]
    stats = {
        "source": source_key,
        "source_name": source_name,
        "months_scanned": 0,
        "urls_discovered": 0,
        "urls_skipped_existing": 0,
        "articles_scraped": 0,
        "articles_embedded": 0,
        "errors": 0,
    }
    
    # Pre-fetch existing URLs for dedup
    r = await db.execute(
        select(Article.source_url).where(Article.source_name.ilike(f"%{source_name}%"))
    )
    existing_urls = set(row[0] for row in r.fetchall() if row[0])
    
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; EarlKnowsBall/1.0; "
            "+https://earlknowsfootball.com)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }
    
    pattern = source["article_link_pattern"]
    base_url = source["base_url"]
    
    # Walk months from current month down to start_year
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    start_year = source.get("start_year", 2016)
    
    async with httpx.AsyncClient(headers=headers, timeout=30.0, follow_redirects=True) as client:
        while year >= start_year:
            if max_articles and stats["articles_scraped"] >= max_articles:
                break
            
            # Fetch ALL pages for this month (page 1, 2, 3, ...)
            month_page = 1
            while True:
                if max_articles and stats["articles_scraped"] >= max_articles:
                    break
                
                if month_page == 1:
                    archive_url = f"{base_url}/{year}/{month:02d}/"
                else:
                    archive_url = f"{base_url}/{year}/{month:02d}/page/{month_page}/"
                
                stats["months_scanned"] += 1
                
                try:
                    resp = await client.get(archive_url)
                    if resp.status_code != 200:
                        break
                    html = resp.text
                except Exception:
                    break
                
                article_urls = _extract_article_urls(html, pattern, source_key)
                if not article_urls:
                    break
                
                # Deduplicate within page
                seen = set()
                unique_urls = []
                for u in article_urls:
                    if u not in seen:
                        seen.add(u)
                        unique_urls.append(u)
                
                stats["urls_discovered"] += len(unique_urls)
                
                for url in unique_urls:
                    if max_articles and stats["articles_scraped"] >= max_articles:
                        break
                    if url in existing_urls:
                        stats["urls_skipped_existing"] += 1
                        continue
                    try:
                        await _process_article(
                            db=db, client=client, url=url,
                            source_name=source_name, source_key=source_key,
                            embed=embed, existing_urls=existing_urls, stats=stats,
                        )
                    except Exception as e:
                        logger.error(f"Error processing {url}: {e}")
                        stats["errors"] += 1
                    await asyncio.sleep(delay)
                
                month_page += 1
            
            # Commit after each month
            try:
                await db.commit()
            except Exception:
                await db.rollback()
            
            month -= 1
            if month < 1:
                month = 12
                year -= 1
    
    logger.info(
        f"  {source_name}: {stats['articles_scraped']} articles "
        f"({stats['months_scanned']} months)"
    )
    return stats


async def _process_article(
    db: AsyncSession,
    client: httpx.AsyncClient,
    url: str,
    source_name: str,
    source_key: str,
    embed: bool,
    existing_urls: set,
    stats: dict,
) -> None:
    """Fetch article page, extract content, store in DB, optionally embed."""
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            stats["errors"] += 1
            return
        html = resp.text
    except Exception as e:
        logger.warning(f"Fetch error for {url}: {e}")
        stats["errors"] += 1
        return
    
    # Extract metadata
    meta = _extract_metadata(html)
    title = meta["title"] or url.rsplit("/", 1)[-1].replace("-", " ").title()
    
    # Extract body
    body = _extract_article_body(html)
    if not body or len(body) < 50:
        stats["errors"] += 1
        return
    
    # Parse date
    pub_date = None
    if meta["date"]:
        try:
            pub_date = datetime.fromisoformat(meta["date"].replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    if pub_date is None:
        pub_date = datetime.now(timezone.utc)
    
    # Category
    category = _map_category([], title, body)
    excerpt = body[:500]
    author = meta.get("author", "")
    
    # Slug
    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
    slug = f"{_slugify(title)}-{title_hash}"
    
    article = Article(
        title=title,
        slug=slug,
        body=body,
        excerpt=excerpt,
        category=category,
        tier="free",
        published=True,
        published_at=pub_date,
        author=author,
        source_url=url,
        source_name=f"National – {source_name}",
        source_type="national",
    )
    
    try:
        async with db.begin_nested():
            db.add(article)
            await db.flush()
        existing_urls.add(url)
        stats["articles_scraped"] += 1
    except Exception:
        existing_urls.add(url)
        stats["urls_skipped_existing"] += 1
        return
    

    
    if stats["articles_scraped"] % 10 == 0:
        logger.info(f"  {source_name}: {stats['articles_scraped']} articles so far...")


async def scrape_all_sources(
    db: AsyncSession,
    max_per_source: Optional[int] = None,
    delay: float = 1.0,
    source_filter: Optional[list[str]] = None,
) -> dict:
    """Scrape all (or filtered) national news sources."""
    results = {"sources": {}, "total_scraped": 0, "total_embedded": 0, "errors": 0}
    
    for source_key in NATIONAL_SOURCES:
        if source_filter and source_key not in source_filter:
            continue
        logger.info(f"\n{'='*60}\nScraping {source_key}...\n{'='*60}")
        try:
            src_stats = await scrape_source(
                db=db, source_key=source_key,
                max_articles=max_per_source, delay=delay, embed=embed,
            )
            results["sources"][source_key] = src_stats
            results["total_scraped"] += src_stats.get("articles_scraped", 0)
            results["total_embedded"] += src_stats.get("articles_embedded", 0)
            results["errors"] += src_stats.get("errors", 0)
        except Exception as e:
            logger.error(f"Failed to scrape {source_key}: {e}")
            results["sources"][source_key] = {"error": str(e)}
            results["errors"] += 1
        
        try:
            await db.commit()
        except Exception:
            await db.rollback()
    
    return results
