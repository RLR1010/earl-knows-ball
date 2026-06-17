"""
NFL article scraper: RSS feeds → PostgreSQL.
Articles are embedded separately by run_embed_pgvector.py.
"""
import hashlib
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional

import feedparser
import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article
from app.core.config import settings

logger = logging.getLogger("earl.articles")

# ── RSS Feed Sources ──────────────────────────────────────────────────────────
RSS_FEEDS = {
    # ── Major networks ──
    "ESPN": "https://www.espn.com/espn/rss/nfl/news",
    "Yahoo Sports": "https://sports.yahoo.com/nfl/rss",
    "The Athletic": "https://www.nytimes.com/athletic/rss/nfl/",
    "SB Nation": "https://www.sbnation.com/rss/nfl/index.xml",

    # ── Independent analysis ──
    "NFL Spin Zone": "https://nflspinzone.com/feed",
    "Last Word on Sports": "https://lastwordonsports.com/nfl/feed",
    "ProFootballTalk": "https://www.nbcsports.com/profootballtalk.rss",
    "Sportsnaut": "https://www.sportsnaut.com/feed",
    "Fox Sports": "https://api.foxsports.com/v1/rss?partnerKey=mbc&tag=nfl",

    # ── FanSided Team Sites ──
    "FanSided Ravens (Ebony Bird)": "https://ebonybird.com/feed/",
    "FanSided Bills (BuffaLowDown)": "https://buffalowdown.com/feed/",
    "FanSided Bengals (Stripe Hype)": "https://stripehype.com/feed/",
    "FanSided Browns (Dawg Pound Daily)": "https://dawgpounddaily.com/feed/",
    "FanSided Broncos (Predominantly Orange)": "https://predominantlyorange.com/feed/",
    "FanSided Texans (Toro Times)": "https://torotimes.com/feed/",
    "FanSided Colts (Horseshoe Heroes)": "https://horseshoeheroes.com/feed/",
    "FanSided Jaguars (Black and Teal)": "https://blackandteal.com/feed/",
    "FanSided Chiefs (Arrowhead Addict)": "https://arrowheadaddict.com/feed/",
    "FanSided Raiders (Just Blog Baby)": "https://justblogbaby.com/feed/",
    "FanSided Chargers (Bolt Beat)": "https://boltbeat.com/feed/",
    "FanSided Dolphins (Phin Phanatic)": "https://phinphanatic.com/feed/",
    "FanSided Patriots (Musket Fire)": "https://musketfire.com/feed/",
    "FanSided Jets (The Jet Press)": "https://thejetpress.com/feed/",
    "FanSided Steelers (Still Curtain)": "https://stillcurtain.com/feed/",
    "FanSided Titans (Titan Sized)": "https://titansized.com/feed/",
    "FanSided Cardinals (Raising Zona)": "https://raisingzona.com/feed/",
    "FanSided Falcons (Blogging Dirty)": "https://bloggingdirty.com/feed/",
    "FanSided Panthers (Cat Crave)": "https://catcrave.com/feed/",
    "FanSided Bears (Bear Goggles On)": "https://beargoggleson.com/feed/",
    "FanSided Cowboys (The Landry Hat)": "https://thelandryhat.com/feed/",
    "FanSided Lions (SideLion Report)": "https://sidelionreport.com/feed/",
    "FanSided Packers (Lombardi Ave)": "https://lombardiave.com/feed/",
    "FanSided Rams (Ramblin' Fan)": "https://ramblinfan.com/feed/",
    "FanSided Vikings (The Viking Age)": "https://thevikingage.com/feed/",
    "FanSided Saints (Who Dat Dish)": "https://whodatdish.com/feed/",
    "FanSided Giants (GMEN HQ)": "https://gmenhq.com/feed/",
    "FanSided Eagles (Inside the Iggles)": "https://insidetheiggles.com/feed/",
    "FanSided 49ers (Niner Noise)": "https://ninernoise.com/feed/",
    "FanSided Seahawks (12th Man Rising)": "https://12thmanrising.com/feed/",
    "FanSided Buccaneers (The Pewter Plank)": "https://thepewterplank.com/feed/",
    "FanSided Commanders (Riggo's Rag)": "https://riggosrag.com/feed/",

    # ── SB Nation AFC East ──
    "Bills (Buffalo Rumblings)": "https://www.buffalorumblings.com/rss/index.xml",
    "Dolphins (The Phinsider)": "https://www.thephinsider.com/rss/index.xml",
    "Patriots (Pats Pulpit)": "https://www.patspulpit.com/rss/index.xml",
    "Jets (Gang Green Nation)": "https://www.ganggreennation.com/rss/index.xml",

    # ── SB Nation AFC North ──
    "Ravens (Baltimore Beatdown)": "https://www.baltimorebeatdown.com/rss/index.xml",
    "Bengals (Cincy Jungle)": "https://www.cincyjungle.com/rss/index.xml",
    "Browns (Dawgsports)": "https://www.dawgsports.com/rss/index.xml",
    "Steelers (Behind the Steel Curtain)": "https://www.behindthesteelcurtain.com/rss/index.xml",

    # ── SB Nation AFC South ──
    "Texans (Battle Red Blog)": "https://www.battleredblog.com/rss/index.xml",
    "Colts (Stampede Blue)": "https://www.stampedeblue.com/rss/index.xml",
    "Jaguars (Big Cat Country)": "https://www.bigcatcountry.com/rss/index.xml",
    "Titans (Music City Miracles)": "https://www.musiccitymiracles.com/rss/index.xml",

    # ── SB Nation AFC West ──
    "Broncos (Mile High Report)": "https://www.milehighreport.com/rss/index.xml",
    "Chiefs (Arrowhead Pride)": "https://www.arrowheadpride.com/rss/index.xml",
    "Raiders (Silver & Black Pride)": "https://www.silverandblackpride.com/rss/index.xml",
    "Chargers (Bolts from the Blue)": "https://www.boltsfromtheblue.com/rss/index.xml",

    # ── SB Nation NFC East ──
    "Cowboys (Blogging the Boys)": "https://www.bloggingtheboys.com/rss/index.xml",
    "Giants (Big Blue View)": "https://www.bigblueview.com/rss/index.xml",
    "Eagles (Bleeding Green Nation)": "https://www.bleedinggreennation.com/rss/index.xml",
    "Commanders (Hogs Haven)": "https://www.hogshaven.com/rss/index.xml",

    # ── SB Nation NFC North ──
    "Bears (Windy City Gridiron)": "https://www.windycitygridiron.com/rss/index.xml",
    "Lions (Pride of Detroit)": "https://www.prideofdetroit.com/rss/index.xml",
    "Packers (Acme Packing Company)": "https://www.acmepackingcompany.com/rss/index.xml",
    "Vikings (Daily Norseman)": "https://www.dailynorseman.com/rss/index.xml",

    # ── SB Nation NFC South ──
    "Falcons (The Falcoholic)": "https://www.thefalcoholic.com/rss/index.xml",
    "Panthers (Cat Scratch Reader)": "https://www.catscratchreader.com/rss/index.xml",
    "Saints (Canal Street Chronicles)": "https://www.canalstreetchronicles.com/rss/index.xml",
    "Buccaneers (Bucs Nation)": "https://www.bucsnation.com/rss/index.xml",

    # ── SB Nation NFC West ──
    "Cardinals (Revenge of the Birds)": "https://www.revengeofthebirds.com/rss/index.xml",
    "Rams (Turf Show Times)": "https://www.turfshowtimes.com/rss/index.xml",
    "49ers (Niners Nation)": "https://www.ninersnation.com/rss/index.xml",
    "Seahawks (Field Gulls)": "https://www.fieldgulls.com/rss/index.xml",
}

