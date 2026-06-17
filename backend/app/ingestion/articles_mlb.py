"""
MLB article RSS scraper: RSS feeds → mlb.articles.
Articles are embedded by run_embed_pgvector.py (once we add MLB support).

Mirrors the NFL articles.py but stores in mlb schema.
"""
import hashlib
import html
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

logger = logging.getLogger("earl.articles_mlb")

# ── MLB RSS Feed Sources ──────────────────────────────────────────────────────
RSS_FEEDS_MLB = {
    # ── Major networks ──
    "ESPN": "https://www.espn.com/espn/rss/mlb/news",
    "Yahoo Sports": "https://sports.yahoo.com/mlb/rss",
    "CBS Sports": "https://www.cbssports.com/rss/headlines/mlb/",
    "MLB.com": "https://www.mlb.com/feeds/news/rss.xml",

    # ── Premium analysis ──
    "The Athletic": "https://www.nytimes.com/athletic/rss/mlb/",
    "Fangraphs": "https://blogs.fangraphs.com/feed/",
    "Baseball Prospectus": "https://www.baseballprospectus.com/feed/",
    "MLB Trade Rumors": "https://www.mlbtraderumors.com/feed",
    "Pitcher List": "https://pitcherlist.com/feed/",

    # ── Independent analysis ──
    "Sportsnaut MLB": "https://sportsnaut.com/mlb/feed",
    "Fox Sports": "https://api.foxsports.com/v1/rss?partnerKey=mbc&tag=mlb",
    "FanSided MLB (Call to the Pen)": "https://calltothepen.com/feed/",

    # ── FanSided Team Sites ──
    "FanSided Orioles (Birds Watcher)": "https://birdswatcher.com/feed/",
    "FanSided Red Sox (BoSox Injection)": "https://bosoxinjection.com/feed/",
    "FanSided White Sox (Southside Showdown)": "https://southsideshowdown.com/feed/",
    "FanSided Guardians (Away Back Gone)": "https://awaybackgone.com/feed/",
    "FanSided Tigers (Motor City Bengals)": "https://motorcitybengals.com/feed/",
    "FanSided Astros (Climbing Tal's Hill)": "https://climbingtalshill.com/feed/",
    "FanSided Royals (Kings of Kauffman)": "https://kingsofkauffman.com/feed/",
    "FanSided Angels (Halo Hangout)": "https://halohangout.com/feed/",
    "FanSided Twins (Puckett's Pond)": "https://puckettspond.com/feed/",
    "FanSided Yankees (Yanks Go Yard)": "https://yanksgoyard.com/feed/",
    "FanSided Athletics (White Cleat Beat)": "https://whitecleatbeat.com/feed/",
    "FanSided Mariners (SoDo Mojo)": "https://sodomojo.com/feed/",
    "FanSided Rays (Rays Colored Glasses)": "https://rayscoloredglasses.com/feed/",
    "FanSided Rangers (Nolan Writin')": "https://nolanwritin.com/feed/",
    "FanSided Blue Jays (Jays Journal)": "https://jaysjournal.com/feed/",
    "FanSided D-backs (Venom Strikes)": "https://venomstrikes.com/feed/",
    "FanSided Braves (House That Hank Built)": "https://housethathankbuilt.com/feed/",
    "FanSided Cubs (Cubbies Crib)": "https://cubbiescrib.com/feed/",
    "FanSided Reds (Blog Red Machine)": "https://blogredmachine.com/feed/",
    "FanSided Rockies (Rox Pile)": "https://roxpile.com/feed/",
    "FanSided Dodgers (Dodgers Way)": "https://dodgersway.com/feed/",
    "FanSided Marlins (Marlin Maniac)": "https://marlinmaniac.com/feed/",
    "FanSided Brewers (Reviewing the Brew)": "https://reviewingthebrew.com/feed/",
    "FanSided Mets (Rising Apple)": "https://risingapple.com/feed/",
    "FanSided Phillies (That Ball's Outta Here)": "https://thatballsouttahere.com/feed/",
    "FanSided Pirates (Rum Bunter)": "https://rumbunter.com/feed/",
    "FanSided Padres (Friars on Base)": "https://friarsonbase.com/feed/",
    "FanSided Giants (Around the Foghorn)": "https://aroundthefoghorn.com/feed/",
    "FanSided Cardinals (Redbird Rants)": "https://redbirdrants.com/feed/",
    "FanSided Nationals (District on Deck)": "https://districtondeck.com/feed/",

    # ── SB Nation MLB main ──
    "SB Nation MLB": "https://www.sbnation.com/rss/mlb/index.xml",

    # ── SB Nation AL East ──
    "Orioles (Camden Chat)": "https://www.camdenchat.com/rss/index.xml",
    "Red Sox (Over the Monster)": "https://www.overthemonster.com/rss/index.xml",
    "Yankees (Pinstripe Alley)": "https://www.pinstripealley.com/rss/index.xml",
    "Rays (DRaysBay)": "https://www.draysbay.com/rss/index.xml",
    "Blue Jays (Bluebird Banter)": "https://www.bluebirdbanter.com/rss/index.xml",

    # ── SB Nation AL Central ──
    "White Sox (South Side Sox)": "https://www.southsidesox.com/rss/index.xml",
    "Guardians (Covering the Corner)": "https://www.coveringthecorner.com/rss/index.xml",
    "Tigers (Bless You Boys)": "https://www.blessyouboys.com/rss/index.xml",
    "Royals (Royals Review)": "https://www.royalsreview.com/rss/index.xml",
    "Twins (Twinkie Town)": "https://www.twinkietown.com/rss/index.xml",

    # ── SB Nation AL West ──
    "Astros (Crawfish Boxes)": "https://www.crawfishboxes.com/rss/index.xml",
    "Angels (Halos Heaven)": "https://www.halosheaven.com/rss/index.xml",
    "Athletics (Athletics Nation)": "https://www.athleticsnation.com/rss/index.xml",
    "Mariners (Lookout Landing)": "https://www.lookoutlanding.com/rss/index.xml",
    "Rangers (Lone Star Ball)": "https://www.lonestarball.com/rss/index.xml",

    # ── SB Nation NL East ──
    "Braves (Battery Power)": "https://www.batterypower.com/rss/index.xml",
    "Marlins (Fish Stripes)": "https://www.fishstripes.com/rss/index.xml",
    "Mets (Amazin' Avenue)": "https://www.amazinavenue.com/rss/index.xml",
    "Phillies (The Good Phight)": "https://www.thegoodphight.com/rss/index.xml",
    "Nationals (Federal Baseball)": "https://www.federalbaseball.com/rss/index.xml",

    # ── SB Nation NL Central ──
    "Cubs (Bleed Cubbie Blue)": "https://www.bleedcubbieblue.com/rss/index.xml",
    "Reds (Red Reporter)": "https://www.redreporter.com/rss/index.xml",
    "Brewers (Brew Crew Ball)": "https://www.brewcrewball.com/rss/index.xml",
    "Pirates (Bucs Dugout)": "https://www.bucsdugout.com/rss/index.xml",
    "Cardinals (Viva El Birdos)": "https://www.vivaelbirdos.com/rss/index.xml",

    # ── SB Nation NL West ──
    "D-backs (AZ Snake Pit)": "https://www.azsnakepit.com/rss/index.xml",
    "Rockies (Purple Row)": "https://www.purplerow.com/rss/index.xml",
    "Dodgers (True Blue LA)": "https://www.truebluela.com/rss/index.xml",
    "Padres (Gaslamp Ball)": "https://www.gaslampball.com/rss/index.xml",
    "Giants (McCovey Chronicles)": "https://www.mccoveychronicles.com/rss/index.xml",
}


