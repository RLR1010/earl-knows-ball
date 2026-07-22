"""
FanDuel sportsbook scraper — DOM-based extraction.

Navigates to FD pages headlessly, waits for React rendering, then extracts
market/runner/odds data directly from the rendered DOM with page.evaluate().
No API interception needed.

Uses one browser context for the full scrap session (cookies persist across
all tabs: futures -> awards -> games) and saves storage state afterwards.
"""

import asyncio
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from playwright.async_api import BrowserContext

from app.scrapers.models import TeamProp, PlayerSeasonProp, PlayerDailyProp

logger = logging.getLogger("earl.scrapers.fanduel")

# Tab types
TAB_GAMES = "games"
TAB_FUTURES = "futures"
TAB_AWARDS = "awards"

# ── Robust selectors (tried in order, pick first that matches) ──────────────
# FanDuel renders React components that use these patterns across MLB/NFL/NBA.
MARKET_SELECTORS = [
    '[data-testid*="market-accordion"]',
    '[data-testid*="accordion"]',
    '[class*="market-accordion"]',
    '[class*="MarketAccordion"]',
]

RUNNER_SELECTORS = [
    '[data-testid*="runner-row"]',
    '[data-testid*="outcome"]',
    '[class*="runner-row"]',
    '[class*="RunnerRow"]',
    '[class*="outcome-row"]',
    'div[role="button"]',
]

PRICE_SELECTORS = [
    '[data-testid*="bet-button"]',
    '[data-testid*="price"]',
    '[class*="price-cell"]',
    '[class*="PriceCell"]',
    '[class*="bet-button"]',
    '[class*="BetButton"]',
]


# ═══════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════

async def scrape_team_props(context: BrowserContext, sport: str) -> list[TeamProp]:
    """Scrape team futures (championship odds, win totals, etc.)."""
    page_data = await _load_tab_dom(context, sport, TAB_FUTURES)
    if not page_data:
        return []
    return _extract_team_props(page_data, sport)


async def scrape_awards(context: BrowserContext, sport: str) -> list[PlayerSeasonProp]:
    """Scrape player season awards (MVP, Cy Young, etc.)."""
    page_data = await _load_tab_dom(context, sport, TAB_AWARDS)
    if not page_data:
        return []
    return _extract_award_props(page_data, sport)


async def scrape_player_props(context: BrowserContext, sport: str) -> list[PlayerDailyProp]:
    """Scrape daily game-level player props."""
    page_data = await _load_tab_dom(context, sport, TAB_GAMES)
    if not page_data:
        return []
    return _extract_player_daily_props(page_data, sport)


# ═══════════════════════════════════════════════════════
# Internal: page loading via DOM extraction
# ═══════════════════════════════════════════════════════

def _tab_url(sport: str, tab: str) -> str:
    """Build FD tab URL."""
    slug_map = {"mlb": "mlb", "nfl": "nfl", "nba": "nba"}
    slug = slug_map.get(sport, sport)
    tab_params = {
        TAB_GAMES: "",
        TAB_FUTURES: "?tab=futures",
        TAB_AWARDS: "?tab=awards",
    }
    param = tab_params.get(tab, "")
    return f"https://sportsbook.fanduel.com/navigation/{slug}{param}"


