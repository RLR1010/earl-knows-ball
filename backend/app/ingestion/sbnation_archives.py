"""
SB Nation archive scraper: monthly HTML archives → article URLs → full text → DB + Cognee-NFL.

SB Nation team blogs use Vox Media's Chorus platform. Archives are at:
    /archives/{year}/{month}/{page}

Each archive page embeds article data in __NEXT_DATA__ → props → pageProps → hydration → responses.
Individual article pages embed JSON-LD with the full article body (articleBody).
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
from sqlalchemy import select, func

from app.models import Article
from app.models.nba import NBAArticle
from app.models.mlb import MLBArticle
from app.ingestion.articles import _slugify, _map_category

logger = logging.getLogger("earl.sbnation_archives")

# ── SB Nation blog domains → (display name, team abbreviation) ──────────
SBNATION_BLOGS: dict[str, tuple[str, str]] = {
    # AFC East
    "buffalorumblings":         ("Buffalo Rumblings", "BUF"),
    "thephinsider":             ("The Phinsider", "MIA"),
    "patspulpit":               ("Pats Pulpit", "NE"),
    "ganggreennation":          ("Gang Green Nation", "NYJ"),
    # AFC North
    "baltimorebeatdown":        ("Baltimore Beatdown", "BAL"),
    "cincyjungle":              ("Cincy Jungle", "CIN"),
    "dawgsports":               ("Dawgsports", "CLE"),
    "behindthesteelcurtain":    ("Behind the Steel Curtain", "PIT"),
    # AFC South
    "battleredblog":            ("Battle Red Blog", "HOU"),
    "stampedeblue":             ("Stampede Blue", "IND"),
    "bigcatcountry":            ("Big Cat Country", "JAX"),
    "musiccitymiracles":        ("Music City Miracles", "TEN"),
    # AFC West
    "milehighreport":           ("Mile High Report", "DEN"),
    "arrowheadpride":           ("Arrowhead Pride", "KC"),
    "silverandblackpride":      ("Silver & Black Pride", "LV"),
    "boltsfromtheblue":         ("Bolts from the Blue", "LAC"),
    # NFC East
    "bloggingtheboys":          ("Blogging the Boys", "DAL"),
    "bigblueview":              ("Big Blue View", "NYG"),
    "bleedinggreennation":      ("Bleeding Green Nation", "PHI"),
    "hogshaven":                ("Hogs Haven", "WAS"),
    # NFC North
    "windycitygridiron":        ("Windy City Gridiron", "CHI"),
    "prideofdetroit":           ("Pride of Detroit", "DET"),
    "acmepackingcompany":       ("Acme Packing Company", "GB"),
    "dailynorseman":            ("Daily Norseman", "MIN"),
    # NFC South
    "thefalcoholic":            ("The Falcoholic", "ATL"),
    "catscratchreader":         ("Cat Scratch Reader", "CAR"),
    "canalstreetchronicles":    ("Canal Street Chronicles", "NO"),
    "bucsnation":               ("Bucs Nation", "TB"),
    # NFC West
    "revengeofthebirds":        ("Revenge of the Birds", "ARI"),
    "turfshowtimes":            ("Turf Show Times", "LAR"),
    "ninersnation":             ("Niners Nation", "SF"),
    "fieldgulls":               ("Field Gulls", "SEA"),
}

# ── NBA SB Nation blogs ──────────────────────────────────────────────
NBA_BLOGS: dict[str, tuple[str, str]] = {
    # Atlantic
    "celticsblog":             ("CelticsBlog", "BOS"),
    "netsdaily":               ("Nets Daily", "BKN"),
    "postingandtoasting":      ("Posting and Toasting", "NYK"),
    "libertyballers":          ("Liberty Ballers", "PHI"),
    "raptorshq":               ("Raptors HQ", "TOR"),
    # Central
    "brewhoop":                ("Brew Hoop", "MIL"),
    "fearthesword":            ("Fear the Sword", "CLE"),
    "detroitbadboys":          ("Detroit Bad Boys", "DET"),
    "indycornrows":            ("Indy Cornrows", "IND"),
    # Southeast
    "peachtreehoops":          ("Peachtree Hoops", "ATL"),
    "bulletsforever":          ("Bullets Forever", "WAS"),
    "hothothoops":             ("Hot Hot Hoops", "MIA"),
    # Northwest
    "blazersedge":             ("Blazers Edge", "POR"),
    "canishoopus":             ("Canis Hoopus", "MIN"),
    "welcometoloudcity":       ("Welcome to Loud City", "OKC"),
    "denverstiffs":            ("Denver Stiffs", "DEN"),
    "slcdunk":                 ("SLC Dunk", "UTA"),
    # Pacific
    "goldenstateofmind":       ("Golden State of Mind", "GSW"),
    "silverscreenandroll":     ("Silver Screen and Roll", "LAL"),
    "clipsnation":             ("Clips Nation", "LAC"),
    "brightsideofthesun":      ("Bright Side of the Sun", "PHX"),
    "sactownroyalty":          ("Sactown Royalty", "SAC"),
    # Southwest
    "mavsmoneyball":           ("Mavs Moneyball", "DAL"),
    "thedreamshake":           ("The Dream Shake", "HOU"),
    "grizzlybearblues":        ("Grizzly Bear Blues", "MEM"),
    "thebirdwrites":           ("The Bird Writes", "NOP"),
    "poundingtherock":         ("Pounding the Rock", "SAS"),
    # Spot for Phoenix? Already have PHX above
}

# ── MLB SB Nation blogs ──────────────────────────────────────────────
MLB_BLOGS: dict[str, tuple[str, str]] = {
    # AL East
    "camdenchat":              ("Camden Chat", "BAL"),
    "overthemonster":          ("Over the Monster", "BOS"),
    "pinstripealley":          ("Pinstripe Alley", "NYY"),
    "draysbay":                ("DRaysBay", "TB"),
    "bluebirdbanter":          ("Bluebird Banter", "TOR"),
    # AL Central
    "southsidesox":            ("South Side Sox", "CWS"),
    "coveringthecorner":       ("Covering the Corner", "CLE"),
    "blessyouboys":            ("Bless You Boys", "DET"),
    "royalsreview":            ("Royals Review", "KC"),
    "twinkietown":             ("Twinkie Town", "MIN"),
    # AL West
    "crawfishboxes":           ("Crawfish Boxes", "HOU"),
    "athleticsnation":         ("Athletics Nation", "OAK"),
    "lookoutlanding":          ("Lookout Landing", "SEA"),
    "lonestarball":            ("Lone Star Ball", "TEX"),
    "halosheaven":             ("Halos Heaven", "LAA"),
    # NL East
    "batterypower":            ("Battery Power", "ATL"),
    "fishstripes":             ("Fish Stripes", "MIA"),
    "amazinavenue":            ("Amazin' Avenue", "NYM"),
    "thegoodphight":           ("The Good Phight", "PHI"),
    "federalbaseball":         ("Federal Baseball", "WAS"),
    # NL Central
    "bleedcubbieblue":         ("Bleed Cubbie Blue", "CHC"),
    "redreporter":             ("Red Reporter", "CIN"),
    "brewcrewball":            ("Brew Crew Ball", "MIL"),
    "bucsdugout":              ("Bucs Dugout", "PIT"),
    "vivaelbirdos":            ("Viva El Birdos", "STL"),
    # NL West
    "azsnakepit":              ("AZ Snake Pit", "ARI"),
    "purplerow":               ("Purple Row", "COL"),
    "truebluela":              ("True Blue LA", "LAD"),
    "gaslampball":             ("Gaslamp Ball", "SD"),
    "mccoveychronicles":       ("McCovey Chronicles", "SF"),
}

# ── HTML parsing helpers ──────────────────────────────────────────────

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _extract_next_data(html: str) -> Optional[dict]:
    """Extract and parse the __NEXT_DATA__ JSON from an archive page."""
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def _extract_articles_from_page(html: str, blog_domain: str) -> list[dict]:
    """
    Extract article metadata from an archive page via __NEXT_DATA__.

    Returns list of { url, title, published_at, author, category }.
    """
    data = _extract_next_data(html)
    if not data:
        return []

    try:
        response = data["props"]["pageProps"]["hydration"]["responses"][0]
        resource = response["data"].get("resource", response["data"])
        posts_container = resource.get("posts", {})
    except (KeyError, IndexError, TypeError):
        return []

    # posts can be a dict with 'nodes' (Relay connection) or a list
    if isinstance(posts_container, dict):
        nodes = posts_container.get("nodes", [])
    elif isinstance(posts_container, list):
        nodes = posts_container
    else:
        nodes = []

    articles = []
    for node in nodes:
        if not isinstance(node, dict):
            continue

        permalink = node.get("permalink") or ""
        if not permalink:
            continue

        title = (node.get("title") or "").strip()
        if not title:
            continue

        # Parse published date
        pub_at = node.get("publishedAt") or ""
        pub_date = None
        if pub_at:
            try:
                pub_date = datetime.fromisoformat(pub_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass
        if pub_date is None:
            pub_date = datetime.now(timezone.utc)

        # Extract author name
        author = ""
        authors = node.get("authors") or []
        if isinstance(authors, list) and len(authors) > 0:
            first = authors[0]
            if isinstance(first, dict):
                author = first.get("name", "")
            elif isinstance(first, str):
                author = first

        # Category
        category = ""
        primary_cat = node.get("primaryCategory") or {}
        if isinstance(primary_cat, dict):
            category = primary_cat.get("title", "")
        if not category:
            section_path = permalink.replace(f"https://{_full_domain(blog_domain)}/", "").split("/")[0]
            category = section_path.replace("-", " ").title()

        articles.append({
            "url": permalink,
            "title": title,
            "published_at": pub_date,
            "author": author,
            "category": category,
        })

    return articles


def _extract_article_body(html: str) -> Optional[dict]:
    """
    Extract full article data from JSON-LD (NewsArticle) on the article page.
    Returns { title, body, date_published, author, description } or None.
    """
    match = re.search(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict) or data.get("@type") != "NewsArticle":
        return None

    title = (data.get("headline") or "").strip()
    body = (data.get("articleBody") or "").strip()
    if not title or not body:
        return None

    return {
        "title": title,
        "body": body,
        "date_published": data.get("datePublished") or "",
        "author": data.get("author", ""),
        "description": (data.get("description") or "")[:500],
    }


def _full_domain(blog_domain: str) -> str:
    """Convert a blog subdomain to the full domain (e.g. acmepackingcompany → www.acmepackingcompany.com)."""
    if blog_domain.startswith("www."):
        return blog_domain
    return f"www.{blog_domain}.com"


def _build_source_name(blog_domain: str) -> str:
    """Map a blog domain to a display name for any sport."""
    info = SBNATION_BLOGS.get(blog_domain) or NBA_BLOGS.get(blog_domain) or MLB_BLOGS.get(blog_domain)
    if info:
        return f"SB Nation – {info[0]} ({info[1]})"
    return f"SB Nation – {blog_domain}"


def _category_from_metadata(category_str: str, title: str, body: str) -> str:
    """Derive an article category from archive metadata + content keywords."""
    cat_lower = category_str.lower()
    if any(w in cat_lower for w in ["draft", "fantasy"]):
        return "fantasy_advice"
    if "analysis" in cat_lower or "film" in cat_lower:
        return "analysis"
    if "news" in cat_lower:
        return "news"
    if "free agency" in cat_lower or "salary" in cat_lower or "cap" in cat_lower:
        return "news"
    if "discussion" in cat_lower:
        return "general"
    if "roster" in cat_lower or "depth" in cat_lower:
        return "general"
    if "history" in cat_lower:
        return "general"
    if "coaching" in cat_lower or "coach" in cat_lower:
        return "news"
    # Fallback to keyword matching in title+body
    return _map_category([], title, body)


# ── HTTP helpers ──────────────────────────────────────────────────────

async def _fetch_url(url: str, client: httpx.AsyncClient) -> Optional[str]:
    """Fetch a URL and return its text content, or None on failure."""
    try:
        resp = await client.get(url, follow_redirects=True, timeout=30.0)
        if resp.status_code == 200:
            return resp.text
        logger.warning(f"HTTP {resp.status_code} for {url}")
        return None
    except Exception as e:
        logger.warning(f"Fetch error for {url}: {e}")
        return None


# ── Core scraper ───────────────────────────────────────────────────────

async def scrape_blog_archives(
    db: AsyncSession,
    blog_domain: str,
    start_year: int = 2022,
    end_year: Optional[int] = None,
    delay: float = 1.5,
    embed: bool = True,
    max_articles: Optional[int] = None,
    article_class=Article,
) -> dict:
    """
    Scrape all months from start_year to end_year for a single SB Nation blog.

    Returns stats dict with counts.
    """
    if end_year is None:
        end_year = datetime.now(timezone.utc).year

    today = datetime.now(timezone.utc)
    current_month = today.month
    if end_year < today.year:
        current_month = 12

    source_name = _build_source_name(blog_domain)
    stats = {
        "blog": blog_domain,
        "source_name": source_name,
        "pages_scanned": 0,
        "urls_discovered": 0,
        "urls_skipped_existing": 0,
        "articles_scraped": 0,
        "articles_embedded": 0,
        "errors": 0,
    }

    # Pre-fetch existing source_urls for dedup
    result = await db.execute(
        select(article_class.source_url).where(article_class.source_name == source_name)
    )
    existing_urls = set(row[0] for row in result.fetchall() if row[0])

    http_headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; EarlKnowsBall/1.0; "
            "+https://earlknowsfootball.com)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(headers=http_headers, timeout=30.0) as client:
        year = end_year
        month = current_month

        while year > start_year or (year == start_year and month >= 1):
            if max_articles and stats["articles_scraped"] >= max_articles:
                logger.info(f"  Reached max_articles ({max_articles}), stopping")
                break

            page = 1
            while True:
                if max_articles and stats["articles_scraped"] >= max_articles:
                    break

                archive_url = f"https://{_full_domain(blog_domain)}/archives/{year}/{month:02d}/{page}"
                # Fetch the archive page and check for redirects (307 → month landing page)
                resp = await client.get(archive_url, follow_redirects=True)
                if resp.status_code != 200 or str(resp.url) != archive_url:
                    break  # 404, redirect, or other error → no more valid pages this month

                html = resp.text
                stats["pages_scanned"] += 1

                # Extract article links via __NEXT_DATA__
                articles = _extract_articles_from_page(html, _full_domain(blog_domain))
                if not articles:
                    break  # empty page → month done

                stats["urls_discovered"] += len(articles)

                for entry in articles:
                    if max_articles and stats["articles_scraped"] >= max_articles:
                        break

                    if entry["url"] in existing_urls:
                        stats["urls_skipped_existing"] += 1
                        continue

                    try:
                        await _process_article(
                            db=db,
                            client=client,
                            blog_domain=blog_domain,
                            source_name=source_name,
                            entry=entry,
                            embed=embed,
                            existing_urls=existing_urls,
                            stats=stats,
                            article_class=article_class,
                        )
                    except Exception as e:
                        logger.error(f"Error processing {entry['url']}: {e}")
                        stats["errors"] += 1

                    await asyncio.sleep(delay)

                # Commit after each page so progress persists if interrupted
                try:
                    await db.commit()
                except Exception:
                    await db.rollback()

                page += 1

            month -= 1
            if month < 1:
                month = 12
                year -= 1

            # Also commit after each month (belt and suspenders)
            try:
                await db.commit()
            except Exception:
                await db.rollback()

    logger.info(
        f"  {blog_domain}: {stats['articles_scraped']} articles "
        f"({stats['pages_scanned']} pages)"
    )
    return stats


async def _process_article(
    db: AsyncSession,
    client: httpx.AsyncClient,
    blog_domain: str,
    source_name: str,
    entry: dict,
    embed: bool,
    existing_urls: set,
    stats: dict,
    article_class=Article,
) -> None:
    """Fetch article page, extract JSON-LD body, store in DB, optionally embed."""
    article_html = await _fetch_url(entry["url"], client)
    if not article_html:
        stats["errors"] += 1
        return

    # Extract body from JSON-LD on the article page
    ld = _extract_article_body(article_html)
    body = (ld["body"] if ld else "").strip()
    if not body:
        # Fallback: use the archive metadata (title + excerpt)
        logger.warning(f"No JSON-LD body at {entry['url']}")
        # Try to extract from raw HTML content
        body_match = re.search(
            r'<div[^>]*class="[^"]*c-entry-content[^"]*"[^>]*>(.*?)</div>',
            article_html, re.DOTALL,
        )
        if body_match:
            body_text = re.sub(r"<[^>]+>", " ", body_match.group(1))
            body_text = re.sub(r"\s+", " ", body_text).strip()
            body = body_text
        else:
            stats["errors"] += 1
            return

    title = entry["title"]
    pub_date = entry["published_at"]
    author = entry["author"]
    metadata_category = entry["category"]
    url = entry["url"]
    excerpt = body[:500]

    # Determine category
    category = _category_from_metadata(metadata_category, title, body)

    # Build unique slug
    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
    slug = f"{_slugify(title)}-{title_hash}"

    article = article_class(
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
        source_name=source_name,
        source_type="sbnation",
    )

    try:
        # Use a savepoint so a single duplicate doesn't roll back previous inserts
        async with db.begin_nested():
            db.add(article)
            await db.flush()
        existing_urls.add(url)
        stats["articles_scraped"] += 1
    except Exception:
        # Duplicate — skip without rolling back the outer transaction
        existing_urls.add(url)
        stats["urls_skipped_existing"] += 1
        return



    if stats["articles_scraped"] % 10 == 0:
        logger.info(f"  {blog_domain}: {stats['articles_scraped']} articles so far...")


async def scrape_blog_year(
    db: AsyncSession,
    blog_domain: str,
    year: int,
    delay: float = 1.5,
    embed: bool = True,
    max_articles: Optional[int] = None,
    start_month: Optional[int] = None,
    article_class=Article,
) -> dict:
    """
    Scrape a single year of archives for one SB Nation blog.
    Goes month-by-month within the year, newest month first.
    Returns per-blog-year stats dict.
    """
    source_name = _build_source_name(blog_domain)
    today = datetime.now(timezone.utc)
    current_month = today.month

    # For the current year, start from this month; for past years, December
    end_month = start_month if start_month is not None else (current_month if year == today.year else 12)

    stats = {
        "blog": blog_domain,
        "year": year,
        "source_name": source_name,
        "pages_scanned": 0,
        "urls_discovered": 0,
        "urls_skipped_existing": 0,
        "articles_scraped": 0,
        "articles_embedded": 0,
        "errors": 0,
    }

    # Pre-fetch existing source_urls for dedup (once per blog-year)
    result = await db.execute(
        select(article_class.source_url).where(article_class.source_name == source_name)
    )
    existing_urls = set(row[0] for row in result.fetchall() if row[0])

    # Skip this year if we already have substantial coverage from RSS/prior runs
    year_count = await db.scalar(
        select(func.count()).select_from(article_class).where(
            article_class.source_name == source_name,
            func.extract('year', article_class.published_at) == year,
        )
    )
    if year_count and year_count >= 30:
        logger.info(f"  {blog_domain} ({year}): {year_count} articles already exist, skipping")
        return stats

    http_headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; EarlKnowsBall/1.0; "
            "+https://earlknowsfootball.com)"
        ),
        "Accept": "text/html,application/xhtml+xml",
    }

    async with httpx.AsyncClient(headers=http_headers, timeout=30.0) as client:
        for month in range(end_month, 0, -1):
            if max_articles and stats["articles_scraped"] >= max_articles:
                break

            page = 1
            while True:
                if max_articles and stats["articles_scraped"] >= max_articles:
                    break

                archive_url = f"https://{_full_domain(blog_domain)}/archives/{year}/{month:02d}/{page}"
                resp = await client.get(archive_url, follow_redirects=True)
                if resp.status_code != 200 or str(resp.url) != archive_url:
                    break  # 404 or redirect → no more pages this month

                html = resp.text
                stats["pages_scanned"] += 1

                articles = _extract_articles_from_page(html, _full_domain(blog_domain))
                if not articles:
                    break  # empty page → month done

                stats["urls_discovered"] += len(articles)

                for entry in articles:
                    if max_articles and stats["articles_scraped"] >= max_articles:
                        break

                    if entry["url"] in existing_urls:
                        stats["urls_skipped_existing"] += 1
                        continue

                    try:
                        await _process_article(
                            db=db,
                            client=client,
                            blog_domain=blog_domain,
                            source_name=source_name,
                            entry=entry,
                            embed=embed,
                            existing_urls=existing_urls,
                            stats=stats,
                            article_class=article_class,
                        )
                    except Exception as e:
                        logger.error(f"Error processing {entry['url']}: {e}")
                        stats["errors"] += 1

                    await asyncio.sleep(delay)

                try:
                    await db.commit()
                except Exception:
                    await db.rollback()

                page += 1

            try:
                await db.commit()
            except Exception:
                await db.rollback()

    if stats["articles_scraped"] > 0:
        logger.info(f"  {blog_domain} ({year}): {stats['articles_scraped']} articles ({stats['pages_scanned']} pages)")
    else:
        logger.debug(f"  {blog_domain} ({year}): 0 new articles")
    return stats


async def scrape_all_blogs(
    db: AsyncSession,
    start_year: int = 2022,
    end_year: Optional[int] = None,
    delay: float = 1.5,
    embed: bool = True,
    max_per_blog: Optional[int] = None,
    blog_filter: Optional[list[str]] = None,
    blogs: Optional[dict] = None,
) -> dict:
    """
    Scrape archives for all SB Nation team blogs, one year at a time.
    Iterates years newest-to-oldest, processing ALL blogs for each year
    before moving to the previous year.

    Accepts optional `blogs` dict (defaults to SBNATION_BLOGS for NFL).
    Returns a combined summary.
    """
    if end_year is None:
        end_year = datetime.now(timezone.utc).year

    if start_year > end_year:
        logger.warning(f"start_year ({start_year}) > end_year ({end_year}), nothing to scrape")
        return {"blogs": {}, "total_scraped": 0, "total_embedded": 0, "errors": 0}

    blog_dict = blogs if blogs is not None else SBNATION_BLOGS

    # Determine which article model to use based on blog dict
    if blogs is NBA_BLOGS:
        article_class = NBAArticle
    elif blogs is MLB_BLOGS:
        article_class = MLBArticle
    else:
        article_class = Article

    results = {
        "blogs": {},
        "total_scraped": 0,
        "total_embedded": 0,
        "errors": 0,
    }

    today = datetime.now(timezone.utc)

    # ── Year-by-year outer loop: newest year first ──
    for year in range(end_year, start_year - 1, -1):
        logger.info(f"\n{'='*60}\n📅 YEAR {year} — scanning all blogs\n{'='*60}")

        for blog_domain in blog_dict:
            if blog_filter and blog_domain not in blog_filter:
                continue

            try:
                year_stats = await scrape_blog_year(
                    db=db,
                    blog_domain=blog_domain,
                    year=year,
                    delay=delay,
                    embed=embed,
                    max_articles=max_per_blog,
                    article_class=article_class,
                )

                # Accumulate into blog-level results
                if blog_domain not in results["blogs"]:
                    results["blogs"][blog_domain] = {
                        "blog": blog_domain,
                        "source_name": year_stats["source_name"],
                        "pages_scanned": 0,
                        "urls_discovered": 0,
                        "urls_skipped_existing": 0,
                        "articles_scraped": 0,
                        "articles_embedded": 0,
                        "errors": 0,
                    }
                blog_acc = results["blogs"][blog_domain]
                blog_acc["pages_scanned"] += year_stats["pages_scanned"]
                blog_acc["urls_discovered"] += year_stats["urls_discovered"]
                blog_acc["urls_skipped_existing"] += year_stats["urls_skipped_existing"]
                blog_acc["articles_scraped"] += year_stats["articles_scraped"]
                blog_acc["articles_embedded"] += year_stats["articles_embedded"]
                blog_acc["errors"] += year_stats["errors"]

                results["total_scraped"] += year_stats["articles_scraped"]
                results["total_embedded"] += year_stats["articles_embedded"]
                results["errors"] += year_stats["errors"]

            except Exception as e:
                logger.error(f"Failed to scrape {blog_domain} ({year}): {e}")
                results["errors"] += 1

        logger.info(f"📅 Year {year} done. Running total: {results['total_scraped']} new articles\n")

    return results
