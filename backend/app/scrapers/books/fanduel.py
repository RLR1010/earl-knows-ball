"""
FanDuel sportsbook scraper — rewritten for real API structure.

Captures data by navigating to FD pages and intercepting their internal API calls.
Uses Playwright with stealth to bypass bot detection.

Data flow:
  1. content-managed-page → page structure (tabs, coupons, events, markets, runner names)
  2. getMarketPrices → actual odds for visible markets

We merge the two: market metadata (names, runner names) from step 1, prices from step 2.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from decimal import Decimal
from typing import Optional

from playwright.async_api import Page, BrowserContext
from playwright_stealth import Stealth

from app.scrapers.models import TeamProp, PlayerSeasonProp, PlayerDailyProp

logger = logging.getLogger("earl.scrapers.fanduel")

# Tab types
TAB_GAMES = "games"
TAB_FUTURES = "futures"
TAB_AWARDS = "awards"


# ═══════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════

async def scrape_team_props(context: BrowserContext, sport: str) -> list[TeamProp]:
    """
    Scrape team props (championship odds, make playoffs, win totals).
    """
    page_data = await _load_tab(context, sport, TAB_FUTURES)
    if not page_data:
        return []
    return _extract_team_props(page_data, sport)


async def scrape_awards(context: BrowserContext, sport: str) -> list[PlayerSeasonProp]:
    """
    Scrape player award props (MVP, Cy Young, Rookie of Year, etc.).
    """
    page_data = await _load_tab(context, sport, TAB_AWARDS)
    if not page_data:
        return []
    return _extract_award_props(page_data, sport)


async def scrape_player_props(context: BrowserContext, sport: str) -> list[PlayerDailyProp]:
    """
    Scrape daily game-level player props.
    """
    page_data = await _load_tab(context, sport, TAB_GAMES)
    if not page_data:
        return []
    return _extract_player_daily_props(page_data, sport)


# ═══════════════════════════════════════════════════════
# Internal: page loading
# ═══════════════════════════════════════════════════════

async def _load_tab(
    context: BrowserContext, sport: str, tab: str
) -> Optional[dict]:
    """
    Navigate to a FD tab, capture all API data, return merged structure.
    """
    url = _tab_url(sport, tab)
    logger.info(f"FD {sport}/{tab}: loading {url}")

    page = await context.new_page()
    result = {"page": None, "prices": []}

    async def capture(response):
        url = response.url
        if response.status != 200:
            return
        try:
            if "content-managed-page" in url:
                result["page"] = await response.json()
            elif "getMarketPrices" in url:
                data = await response.json()
                result["prices"].append(data)
        except Exception as e:
            logger.warning(f"FD capture error: {e}")

    page.on("response", capture)

    try:
        await page.goto(url, wait_until="load", timeout=60_000)
        await page.wait_for_timeout(8000)
    except Exception as e:
        logger.error(f"FD {sport}/{tab} navigation error: {e}")
        await page.close()
        return None

    await page.close()

    if not result["page"]:
        logger.warning(f"FD {sport}/{tab}: no content-managed-page captured")
        return None

    # The content-managed-page already has runner odds embedded
    logger.info(
        f"FD {sport}/{tab}: loaded page ({len(result['page'].get('attachments',{}).get('markets',{}))} "
        f"markets, {len(result['prices'])} price sets)"
    )
    return result["page"]


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
        # Only include markets where runner names are simple team names,
        # not full "Team A to beat Team B" head-to-head matchups.
        champ_keywords = {
            "mlb": ["world series"],
            "nfl": ["super bowl"],
            "nba": ["nba championship", "nba finals"],
        }
        if any(kw in mn for kw in champ_keywords.get(sport, [])):
            for r in runners:
                rname = r.get("runnerName", "")
                price = _get_american_odds(r)
                # Skip head-to-head matchups (contain "to beat")
                if not rname or "to beat" in rname.lower():
                    continue
                if rname and price is not None:
                    props.append(TeamProp(
                        sport=sport,
                        season_year=_extract_year(mn, sport),
                        team_name=rname,
                        bookmaker="fanduel",
                        championship_odds=price,
                    ))

        # Win totals — FD uses tiered thresholds ("70+ Wins @ -112"),
        # not standard O/U lines. Skip for now since the schema expects
        # a single win_total + over_odds/under_odds.
        # Revisit when we add a second book (DK) or change the schema.

    # Deduplicate
    seen = set()
    unique = []
    for p in props:
        key = (p.team_name, p.championship_odds, str(p.win_total))
        if key not in seen:
            seen.add(key)
            unique.append(p)

    logger.info(f"FD {sport}: extracted {len(unique)} team props")
    logger.info(f"  Championship: {len([p for p in unique if p.championship_odds])}")
    logger.info(f"  Win totals: {len([p for p in unique if p.win_total])}")
    return unique


# ═══════════════════════════════════════════════════════
# Award props extraction
# ═══════════════════════════════════════════════════════

def _extract_award_props(page_data: dict, sport: str) -> list[PlayerSeasonProp]:
    """Extract player award props from page data."""
    markets = page_data.get("attachments", {}).get("markets", {})
    props = []

    for mid, mk in markets.items():
        mn = mk.get("marketName", "")
        award_type = _classify_award(mn, sport)
        if not award_type:
            continue

        runners = mk.get("runners", [])
        for r in runners:
            rname = r.get("runnerName", "")
            price = _get_american_odds(r)
            if rname and price is not None:
                props.append(PlayerSeasonProp(
                    sport=sport,
                    season_year=_extract_year(mn, sport),
                    player_name=rname,
                    team_name=None,
                    prop_type=award_type,
                    bookmaker="fanduel",
                    odds=price,
                ))

    logger.info(f"FD {sport}: extracted {len(props)} award props")
    return props


# ═══════════════════════════════════════════════════════
# Player daily props extraction
# ═══════════════════════════════════════════════════════

def _extract_player_daily_props(page_data: dict, sport: str) -> list[PlayerDailyProp]:
    """Extract game-level player props from page data.

    FD formats:
      - Standard O/U: market "Strikeouts", runners "Over 0.5" / "Under 0.5"
      - Tiered:      market "Home Runs Allowed", runners "2+ Hits", "3+ Hits"
      - Season:      market "Regular Season Home Runs Leader 2026" → skip
      - Game lines:  market "Total Runs" or "Moneyline" → skip
    """
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
        game_id = str(mk.get("competitionId", mk.get("eventId", "")))

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

            # Determine game_id from competition for this runner
            # (some runners might belong to different events)

            props.append(PlayerDailyProp(
                sport=sport,
                game_id=game_id,
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


def _is_game_line(market_name: str) -> bool:
    """Check if this is a game line (not a player prop)."""
    mn = market_name.lower().strip()
    game_lines = ["moneyline", "spread", "total runs", "total points", "handicap"]
    if mn in game_lines:
        return True
    # Also check if it starts with generic O/U
    if mn in ["over", "under"]:
        return True
    return False


def _is_season_prop(market_name: str) -> bool:
    """Check if this is a season-long prop, not a game prop."""
    mn = market_name.lower()
    season_kw = ["regular season", "season wins", "win total", "leader 2026",
                 "2026 winner", "to win ", "championship", "make playoffs",
                 "world series", "super bowl", "nba championship"]
    for kw in season_kw:
        if kw in mn:
            return True
    return False


def _is_injury_prop(market_name: str) -> bool:
    """Check if this is an injury status prop."""
    mn = market_name.lower()
    injury_kw = ["doubtful", "questionable", "out", "injured", 
                 "starting pitcher", "lineup", "probable"]
    for kw in injury_kw:
        if kw in mn:
            return True
    return False


def _parse_runner(runner_name: str, market_name: str):
    """Parse a runner name into (player_name, line, direction).

    Examples:
      "Over 0.5" → (market_name, 0.5, 'over')
      "Under 0.5" → (market_name, 0.5, 'under')
      "2+ Hits" → (market_name, 2, 'tiered')
      "Shohei Ohtani 2+ Hits" → ("Shohei Ohtani", 2, 'tiered')
      "Over" → (market_name, None, 'over')
      "Under" → (market_name, None, 'under')
    """
    rn = runner_name.strip()
    mn = market_name.strip()

    # Case 1: "Over 0.5" or "Under 0.5"
    import re
    over_match = re.match(r"(?i)^(over|under)\s+(\d+\.?\d*)\s*.*", rn)
    if over_match:
        direction = over_match.group(1).lower()
        line = Decimal(over_match.group(2))
        return (mn, line, direction)

    # Case 2: "Over" or "Under" (no number)
    if rn.lower() in ["over", "under"]:
        return (mn, Decimal("0"), rn.lower())

    # Case 3: "2+ Hits" or "3+ Home Runs"
    tier_match = re.match(r"^(\d+\.?\d*)\+\s+(.*)", rn)
    if tier_match:
        line = Decimal(tier_match.group(1))
        # Player name might be in the runner name with tier
        # e.g., "Shohei Ohtani 2+ Hits"
        return (mn, line, "tiered")

    # Case 4: "Player Name 2+ Stat"
    player_tier = re.match(r"^(.+?)\s+(\d+\.?\d*)\+\s+(.*)", rn)
    if player_tier:
        player_name = player_tier.group(1).strip()
        line = Decimal(player_tier.group(2))
        return (player_name, line, "tiered")

    # Unrecognized format — skip
    return None


# ═══════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════


def _get_american_odds(runner: dict) -> Optional[int]:
    """Extract American odds from a runner's winRunnerOdds."""
    try:
        return runner["winRunnerOdds"]["americanDisplayOdds"]["americanOddsInt"]
    except (KeyError, TypeError):
        pass
    try:
        return runner["winRunnerOdds"]["americanDisplayOdds"]["americanOdds"]
    except (KeyError, TypeError):
        pass
    try:
        return runner["winRunnerOdds"]["trueOdds"]["fractionalOdds"]["numerator"]
    except (KeyError, TypeError):
        pass
    return None