async def _load_tab_dom(
    context: BrowserContext, sport: str, tab: str
) -> Optional[dict]:
    """
    Navigate to a FD tab, wait for React rendering, then extract market data
    from the rendered DOM using page.evaluate().

    Returns a dict matching the shape the _extract_* functions expect:
        {"attachments": {"markets": {market_id: {"marketName": ..., "runners": [...]}}}}
    """
    url = _tab_url(sport, tab)
    logger.info(f"FD {sport}/{tab}: loading {url}")

    page = await context.new_page()
    # NO page.close() at end. The persistent browser keeps tabs open
    # so that any captcha/challenge page stays visible for Rich to answer.
    try:
        await page.goto(url, wait_until="load", timeout=60_000)

        # Wait for network to settle and React to hydrate
        await page.wait_for_load_state("networkidle", timeout=30_000)
        await asyncio.sleep(3)

        # Check for DataDome/captcha page
        blocked = await page.evaluate(
            "document.body.innerText.substring(0, 300).toLowerCase().includes('blocked') "
            "|| document.body.innerText.substring(0, 300).toLowerCase().includes('captcha')"
        )
        if blocked:
            body = await page.evaluate("document.body.innerText.substring(0, 500)")
            logger.warning(f"FD {sport}/{tab}: DataDome blocked page\n{body}")
            return None

        # Extract markets from DOM
        page_data = await page.evaluate("""
        (args) => {
            const { marketSelectors, runnerSelectors, priceSelectors } = args;

            // Helper: find first matching element
            function qs(parent, selectors) {
                for (const sel of selectors) {
                    const el = parent.querySelector(sel);
                    if (el) return el;
                }
                return null;
            }

            // Helper: find ALL matching elements
            function qsa(parent, selectors) {
                const seen = new Set();
                const results = [];
                for (const sel of selectors) {
                    const els = parent.querySelectorAll(sel);
                    for (const el of els) {
                        if (!seen.has(el)) {
                            seen.add(el);
                            results.push(el);
                        }
                    }
                }
                return results;
            }

            // Helper: get clean text content
            function getText(el) {
                if (!el) return '';
                return el.textContent.trim().replace(/\\s+/g, ' ');
            }

            // Helper: parse odds text to American int
            function parseOdds(text) {
                text = text.trim();
                if (text.toUpperCase() === 'EVEN') return 100;
                const m = text.match(/^[+-]?\\d+$/);
                if (m) {
                    let val = parseInt(m[0], 10);
                    if (val > 0 && !text.startsWith('+')) val = -val;
                    return val;
                }
                return null;
            }

            // Find all market containers
            let marketContainers = qsa(document, marketSelectors);

            if (marketContainers.length === 0) {
                const allDivs = document.querySelectorAll('section, div[class*="accordion"], div[class*="market"]');
                for (const div of allDivs) {
                    if (div.querySelector('[class*="price"], [class*="odds"], [data-testid*="price"]')) {
                        marketContainers.push(div);
                    }
                }
            }

            if (marketContainers.length === 0) {
                const body = document.body;
                for (const child of body.children) {
                    if (child.tagName === 'SECTION' || child.tagName === 'DIV') {
                        const inner = child.querySelectorAll('div');
                        if (inner.length >= 3) {
                            marketContainers.push(child);
                        }
                    }
                }
            }

            const markets = {};
            let mId = 0;

            for (const mc of marketContainers) {
                const cls = (mc.className || '').toLowerCase();
                const html = (mc.innerHTML || '').toLowerCase();
                if (cls.includes('nav') || cls.includes('header') || cls.includes('footer') ||
                    cls.includes('sidebar')) continue;
                if (html.length < 50 || html.length > 500000) continue;

                let marketName = '';
                const headings = mc.querySelectorAll('h2, h3, h4, h5, strong, [class*="market-title"], [class*="MarketTitle"], [class*="header-title"]');
                for (const h of headings) {
                    const t = getText(h);
                    if (t.length > 1 && t.length < 200) {
                        marketName = t;
                        break;
                    }
                }
                if (!marketName) {
                    const textNodes = [];
                    function walkText(el, depth) {
                        if (depth > 3) return;
                        for (const child of el.children) {
                            const t = getText(child);
                            if (t.length > 3 && t.length < 200 &&
                                !t.includes('$') && !/^[+-]\\d+$/.test(t)) {
                                textNodes.push(t);
                            }
                            walkText(child, depth + 1);
                        }
                    }
                    walkText(mc, 0);
                    for (const t of textNodes) {
                        if (t.includes(' ') && !/^[+-]\\d/.test(t)) {
                            marketName = t;
                            break;
                        }
                    }
                }
                if (!marketName) continue;

                const runnerEls = qsa(mc, runnerSelectors);
                const runners = [];
                let priceEls = runnerEls.length > 0 ? [] : qsa(mc, priceSelectors);

                if (runnerEls.length > 0) {
                    for (const re of runnerEls) {
                        const runnerText = getText(re);
                        if (!runnerText || runnerText.length < 1) continue;

                        const priceEl = qs(re, priceSelectors);
                        const oddsText = priceEl ? getText(priceEl) : '';
                        const odds = oddsText ? parseOdds(oddsText) : null;

                        const finalOdds = odds !== null ? odds : (() => {
                            const match = runnerText.match(/[+-]\\d{2,4}/);
                            return match ? parseInt(match[0], 10) : null;
                        })();

                        if (finalOdds === null) continue;

                        let name = runnerText.replace(oddsText, '').trim();
                        name = name.replace(/[+-]\\d{2,4}/g, '').trim();
                        if (name.length < 1) continue;

                        runners.push({runnerName: name, odds: finalOdds});
                    }
                } else if (priceEls.length > 0) {
                    for (const pe of priceEls) {
                        const oddsText = getText(pe);
                        const odds = parseOdds(oddsText);
                        if (odds === null) continue;

                        let nameEl = pe.previousElementSibling || pe.parentElement?.previousElementSibling;
                        if (nameEl) {
                            const name = getText(nameEl).replace(oddsText, '').trim();
                            if (name.length > 0) {
                                runners.push({runnerName: name, odds: odds});
                                continue;
                            }
                        }
                        let parent = pe.parentElement;
                        for (let i = 0; i < 3 && parent; i++) {
                            const fullText = getText(parent);
                            const nonOdds = fullText.replace(oddsText, '').trim();
                            if (nonOdds.length > 0 && nonOdds.length < 100 &&
                                !/(championship|win total|make|miss)/i.test(nonOdds)) {
                                runners.push({runnerName: nonOdds, odds: odds});
                                break;
                            }
                            parent = parent.parentElement;
                        }
                    }
                }

                if (runners.length > 0) {
                    mId++;
                    markets[`m${mId}`] = {
                        marketName: marketName,
                        runners: runners
                    };
                }
            }

            return markets;
        }
        """ , {
            "marketSelectors": MARKET_SELECTORS,
            "runnerSelectors": RUNNER_SELECTORS,
            "priceSelectors": PRICE_SELECTORS,
        })

        if not page_data or len(page_data) == 0:
            # Last resort: dump raw HTML for debugging
            html = await page.content()
            logger.warning(
                f"FD {sport}/{tab}: DOM extraction returned 0 markets. "
                f"Page title: {await page.title()}. "
                f"HTML length: {len(html)}"
            )
            return None

        logger.info(
            f"FD {sport}/{tab}: DOM extracted {len(page_data)} markets"
        )

        return {"attachments": {"markets": page_data}}

    except Exception as e:
        logger.error(f"FD {sport}/{tab}: DOM extraction error: {e}")
        # Dump what we can for debugging
        try:
            html_len = await page.evaluate("document.documentElement.innerHTML.length")
            title = await page.title()
            logger.warning(f"FD {sport}/{tab}: debug — title='{title}', html_len={html_len}")
        except:
            pass
        return None
    # Intentionally NOT closing page — keep the tab open in the
    # persistent browser so captchas stay visible for Rich to answer.