# ── Helpers (same as NFL version) ─────────────────────────────────────────────

def _slugify(title: str) -> str:
    """Turn a title into a URL-safe slug."""
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\- ]+", " ", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:200]


def _map_category(tags: list, title: str = "", body: str = "") -> str:
    """Map RSS tags/categories to a standardized category."""
    categories = {
        "game recap": "game_recap",
        "game preview": "game_preview",
        "preview": "game_preview",
        "recap": "game_recap",
        "fantasy": "fantasy_advice",
        "fantasy baseball": "fantasy_advice",
        "dfc": "fantasy_advice",
        "dfs": "fantasy_advice",
        "draftkings": "fantasy_advice",
        "fanduel": "fantasy_advice",
        "betting": "betting_pick",
        "pick": "betting_pick",
        "pick'em": "betting_pick",
        "props": "betting_pick",
        "odds": "betting_pick",
        "mlb picks": "betting_pick",
        "analysis": "team_analysis",
        "team analysis": "team_analysis",
        "scouting": "team_analysis",
        "prospect": "team_analysis",
        "prospects": "team_analysis",
        "trade": "news",
        "rumors": "news",
        "injury": "news",
        "signing": "news",
        "transaction": "news",
        "hitting": "fantasy_advice",
        "pitching": "fantasy_advice",
        "pitcher list": "fantasy_advice",
        "sabermetrics": "team_analysis",
        "statcast": "team_analysis",
        "power ranking": "general",
        "playoff": "general",
        "postseason": "general",
    }
    for tag in tags:
        label = tag.get("label", "") or tag.get("term", "") or ""
        label_lower = label.lower().strip()
        for key, value in categories.items():
            if key in label_lower:
                return value
    return "general"