def _get_market_team_name(market: dict, market_name: str) -> Optional[str]:
    """Infer team name from a market (used for win totals)."""
    # Win total markets have the team name in the marketName
    mn = market.get("marketName", "")
    for suffix in [" Win Total", " Season Wins", " Over/Under",
                   " Regular Season Wins", " Regular Season Win Total"]:
        idx = mn.lower().find(suffix.lower())
        if idx >= 0:
            return mn[:idx].strip()
    return None


def _classify_award(market_name: str, sport: str) -> Optional[str]:
    """Match market name to award type key."""
    mn = market_name.lower()
    awards = {
        "mlb": [
            ("mvp", "mvp"),
            ("cy young", "cy_young"),
            ("rookie of the year", "rookie_of_year"),
            ("manager of the year", "manager_of_year"),
            ("comeback player", "comeback_player"),
            ("silver slugger", "silver_slugger"),
            ("gold glove", "gold_glove"),
        ],
        "nfl": [
            ("mvp", "mvp"),
            ("offensive player of the year", "offensive_poy"),
            ("defensive player of the year", "defensive_poy"),
            ("offensive rookie", "offensive_roy"),
            ("defensive rookie", "defensive_roy"),
            ("comeback player", "comeback_player"),
        ],
        "nba": [
            ("mvp", "mvp"),
            ("rookie of the year", "rookie_of_year"),
            ("defensive player", "defensive_poy"),
            ("sixth man", "sixth_man"),
            ("most improved", "most_improved"),
            ("comeback player", "comeback_player"),
        ],
    }
    for keyword, award_key in awards.get(sport, []):
        if keyword in mn:
            return award_key
    return None