# ═══════════════════════════════════════════════════════
# Team props extraction
# ═══════════════════════════════════════════════════════

def _extract_team_props(page_data: dict, sport: str) -> list[TeamProp]:
    """Extract team props from page data — market-based approach."""
    markets = page_data.get("attachments", {}).get("markets", {})
    props = []

    for mid, mk in markets.items():
        mn = mk.get("marketName", "").lower()
        runners = mk.get("runners", [])

        # Championship (World Series, Super Bowl, NBA Championship)
        if "championship" in mn or "winner" in mn:
            for r in runners:
                props.append(TeamProp(
                    sport=sport,
                    season_year=datetime.utcnow().year,
                    team_name=r["runnerName"],
                    bookmaker="fanduel",
                    championship_odds=_get_american_odds(r),
                ))

        # Win totals
        elif "win total" in mn or "regular season wins" in mn:
            for r in runners:
                # Parse "Over X.5" / "Under X.5" pattern
                parsed = _parse_team_win_total(r)
                if parsed:
                    team_name, win_total = parsed
                    odds = _get_american_odds(r)
                    if "over" in r["runnerName"].lower():
                        props.append(TeamProp(
                            sport=sport,
                            season_year=datetime.utcnow().year,
                            team_name=team_name,
                            bookmaker="fanduel",
                            win_total=win_total,
                            win_total_over_odds=odds,
                        ))
                    elif "under" in r["runnerName"].lower():
                        props.append(TeamProp(
                            sport=sport,
                            season_year=datetime.utcnow().year,
                            team_name=team_name,
                            bookmaker="fanduel",
                            win_total=win_total,
                            win_total_under_odds=odds,
                        ))

        # Make/miss playoffs
        elif "make playoffs" in mn or "miss playoffs" in mn:
            for r in runners:
                odds = _get_american_odds(r)
                if "make" in mn:
                    props.append(TeamProp(
                        sport=sport,
                        season_year=datetime.utcnow().year,
                        team_name=r["runnerName"],
                        bookmaker="fanduel",
                        make_playoffs_odds=odds,
                    ))
                elif "miss" in mn:
                    props.append(TeamProp(
                        sport=sport,
                        season_year=datetime.utcnow().year,
                        team_name=r["runnerName"],
                        bookmaker="fanduel",
                        miss_playoffs_odds=odds,
                    ))

    logger.info(f"FD {sport}: extracted {len(props)} team props")
    return props