ARTICLE_AGE_LIMIT_DAYS = 30


def _slugify(title: str) -> str:
    """Generate a URL-safe slug from a title."""
    slug = title.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug)
    return slug[:200]


def _clean_html(raw: str) -> str:
    """Strip HTML tags and decode entities from RSS descriptions."""
    import html
    cleaned = re.sub(r"<[^>]+>", " ", raw)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _extract_body(entry) -> str:
    """Extract the best available body text from an RSS entry."""
    if hasattr(entry, "content") and entry.content:
        return _clean_html(entry.content[0].get("value", ""))
    if hasattr(entry, "summary") and entry.summary:
        return _clean_html(entry.summary)
    if hasattr(entry, "description") and entry.description:
        return _clean_html(entry.description)
    return ""


def _extract_categories(entry) -> list[str]:
    """Extract topic categories from an RSS entry."""
    cats = []
    if hasattr(entry, "tags"):
        for tag in entry.tags:
            term = tag.get("term", "").lower().strip()
            if term:
                cats.append(term)
    return cats


def _map_category(rss_cats: list[str], title: str, body: str) -> str:
    """Map RSS tags to our category system."""
    text = (title + " " + body).lower()
    if any(w in text for w in ["fantasy", "ppr", "rankings", "waiver"]):
        return "fantasy_advice"
    if any(w in text for w in ["betting", "spread", "over/under", "pick", "prop"]):
        return "betting_pick"
    if any(w in text for w in ["preview", "week ", "matchup"]):
        return "game_preview"
    if any(w in text for w in ["recap", "result", "score", "highlights"]):
        return "game_recap"
    if any(w in text for w in ["draft", "rookie", "prospect"]):
        return "fantasy_advice"
    if any(w in text for w in ["injury", "cart", "out for"]):
        return "general"
    for cat in rss_cats:
        if cat in ("fantasy", "fantasy football"):
            return "fantasy_advice"
        if cat in ("betting", "odds"):
            return "betting_pick"
        if cat in ("injury",):
            return "general"
    return "news"


