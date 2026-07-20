#!/usr/bin/env python3
"""
Wait for FD cookies on stdin, then bootstrap the session immediately.

Run this script, then immediately copy fresh cookies from your browser
and paste them into the terminal. The script will read stdin and use
the cookies within milliseconds.
"""
import asyncio, json, logging, sys, time
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s", stream=sys.stdout)
log = logging.getLogger("earl.cookie_bootstrap")

from app.scrapers.browser import BrowserManager, STORAGE_STATE_PATH
from app.scrapers.books import fanduel
from app.core.config import settings
from sqlalchemy import create_engine
from app.scrapers.db import save_team_props, save_player_season_props, save_player_daily_props


def parse_cookies_table(text: str) -> list[dict]:
    """Parse the tab-separated cookie format Rich copies from Chrome."""
    cookies = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or line.startswith("___"):
            continue
        # Tab-separated: name value domain path expires size httpOnly secure sameSite priority
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cookie = {
            "name": parts[0],
            "value": parts[1],
            "domain": parts[2],
            "path": parts[3] if len(parts) > 3 else "/",
        }
        cookies.append(cookie)
    return cookies


async def run():
    print("\n⚡ COOKIE BOOTSTRAP SCRIPT")
    print("=" * 60)
    print("1. Open https://sportsbook.fanduel.com in Chrome")
    print("2. Copy ALL cookies from DevTools → Application → Cookies")
    print("3. Paste them below and press Ctrl+D to finish")
    print("=" * 60)
    print()

    # Read multiline input
    lines = []
    try:
        for line in sys.stdin:
            lines.append(line.rstrip("\n"))
    except EOFError:
        pass

    raw = "\n".join(lines)
    cookies = parse_cookies_table(raw)
    log.info(f"Parsed {len(cookies)} cookies")

    # Delete old state
    STORAGE_STATE_PATH.unlink(missing_ok=True)

    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url, pool_pre_ping=True)

    browser = BrowserManager()
    await browser.start()
    stats = {"team_props": 0, "season_props": 0, "daily_props": 0}

    try:
        # TAB 1: Team props
        log.info("=== TAB 1: Team props ===")
        ctx = await browser.new_context(bootstrap_cookies=cookies)
        t0 = time.time()
        props = await fanduel.scrape_team_props(ctx, "mlb")
        log.info(f"  {time.time()-t0:.1f}s — {len(props)} props")
        if props:
            stats["team_props"] = save_team_props(engine, props)
            log.info(f"  → Saved {stats['team_props']} rows")
        await browser.save_storage_state(ctx)
        ctx_saved_path = STORAGE_STATE_PATH
        await ctx.close()
        await asyncio.sleep(2)

        # TAB 2: Awards
        log.info("=== TAB 2: Awards ===")
        ctx = await browser.new_context()
        t0 = time.time()
        props = await fanduel.scrape_awards(ctx, "mlb")
        log.info(f"  {time.time()-t0:.1f}s — {len(props)} props")
        if props:
            stats["season_props"] = save_player_season_props(engine, props)
            log.info(f"  → Saved {stats['season_props']} rows")
        await ctx.close()
        await asyncio.sleep(2)

        # TAB 3: Daily props
        log.info("=== TAB 3: Daily props ===")
        ctx = await browser.new_context()
        t0 = time.time()
        props = await fanduel.scrape_player_props(ctx, "mlb")
        log.info(f"  {time.time()-t0:.1f}s — {len(props)} props")
        if props:
            stats["daily_props"] = save_player_daily_props(engine, props)
            log.info(f"  → Saved {stats['daily_props']} rows")
        await ctx.close()

    finally:
        await browser.stop()

    log.info(f"\n=== FINAL: {stats} ===")
    from sqlalchemy import text
    with engine.connect() as conn:
        for t in ["team_props","player_season_props","player_daily_props"]:
            r = conn.execute(text(f"SELECT COUNT(*) FROM mlb.{t}")).scalar()
            log.info(f"  mlb.{t}: {r} rows")

    log.info(f"\nStorage state saved to: {ctx_saved_path}")
    log.info("Future runs will use this saved session automatically.")


if __name__ == "__main__":
    asyncio.run(run())