# ═══════════════════════════════════════════════════════
# Awards extraction
# ═══════════════════════════════════════════════════════

def _extract_award_props(page_data: dict, sport: str) -> list[PlayerSeasonProp]:
    """Extract player season-long award props (MVP, Cy Young, etc.)."""
    markets = page_data.get("attachments", {}).get("markets", {})
    props = []

    # Map market name keywords to normalized prop_type values
    award_type_map = {
        "most valuable player": "mvp",
        "mvp": "mvp",
        "cy young": "cy_young",
        "rookie of the year": "rookie",
        "manager of the year": "manager",
        "comeback player": "comeback",
        "home run": "hr_leader",
        "strikeout": "k_leader",
        "batting title": "batting_leader",
        "defensive player": "dpoy",
    }

    for mid, mk in markets.items():
        mn = mk.get("marketName", "").strip()
        runners = mk.get("runners", [])
        if not runners:
            continue

        # Determine if this is an award market
        mn_lower = mn.lower()
        award_key = None
        for keyword, prop_type in award_type_map.items():
            if keyword in mn_lower:
                award_key = prop_type
                break
        if not award_key:
            continue

        # Determine conference/league suffix
        suffix = ""
        if "american league" in mn_lower or "al " in mn_lower:
            suffix = "_al"
        elif "national league" in mn_lower or "nl " in mn_lower:
            suffix = "_nl"

        # NBA awards
        if any(k in mn_lower for k in ["most valuable player", "mvp", "rookie", "defensive"]):
            if "eastern" in mn_lower:
                suffix = "_east"
            elif "western" in mn_lower:
                suffix = "_west"

        prop_type_key = f"{award_key}{suffix}" if suffix else award_key

        for r in runners:
            rn = r.get("runnerName", "")
            if not rn:
                continue

            odds = _get_american_odds(r)
            if odds is None:
                continue

            props.append(PlayerSeasonProp(
                sport=sport,
                season_year=datetime.utcnow().year,
                player_name=rn,
                team_name=None,
                prop_type=prop_type_key,
                bookmaker="fanduel",
                odds=odds,
            ))

    logger.info(f"FD {sport}: extracted {len(props)} award props")
    return props


# ═══════════════════════════════════════════════════════
# Player daily props extraction
# ═══════════════════════════════════════════════════════

def _extract_player_daily_props(page_data: dict, sport: str) -> list[PlayerDailyProp]:
    """Extract game-level player props from page data."""
    markets = page_data.get("attachments", {}).get("markets", {})
    props = []

    for mid, mk in markets.items():
        mn = mk.get("marketName", "").strip()
        runners = mk.get("runners", [])
        if not runners:
            continue

        # Skip non-player markets
        if _is_game_line(mn):
            continue
        if _is_season_prop(mn):
            continue
        if _is_injury_prop(mn):
            continue

        # Determine prop type from market name
        prop_type = _classify_player_prop(mn)

        for r in runners:
            rn = r.get("runnerName", "")
            odds = _get_american_odds(r)
            if not rn or odds is None:
                continue

            # Parse the runner: extract line and direction
            parsed = _parse_runner(rn, mn)
            if not parsed:
                continue

            player_name, line, direction = parsed

            props.append(PlayerDailyProp(
                sport=sport,
                game_id="",
                player_name=player_name,
                team_name=None,
                prop_type=prop_type,
                bookmaker="fanduel",
                line=line,
                odds=odds,
                direction=direction,
            ))

    logger.info(f"FD {sport}: extracted {len(props)} player daily props")
    return props


# ═══════════════════════════════════════════════════════
# Helper: market/runner classification
# ═══════════════════════════════════════════════════════

def _is_game_line(market_name: str) -> bool:
    """Check if this is a game line (not a player prop)."""
    mn = market_name.lower().strip()
    game_lines = ["moneyline", "spread", "total runs", "total points", "handicap"]
    if mn in game_lines:
        return True
    if mn in ["over", "under"]:
        return True
    return False


def _is_season_prop(market_name: str) -> bool:
    """Check if this is a season-long prop, not a game prop."""
    mn = market_name.lower()
    season_kw = ["regular season", "season wins", "win total", "leader 2026",
                 "championship", "world series", "super bowl",
                 "stanley cup", "nba championship"]
    return any(kw in mn for kw in season_kw)