def _classify_player_prop(market_name: str) -> str:
    """Map market name to prop type key."""
    mn = market_name.lower()
    prop_map = {
        "strikeout": "strikeouts_ou",
        "hit": "hits_ou",
        "home run": "home_runs_ou",
        "rbi": "rbis_ou",
        "point": "points_ou",
        "rebound": "rebounds_ou",
        "assist": "assists_ou",
        "3-pointer": "threes_ou",
        "passing yard": "passing_yards_ou",
        "rushing yard": "rushing_yards_ou",
        "receiving yard": "receiving_yards_ou",
        "touchdown": "touchdowns_ou",
        "sack": "sacks_ou",
        "tackle": "tackles_ou",
        "interception": "interceptions_ou",
    }
    for keyword, prop_key in prop_map.items():
        if keyword in mn:
            return prop_key
    return "unknown_prop"


def _extract_year(text: str, sport: str) -> int:
    """Extract season year from text."""
    years = re.findall(r"\b(20\d{2})\b", text)
    if years:
        return int(years[0])
    now = datetime.utcnow()
    if sport == "nfl" and now.month >= 9:
        return now.year + 1
    return now.year


def _extract_decimal(text: str) -> Optional[Decimal]:
    """Extract a decimal number from text."""
    match = re.search(r"(\d+\.?\d*)", text)
    if match:
        try:
            return Decimal(match.group(1))
        except Exception:
            pass
    return None