# ── Main scrape function ──────────────────────────────────────────────────────

async def scrape_rss_feeds_mlb(
    db: AsyncSession,
    max_per_feed: int = 20,
    skip_older_than_days: int = 30,
) -> dict:
    """
    Scrape all configured MLB RSS feeds and store articles in mlb.articles.
    Returns stats about what was scraped.
    """
    stats = {
        "feeds_checked": 0,
        "feeds_with_errors": 0,
        "entries_found": 0,
        "articles_scraped": 0,
        "duplicates_skipped": 0,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        for source_name, feed_url in RSS_FEEDS_MLB.items():
            stats["feeds_checked"] += 1

            try:
                resp = await client.get(feed_url, follow_redirects=True)
                if resp.status_code != 200:
                    logger.warning(f"  {source_name}: HTTP {resp.status_code}")
                    stats["feeds_with_errors"] += 1
                    continue

                feed = feedparser.parse(BytesIO(resp.content))
                if not feed.entries:
                    logger.info(f"  {source_name}: 0 entries")
                    continue

                entries = feed.entries[:max_per_feed]
                stats["entries_found"] += len(entries)

                for entry in entries:
                    title = html.unescape(entry.get("title", "").strip())
                    link = entry.get("link", "").strip()

                    if not title or not link:
                        continue

                    # Build unique slug
                    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
                    slug = f"{_slugify(title)}-{title_hash}"

                    # Check for duplicate
                    existing = await db.execute(
                        select(MLBArticle).where(MLBArticle.slug == slug)
                    )
                    if existing.scalar_one_or_none():
                        stats["duplicates_skipped"] += 1
                        continue

                    # Extract body
                    body = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
                    # Clean HTML from body
                    body_text = re.sub(r"<[^>]+>", " ", body)
                    body_text = html.unescape(body_text)
                    body_text = re.sub(r"\s+", " ", body_text).strip()

                    # Get published date
                    pub_struct = entry.get("published_parsed")
                    pub_date = (
                        datetime(*pub_struct[:6], tzinfo=timezone.utc)
                        if pub_struct
                        else datetime.now(timezone.utc)
                    )

                    # Skip old articles
                    days_old = (datetime.now(timezone.utc) - pub_date).days
                    if days_old > skip_older_than_days:
                        continue

                    # Extract author
                    author = None
                    if hasattr(entry, "author"):
                        author = entry.author
                    elif hasattr(entry, "authors") and entry.authors:
                        author = entry.authors[0].get("name")

                    # Map category from tags
                    tags = entry.get("tags", [])
                    category = _map_category(tags, title, body_text)

                    # Excerpt
                    excerpt = body_text[:500]

                    article = MLBArticle(
                        title=title,
                        slug=slug,
                        body=body_text,
                        excerpt=excerpt,
                        category=category,
                        tier="free",
                        published=True,
                        published_at=pub_date,
                        author=author or "MLB Staff",
                        source_url=link,
                        source_name=source_name,
                        source_type="rss",
                    )
                    db.add(article)
                    stats["articles_scraped"] += 1

                await db.commit()
                logger.info(f"  {source_name}: {stats['articles_scraped']} articles (running total)")

            except Exception as e:
                logger.error(f"  {source_name}: error - {e}")
                stats["feeds_with_errors"] += 1
                continue

    return stats
