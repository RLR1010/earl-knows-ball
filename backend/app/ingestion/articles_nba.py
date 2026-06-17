"""
NBA article RSS scraper: RSS feeds → nba.articles.
Mirrors the MLB/pattern but stores in nba schema.
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

from app.models.nba import NBAArticle

logger = logging.getLogger("earl.articles_nba")

RSS_FEEDS_NBA = {
    # Major networks
    "ESPN": "https://www.espn.com/espn/rss/nba/news",
    "Yahoo Sports": "https://sports.yahoo.com/nba/rss/",
    "CBS Sports": "https://www.cbssports.com/rss/headlines/nba/",
    "NBC Sports": "https://nba.nbcsports.com/feed/",
    "The Athletic": "https://www.nytimes.com/athletic/rss/nba/",

    # Analysis + news
    "ClutchPoints": "https://clutchpoints.com/feed",

    # Independent analysis
    "Sportsnaut NBA": "https://sportsnaut.com/nba/feed",
    "BasketballNews.com": "https://www.basketballnews.com/feed/",
    "Fox Sports": "https://api.foxsports.com/v1/rss?partnerKey=mbc&tag=nba",
    "FanSided NBA (Hoops Habit)": "https://hoopshabit.com/feed/",

    # ── FanSided Team Sites ──
    "FanSided Hawks (Soaring Down South)": "https://soaringdownsouth.com/feed/",
    "FanSided Celtics (Hardwood Houdini)": "https://hardwoodhoudini.com/feed/",
    "FanSided Hornets (Swarm and Sting)": "https://swarmandsting.com/feed/",
    "FanSided Bulls (Pippen Ain't Easy)": "https://pippenainteasy.com/feed/",
    "FanSided Cavaliers (King James Gospel)": "https://kingjamesgospel.com/feed/",
    "FanSided Pistons (PistonPowered)": "https://pistonpowered.com/feed/",
    "FanSided Pacers (8 Points, 9 Seconds)": "https://8points9seconds.com/feed/",
    "FanSided Heat (All U Can Heat)": "https://allucanheat.com/feed/",
    "FanSided Bucks (Behind the Buck Pass)": "https://behindthebuckpass.com/feed/",
    "FanSided Knicks (Daily Knicks)": "https://dailyknicks.com/feed/",
    "FanSided Magic (Orlando Magic Daily)": "https://orlandomagicdaily.com/feed/",
    "FanSided 76ers (The Sixer Sense)": "https://thesixersense.com/feed/",
    "FanSided Raptors (Raptors Rapture)": "https://raptorsrapture.com/feed/",
    "FanSided Wizards (Wiz of Awes)": "https://wizofawes.com/feed/",
    "FanSided Mavericks (The Smoking Cuban)": "https://thesmokingcuban.com/feed/",
    "FanSided Nuggets (Nugg Love)": "https://nugglove.com/feed/",
    "FanSided Warriors (Blue Man Hoop)": "https://bluemanhoop.com/feed/",
    "FanSided Rockets (Space City Scoop)": "https://spacecityscoop.com/feed/",
    "FanSided Clippers (Clipperholics)": "https://clipperholics.com/feed/",
    "FanSided Lakers (Lake Show Life)": "https://lakeshowlife.com/feed/",
    "FanSided Grizzlies (Beale Street Bears)": "https://bealestreetbears.com/feed/",
    "FanSided Timberwolves (Dunking with Wolves)": "https://dunkingwithwolves.com/feed/",
    "FanSided Pelicans (Pelican Debrief)": "https://pelicandebrief.com/feed/",
    "FanSided Thunder (Thunderous Intentions)": "https://thunderousintentions.com/feed/",
    "FanSided Suns (Valley of the Suns)": "https://valleyofthesuns.com/feed/",
    "FanSided Trail Blazers (Rip City Project)": "https://ripcityproject.com/feed/",
    "FanSided Kings (A Royal Pain)": "https://aroyalpain.com/feed/",
    "FanSided Spurs (Air Alamo)": "https://airalamo.com/feed/",
    "FanSided Jazz (The J-Notes)": "https://thejnotes.com/feed/",

    # SB Nation NBA main
    "SB Nation NBA": "https://www.sbnation.com/rss/nba/index.xml",

    # SB Nation Atlantic
    "Celtics (CelticsBlog)": "https://www.celticsblog.com/rss/index.xml",
    "Nets (Nets Daily)": "https://www.netsdaily.com/rss/index.xml",
    "Knicks (Posting and Toasting)": "https://www.postingandtoasting.com/rss/index.xml",
    "Sixers (Liberty Ballers)": "https://www.libertyballers.com/rss/index.xml",
    "Raptors (Raptors HQ)": "https://www.raptorshq.com/rss/index.xml",

    # SB Nation Central
    "Bulls (Blog a Bull)": "https://www.blogabull.com/rss/index.xml",
    "Cavaliers (Fear the Sword)": "https://www.fearthesword.com/rss/index.xml",
    "Pistons (Detroit Bad Boys)": "https://www.detroitbadboys.com/rss/index.xml",
    "Pacers (Indy Cornrows)": "https://www.indycornrows.com/rss/index.xml",
    "Bucks (Brew Hoop)": "https://www.brewhoop.com/rss/index.xml",

    # SB Nation Southeast
    "Hawks (Peachtree Hoops)": "https://www.peachtreehoops.com/rss/index.xml",
    "Hornets (At the Hive)": "https://www.athrive.com/rss/index.xml",
    "Heat (Hot Hot Hoops)": "https://www.hothothoops.com/rss/index.xml",
    "Magic (Orlando Pinstriped Post)": "https://www.orlandopinstripedpost.com/rss/index.xml",
    "Wizards (Bullets Forever)": "https://www.bulletsforever.com/rss/index.xml",

    # SB Nation Northwest
    "Nuggets (Denver Stiffs)": "https://www.denverstiffs.com/rss/index.xml",
    "Timberwolves (Canis Hoopus)": "https://www.canishoopus.com/rss/index.xml",
    "Thunder (Welcome to Loud City)": "https://www.welcometoloudcity.com/rss/index.xml",
    "Trail Blazers (Blazers Edge)": "https://www.blazersedge.com/rss/index.xml",
    "Jazz (SLC Dunk)": "https://www.slcdunk.com/rss/index.xml",

    # SB Nation Pacific
    "Warriors (Golden State of Mind)": "https://www.goldenstateofmind.com/rss/index.xml",
    "Clippers (Clips Nation)": "https://www.clipsnation.com/rss/index.xml",
    "Lakers (Silver Screen and Roll)": "https://www.silverscreenandroll.com/rss/index.xml",
    "Suns (Bright Side of the Sun)": "https://www.brightsideofthesun.com/rss/index.xml",
    "Kings (Sactown Royalty)": "https://www.sactownroyalty.com/rss/index.xml",

    # SB Nation Southwest
    "Mavericks (Mavs Moneyball)": "https://www.mavsmoneyball.com/rss/index.xml",
    "Rockets (The Dream Shake)": "https://www.thedreamshake.com/rss/index.xml",
    "Grizzlies (Grizzly Bear Blues)": "https://www.grizzlybearblues.com/rss/index.xml",
    "Pelicans (The Bird Writes)": "https://www.thebirdwrites.com/rss/index.xml",
    "Spurs (Pounding the Rock)": "https://www.poundingtherock.com/rss/index.xml",
}


def _slugify(title: str) -> str:
    slug = title.lower().strip()
    slug = re.sub(r"[^a-z0-9\- ]+", " ", slug)
    slug = re.sub(r"\s+", "-", slug)
    return slug[:200]


def _map_category(tags: list, title: str = "", body: str = "") -> str:
    categories = {
        "game recap": "game_recap",
        "game preview": "game_preview",
        "preview": "game_preview",
        "recap": "game_recap",
        "fantasy": "fantasy_advice",
        "fantasy basketball": "fantasy_advice",
        "dfs": "fantasy_advice",
        "daily fantasy": "fantasy_advice",
        "draftkings": "fantasy_advice",
        "fanduel": "fantasy_advice",
        "betting": "betting_pick",
        "odds": "betting_pick",
        "pick": "betting_pick",
        "nba picks": "betting_pick",
        "prop": "betting_pick",
        "analysis": "team_analysis",
        "team analysis": "team_analysis",
        "scouting": "team_analysis",
        "draft": "team_analysis",
        "mock draft": "team_analysis",
        "prospect": "team_analysis",
        "trade": "news",
        "rumors": "news",
        "injury": "news",
        "signing": "news",
        "transaction": "news",
        "free agency": "news",
        "playoff": "general",
        "postseason": "general",
        "power ranking": "general",
    }
    for tag in tags:
        label = tag.get("label", "") or tag.get("term", "") or ""
        label_lower = label.lower().strip()
        for key, value in categories.items():
            if key in label_lower:
                return value
    return "general"


async def scrape_rss_feeds_nba(
    db: AsyncSession,
    max_per_feed: int = 20,
    skip_older_than_days: int = 30,
) -> dict:
    stats = {
        "feeds_checked": 0,
        "feeds_with_errors": 0,
        "entries_found": 0,
        "articles_scraped": 0,
        "duplicates_skipped": 0,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        for source_name, feed_url in RSS_FEEDS_NBA.items():
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

                    title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
                    slug = f"{_slugify(title)}-{title_hash}"

                    existing = await db.execute(
                        select(NBAArticle).where(NBAArticle.slug == slug)
                    )
                    if existing.scalar_one_or_none():
                        stats["duplicates_skipped"] += 1
                        continue

                    body = entry.get("summary", "") or entry.get("content", [{}])[0].get("value", "")
                    body_text = re.sub(r"<[^>]+>", " ", body)
                    body_text = html.unescape(body_text)
                    body_text = re.sub(r"\s+", " ", body_text).strip()

                    pub_struct = entry.get("published_parsed")
                    pub_date = (
                        datetime(*pub_struct[:6], tzinfo=timezone.utc)
                        if pub_struct
                        else datetime.now(timezone.utc)
                    )

                    days_old = (datetime.now(timezone.utc) - pub_date).days
                    if days_old > skip_older_than_days:
                        continue

                    author = None
                    if hasattr(entry, "author"):
                        author = entry.author
                    elif hasattr(entry, "authors") and entry.authors:
                        author = entry.authors[0].get("name")

                    tags = entry.get("tags", [])
                    category = _map_category(tags, title, body_text)

                    article = NBAArticle(
                        title=title,
                        slug=slug,
                        body=body_text,
                        excerpt=body_text[:500],
                        category=category,
                        tier="free",
                        published=True,
                        published_at=pub_date,
                        author=author or "NBA Staff",
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