async def _try_insert_article(db: AsyncSession, article: Article) -> bool:
    """
    Try to insert an article. Returns True on success, False on duplicate.
    Uses a savepoint so failures don't corrupt the outer transaction.
    """
    try:
        db.add(article)
        await db.flush()
        return True
    except Exception:
        await db.rollback()
        return False


# ── Public API ────────────────────────────────────────────────────────────────

async def scrape_rss_feeds(
    db: AsyncSession,
    max_per_feed: int = 20,
    skip_older_than_days: Optional[int] = None,
) -> dict:
    """
    Scrape all configured RSS feeds for NFL articles.

    Articles are stored in the database; the pgvector embedder
    (run_embed_pgvector.py) handles embeddings separately.
    """
    if skip_older_than_days is None:
        skip_older_than_days = ARTICLE_AGE_LIMIT_DAYS

    results = {"feeds": {}, "total_scraped": 0, "total_new": 0}

    for source_name, feed_url in RSS_FEEDS.items():
        feed_results = {"fetched": 0, "new": 0, "skipped": 0, "errors": 0}
        try:
            logger.info(f"Fetching RSS: {source_name} → {feed_url}")
            try:
                async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as rss_client:
                    rss_resp = await rss_client.get(feed_url)
                    feed = feedparser.parse(rss_resp.text)
            except Exception:
                feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                feed_results["errors"] = 1
                results["feeds"][source_name] = feed_results
                logger.warning(f"RSS parse error for {source_name}: {feed.bozo_exception}")
                continue

            entries = feed.entries[:max_per_feed]
            feed_results["fetched"] = len(entries)

            for entry in entries:
                try:
                    pub_date = None
                    if hasattr(entry, "published_parsed") and entry.published_parsed:
                        pub_date = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                    elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                        pub_date = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                    if pub_date and skip_older_than_days:
                        age = (datetime.now(timezone.utc) - pub_date).days
                        if age > skip_older_than_days:
                            feed_results["skipped"] += 1
                            continue

                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    body = _extract_body(entry)
                    excerpt = entry.get("summary", "")[:500] if hasattr(entry, "summary") else body[:500]
                    author = ""
                    if hasattr(entry, "author"):
                        author = entry.author
                    elif hasattr(entry, "authors") and entry.authors:
                        author = entry.authors[0].get("name", "")
                    rss_cats = _extract_categories(entry)
                    category = _map_category(rss_cats, title, body)

                    if not title or not link:
                        feed_results["skipped"] += 1
                        continue

                    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
                    slug = f"{_slugify(title)}-{title_hash}"

                    article = Article(
                        title=title,
                        slug=slug,
                        body=body or excerpt,
                        excerpt=excerpt,
                        category=category,
                        tier="free",
                        published=True,
                        published_at=pub_date or datetime.now(timezone.utc),
                        author=author or source_name,
                        source_url=link,
                        source_name=source_name,
                        source_type="rss",
                    )

                    inserted = await _try_insert_article(db, article)
                    if not inserted:
                        feed_results["skipped"] += 1
                        continue

                    feed_results["new"] += 1

                except Exception as e:
                    logger.error(f"Error processing article '{entry.get('title','?')}': {e}")
                    feed_results["errors"] += 1
                    await db.rollback()
                    continue

        except Exception as e:
            logger.error(f"Error fetching {source_name}: {e}")
            feed_results["errors"] = 1

        try:
            await db.commit()
        except Exception:
            await db.rollback()

        results["feeds"][source_name] = feed_results
        results["total_scraped"] += feed_results["fetched"]
        results["total_new"] += feed_results["new"]

    return results


async def add_article_manually(
    db: AsyncSession,
    title: str,
    body: str,
    category: str = "general",
    excerpt: Optional[str] = None,
    author: Optional[str] = None,
    source_name: Optional[str] = None,
    source_url: Optional[str] = None,
    tier: str = "free",
) -> Article:
    """Add a single article manually (for API or admin use)."""
    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
    slug = f"{_slugify(title)}-{title_hash}"

    article = Article(
        title=title,
        slug=slug,
        body=body,
        excerpt=excerpt or body[:500],
        category=category,
        tier=tier,
        published=True,
        published_at=datetime.now(timezone.utc),
        author=author or "Earl Knows Ball",
        source_url=source_url,
        source_name=source_name or "Manual",
        source_type="manual" if not source_url else "api",
    )
    db.add(article)
    await db.flush()
    await db.commit()
    return article