def _is_injury_prop(market_name: str) -> bool:
    """Check if this is an injury replacement prop."""
    mn = market_name.lower()
    return "injury" in mn or "replacement" in mn or "alternate" in mn


def _classify_player_prop(market_name: str) -> str:
    """Classify a prop type from market name. Returns a normalized key."""
    mn = market_name.lower()
    mapping = [
        (lambda s: "strikeout" in s, "strikeouts"),
        (lambda s: "home run" in s and "allowed" not in s, "home_runs"),
        (lambda s: "hits" in s and "allowed" not in s and "home" not in s, "hits"),
        (lambda s: "hit" in s, "hits"),
        (lambda s: "runs" in s or "run" in s, "runs"),
        (lambda s: "rbi" in s, "rbi"),
        (lambda s: "rebound" in s, "rebounds"),
        (lambda s: "assist" in s, "assists"),
        (lambda s: "point" in s and "three" not in s and "turnover" not in s, "points"),
        (lambda s: "three" in s or "3pt" in s or "3-pointer" in s, "three_pointers"),
        (lambda s: "steal" in s, "steals"),
        (lambda s: "block" in s, "blocks"),
        (lambda s: "turnover" in s, "turnovers"),
        (lambda s: "touchdown" in s or "td" in s, "touchdowns"),
        (lambda s: "yard" in s or "passing" in s, "passing_yards"),
        (lambda s: "reception" in s, "receptions"),
        (lambda s: "sack" in s, "sacks"),
        (lambda s: "error" in s, "errors"),
        (lambda s: "win" in s, "wins"),
        (lambda s: "save" in s, "saves"),
        (lambda s: "innings" in s or "ip" in s, "innings_pitched"),
    ]
    for pred, label in mapping:
        if pred(mn):
            return label
    return "other"


def _parse_runner(runner_name: str, market_name: str) -> Optional[tuple]:
    """
    Parse a daily prop runner name into (player_name, line, direction).

    Supports:
      - "K. Bryant Over 1.5 Hits"    → (K. Bryant, 1.5, over)
      - "J. Smith Under 10.5 Points"  → (J. Smith, 10.5, under)
      - "M. Rivera 2+ Hits"           → (M. Rivera, 1.5, tiered)
      - "Over 8.5 Stikeouts"          → (player_unknown, 8.5, over)
    """
    rn = runner_name.strip()
    mn = market_name.strip()

    # Patterns: "PlayerName Over/Under X.5 PropName" or "PlayerName X+ PropName"
    # Also: "Over X.5" / "Under X.5" (game lines, already filtered)

    # Try "Over X.5" / "Under X.5" pattern in name
    ou_match = re.search(r"(over|under)\s+(\d+\.?\d*)", rn, re.IGNORECASE)
    if ou_match:
        direction = ou_match.group(1).lower()
        line = Decimal(ou_match.group(2))
        # Everything before the O/U is the player name
        player = rn[:ou_match.start()].strip()
        return (player, line, direction)

    # Try "X+ Hits" / "2+ Runs" pattern (tiered)
    tiered_match = re.search(r"(\d+)\s*\+", rn)
    if tiered_match:
        line = Decimal(tiered_match.group(1))
        player = rn[:tiered_match.start()].strip()
        return (player, line, "tiered")

    # If runner name doesn't have O/U or tiered, try full form:
    # "K. Bryant Over 1.5 Hits" — but the market name should be just "Hits"
    # This happens when FD doesn't prefix the runner name
    for direction in ["over", "under"]:
        pat = re.compile(rf"({direction})\s+(\d+\.?\d*)", re.IGNORECASE)
        m = pat.search(rn)
        if m:
            line = Decimal(m.group(2))
            player = rn[:m.start()].strip()
            return (player, line, direction)

    # No parseable pattern — assume tiered or unknown
    return None


def _parse_team_win_total(runner: dict) -> Optional[tuple]:
    """Parse a win total runner into (team_name, win_total)."""
    rn = runner.get("runnerName", "")
    m = re.search(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(over|under)\s+(\d+\.?\d*)", rn, re.IGNORECASE)
    if m:
        return (m.group(1), Decimal(m.group(3)))
    return None


def _get_american_odds(runner: dict) -> Optional[int]:
    """Extract American odds from runner dict."""
    raw = runner.get("odds")
    if raw is not None:
        return int(raw)
    return None
