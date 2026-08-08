"""
BetMGM futures / season-prop scraper.

Source: BetMGM's Bwin CDS API (https://www.il.betmgm.com/cds-api/...).
This is an API-based scraper, NOT a DOM scraper. All data comes from a
single JSON call that includes every futures/awards fixture for a sport.

Why this book:
  - The Odds API only has championship outrights (super_bowl / world_series /
    nba_championship). It has NO player awards (MVP/Cy Young/ROY) and no
    win totals / make-playoffs team props.
  - FanDuel awards scraping is throttled by DataDome (~1-2 sessions/IP/day).
  - BetMGM carries awards + win totals + make-playoffs and its Cloudflare is
    reachable with Playwright Firefox + stealth (same browser as the FD scraper).

Architecture mirrors fanduel.py + db.py: produce normalized Model instances
(TeamProp / PlayerSeasonProp) that db.save_team_props() / save_player_season_props()
write to the per-sport schema tables (team_props / player_season_props).

Accessid: BetMGM's CDS API requires an `x-bwin-accessid` token obtained during
a live session. It is captured automatically from the first in-flight request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import Counter, defaultdict
from decimal import Decimal
from typing import Optional

from app.scrapers.models import TeamProp, PlayerSeasonProp
from app.scrapers.sports import SportScrapeConfig

logger = logging.getLogger("earl.scrapers.betmgm")

# BetMGM CDS sport ids
SPORT_IDS = {"mlb": 23, "nfl": 11, "nba": 7}

# Bwin CDS base for the Illinois sportsbook (full markets, legal state)
BASE = "https://www.il.betmgm.com"

# Market category tags emitted by BetMGM (templateCategory.dynamicCategories)
AWARD_CATEGORIES = {
    "mvp", "cyyoung", "cy", "rookie", "roy", "dpoy", "opoy", "cpoy",
    "mip", "sixthman", "playerawards", "moty", "awards", "coy",
}
# Win-total / playoff / championship category tags
TEAM_FUTURE_CATEGORIES = {
    "regularseasonwins", "teamfutures", "playoff", "outrights", "championship",
}

# Award prop_type normalization by (league keyword present in market name, category)
# We map BetMGM market names -> our normalized prop_type. Examples:
#   "American League MVP winner"              -> mvp_al
#   "National League Cy Young winner"         -> cy_young_nl
#   "American League Rookie of the Year"      -> rookie_al
#   "NFL MVP Award Winner"                    -> mvp (no league)
#   "NBA Most Valuable Player"                -> mvp
#   "NBA Rookie of the Year"                  -> rookie
# The league split only applies to MLB (AL/NL) and to AL/NL-specific awards.

MLB_LEAGUES = {"american league": "al", "national league": "nl"}


# Canonical team names per sport (as stored in {sport}.teams) + abbrev -> name.
# Used to normalize BetMGM's short/mascot/city-only variants and to reject
# non-league teams (e.g. WNBA leaking into the NBA response).
_SPORT_TEAMS: dict[str, set[str]] = {
    "nfl": {"Arizona Cardinals", "Atlanta Falcons", "Baltimore Ravens", "Buffalo Bills",
            "Carolina Panthers", "Chicago Bears", "Cincinnati Bengals", "Cleveland Browns",
            "Dallas Cowboys", "Denver Broncos", "Detroit Lions", "Green Bay Packers",
            "Houston Texans", "Indianapolis Colts", "Jacksonville Jaguars", "Kansas City Chiefs",
            "Las Vegas Raiders", "Los Angeles Chargers", "Los Angeles Rams", "Miami Dolphins",
            "Minnesota Vikings", "New England Patriots", "New Orleans Saints", "New York Giants",
            "New York Jets", "Philadelphia Eagles", "Pittsburgh Steelers", "San Francisco 49ers",
            "Seattle Seahawks", "Tampa Bay Buccaneers", "Tennessee Titans", "Washington Commanders"},
    "nba": {"Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
            "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
            "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
            "LA Clippers", "Los Angeles Lakers", "Memphis Grizzlies", "Miami Heat",
            "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
            "New York Knicks", "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
            "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
            "Toronto Raptors", "Utah Jazz", "Washington Wizards"},
    "mlb": {"Arizona Diamondbacks", "Atlanta Braves", "Baltimore Orioles", "Boston Red Sox",
            "Chicago Cubs", "Chicago White Sox", "Cincinnati Reds", "Cleveland Guardians",
            "Colorado Rockies", "Detroit Tigers", "Houston Astros", "Kansas City Royals",
            "Los Angeles Angels", "Los Angeles Dodgers", "Miami Marlins", "Milwaukee Brewers",
            "Minnesota Twins", "New York Mets", "New York Yankees", "Oakland Athletics",
            "Philadelphia Phillies", "Pittsburgh Pirates", "San Diego Padres", "San Francisco Giants",
            "Seattle Mariners", "St. Louis Cardinals", "Tampa Bay Rays", "Texas Rangers",
            "Toronto Blue Jays", "Washington Nationals"},
}

# BetMGM short names (city-only / mascot-only) -> canonical full name, per sport.
_TEAM_ALIASES: dict[str, dict[str, str]] = {
    "nfl": {
        "49ers": "San Francisco 49ers", "bears": "Chicago Bears", "bengals": "Cincinnati Bengals",
        "bills": "Buffalo Bills", "broncos": "Denver Broncos", "browns": "Cleveland Browns",
        "buccaneers": "Tampa Bay Buccaneers", "cardinals": "Arizona Cardinals", "chargers": "Los Angeles Chargers",
        "chiefs": "Kansas City Chiefs", "colts": "Indianapolis Colts", "commanders": "Washington Commanders",
        "cowboys": "Dallas Cowboys", "dolphins": "Miami Dolphins", "eagles": "Philadelphia Eagles",
        "falcons": "Atlanta Falcons", "giants": "New York Giants", "jaguars": "Jacksonville Jaguars",
        "jets": "New York Jets", "lions": "Detroit Lions", "packers": "Green Bay Packers",
        "panthers": "Carolina Panthers", "patriots": "New England Patriots", "raiders": "Las Vegas Raiders",
        "rams": "Los Angeles Rams", "ravens": "Baltimore Ravens", "saints": "New Orleans Saints",
        "seahawks": "Seattle Seahawks", "steelers": "Pittsburgh Steelers", "titans": "Tennessee Titans",
        "vikings": "Minnesota Vikings", "texans": "Houston Texans", "buccaneers": "Tampa Bay Buccaneers",
    },
    "nba": {
        "76ers": "Philadelphia 76ers", "cavaliers": "Cleveland Cavaliers", "celtics": "Boston Celtics",
        "clippers": "LA Clippers", "heat": "Miami Heat", "jazz": "Utah Jazz", "knicks": "New York Knicks",
        "lakers": "Los Angeles Lakers", "thunder": "Oklahoma City Thunder", "warriors": "Golden State Warriors",
        "wizards": "Washington Wizards", "hawks": "Atlanta Hawks", "nets": "Brooklyn Nets",
        "hornets": "Charlotte Hornets", "bulls": "Chicago Bulls", "mavericks": "Dallas Mavericks",
        "nuggets": "Denver Nuggets", "pistons": "Detroit Pistons", "rockets": "Houston Rockets",
        "pacers": "Indiana Pacers", "grizzlies": "Memphis Grizzlies", "bucks": "Milwaukee Bucks",
        "timberwolves": "Minnesota Timberwolves", "pelicans": "New Orleans Pelicans", "magic": "Orlando Magic",
        "suns": "Phoenix Suns", "trail blazers": "Portland Trail Blazers", "kings": "Sacramento Kings",
        "spurs": "San Antonio Spurs", "raptors": "Toronto Raptors",
        "los angeles clippers": "LA Clippers",  # BetMGM uses the full name in win-total markets
    },
    "mlb": {
        "diamondbacks": "Arizona Diamondbacks", "braves": "Atlanta Braves", "orioles": "Baltimore Orioles",
        "red sox": "Boston Red Sox", "cubs": "Chicago Cubs", "white sox": "Chicago White Sox",
        "reds": "Cincinnati Reds", "guardians": "Cleveland Guardians", "rockies": "Colorado Rockies",
        "tigers": "Detroit Tigers", "astros": "Houston Astros", "royals": "Kansas City Royals",
        "angels": "Los Angeles Angels", "dodgers": "Los Angeles Dodgers", "marlins": "Miami Marlins",
        "brewers": "Milwaukee Brewers", "twins": "Minnesota Twins", "mets": "New York Mets",
        "yankees": "New York Yankees", "athletics": "Oakland Athletics", "phillies": "Philadelphia Phillies",
        "pirates": "Pittsburgh Pirates", "padres": "San Diego Padres", "giants": "San Francisco Giants",
        "mariners": "Seattle Mariners", "cardinals": "St. Louis Cardinals", "rays": "Tampa Bay Rays",
        "rangers": "Texas Rangers", "blue jays": "Toronto Blue Jays", "nationals": "Washington Nationals",
    },
}


def _canonical_team_name(sport: str, raw: str) -> Optional[str]:
    """Return the canonical DB team name (or None if not a real league team).

    Handles BetMGM's full names, city-only, and mascot-only variants, and
    rejects WNBA/college/junk rows.
    """
    if not raw:
        return None
    s = raw.strip()
    canon = _SPORT_TEAMS.get(sport, set())
    if s in canon:
        return s
    # try alias (case-insensitive on both)
    key = s.lower()
    aliases = _TEAM_ALIASES.get(sport, {})
    if key in aliases:
        return aliases[key]
    # try normalize: strip 'the', collapse spaces, and match a canonical's city/mascot
    return None


def _american_odds(result: dict) -> Optional[int]:
    """Extract American odds from a CDS result dict."""
    v = result.get("americanOdds")
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("+-").isdigit():
        return int(float(v))
    # fallback: price block
    price = result.get("price") or {}
    for cand in ("americanDisplayOdds", "americanFormat"):
        c = price.get(cand)
        if isinstance(c, (int, float)):
            return int(c)
        if isinstance(c, dict):
            c2 = c.get("americanOddsInt") or c.get("value")
            if isinstance(c2, (int, float)):
                return int(c2)
    return None


def _player_name(result: dict) -> Optional[str]:
    """Extract the player/team name from a CDS result."""
    nm = result.get("name")
    if isinstance(nm, dict):
        return nm.get("value")
    if isinstance(nm, str):
        return nm
    return None


# BetMGM mixes College Football players into NFL award markets (esp. rookie /
# player-of-the-year). NFL rookies carry an NFL-team suffix like "Michael Trigg
# (DAL)"; college prospects have no suffix. Keep only NFL-suffixed entries and
# strip the suffix for storage.
_NFL_TEAM_SUFFIX = re.compile(r"\s*\(([A-Z]{2,4})\)\s*$")


def _filter_nfl_award_player(name: str, sport: str, prop_type: str) -> Optional[str]:
    """Return a clean player name if the entry is a real NFL player, else None.

    Only the NFL Rookie-of-the-Year markets mix in College Football prospects.
    Real NFL rookies carry an NFL-team paren suffix like "Michael Trigg (DAL)";
    college prospects lack it. For rookie markets, keep suffixed entries and
    strip the suffix. For MLB/NBA and non-rookie NFL awards (MVP/DPOY/etc. are
    pure-pro), return the name as-is.
    """
    if sport != "nfl" or not (prop_type and "roty" in prop_type):
        return name
    m = _NFL_TEAM_SUFFIX.search(name)
    if not m:
        return None  # college prospect
    return name[: m.start()].strip()


def _category_tags(game: dict) -> set[str]:
    cats = game.get("templateCategory", {}).get("dynamicCategories", [])
    return {str(c).lower() for c in cats}


def _is_award_game(game: dict) -> bool:
    return bool(_category_tags(game) & AWARD_CATEGORIES)


def _is_team_prop_game(game: dict) -> bool:
    return bool(_category_tags(game) & TEAM_FUTURE_CATEGORIES)


def _league_from_name(game_name: str) -> str:
    gl = game_name.lower()
    for league, code in MLB_LEAGUES.items():
        if league in gl:
            return code
    return ""


def _prop_type_from_award(game_name: str, tags: set[str], sport: str) -> Optional[str]:
    """Map a BetMGM award market to our normalized prop_type string."""
    gl = game_name.lower()
    league = _league_from_name(game_name)
    if sport == "mlb":
        if "mvp" in tags or "most valuable" in gl or gl.startswith("mvp"):
            return f"mvp_{league}" if league else "mvp"
        if "cy young" in gl or "cyyoung" in tags or "cy" in tags:
            return f"cy_young_{league}" if league else "cy_young"
        if "rookie" in gl or "roy" in tags:
            return f"rookie_{league}" if league else "rookie"
        if "manager" in gl or "moty" in tags or "coy" in tags or "coach" in gl:
            return "manager_of_year"
        if "batting title" in gl:
            return "batting_title"
        return None
    if sport in ("nfl", "nba"):
        if "mvp" in tags or "most valuable" in gl or "mvp" in gl.split():
            if "offensive" in gl and "defensive" not in gl:
                return "opoy"
            return "mvp"
        if "rookie" in gl or "roy" in tags:
            return "roty"
        if "offensive player" in gl:
            return "opoy"
        if "defensive player" in gl or "dpoy" in tags:
            return "dpoy"
        if "coach" in gl or "coy" in tags:
            return "coy"
        if "most improved" in gl or "mip" in tags:
            return "mip"
        if "sixth man" in gl or "sixthman" in tags:
            return "sixth_man"
        if "comeback" in gl or "cpoy" in tags:
            return "cpoy"
        return None
    return None


def _parse_win_total(game_name: str) -> Optional[Decimal]:
    """Extract the numeric win-total threshold from a market name like
    'To Win 9.5 Games' / 'Team WIN TOTAL - 9.5' / 'Regular Season Wins'..."""
    m = re.search(r"(\d+(?:\.\d+)?)", game_name)
    if not m:
        return None
    return Decimal(m.group(1))


def _classify_team_game(game_name: str) -> tuple[Optional[str], Optional[str], bool]:
    """Classify a team-futures market into (field, team_name, is_main_champ).

    Team name is derived from the leading part of the game name for markets that
    are per-team (win totals, playoffs), e.g.
    'Dallas Cowboys: Regular season wins' -> ('win_total', 'Dallas Cowboys', False).
    For one-for-all markets (Super Bowl / division / conference winner) the results
    hold the teams, so team_name here is only the market label (unused for those).
    Returns (None, None, False) if the market isn't a team-future market.
    """
    gl = game_name.lower()
    # Split the team (everything before ':' or a trailing market phrase)
    team = None
    if ":" in game_name:
        team = game_name.split(":", 1)[0].strip()
    # detect market type
    is_main_champ = False
    if any(w in gl for w in ["playoff", "make the playoffs", "to make the playoffs"]):
        return "playoffs", (team or game_name.strip()), False
    if any(w in gl for w in ["win total", "regular season wins", "season wins", "wins over", "wins under"]):
        return "win_total", (team or game_name.strip()), False
    # main title markets (Super Bowl / Finals / World Series) -> championship_odds
    for main in ["super bowl", " world series " , " finals ", "to win the world series", "to win the super bowl", "to win the finals"]:
        if main.strip() in gl:
            is_main_champ = True
            break
    if any(w in gl for w in ["champion", "winner", "title", "to win it all", "to lift", "pennant"]):
        return "championship", (team or game_name.strip()), is_main_champ
    return None, None, False


def _fixtures_url(accessid: str, sport_id: int) -> str:
    """Build the CDS fixtures API URL for a sport."""
    return (
        f"{BASE}/cds-api/bettingoffer/fixtures"
        f"?x-bwin-accessid={accessid}"
        f"&lang=en-us&country=US&userCountry=US&subdivision=US-Texas"
        f"&fixtureTypes=Standard&state=Latest&offerMapping=All"
        f"&offerCategories=AllOffers&fixtureCategories=All"
        f"&statisticsModes=Pitchers,SeasonStandings&take=1000&sortBy=Tags"
        f"&sportIds={sport_id}&includeMarkets=true"
    )


async def fetch_sport_fixtures(page, accessid: str, sport_id: int) -> list[dict]:
    """Fetch the CDS fixtures JSON for a sport via in-page fetch.

    Must run inside a browser session (carries Cloudflare cookies).
    Returns the parsed JSON body (top-level dict).
    """
    url = _fixtures_url(accessid, sport_id)
    body = await page.evaluate("""async (u) => {
        const res = await fetch(u, {headers: {'accept': 'application/json'}});
        return await res.text();
    }""", url)
    return json.loads(body)


def parse_sport_fixtures(config: SportScrapeConfig, accessid: str,
                         data: dict) -> tuple[list[TeamProp], list[PlayerSeasonProp]]:
    """Parse the CDS fixtures response for one sport into normalized models.

    ``data`` is the full JSON dict returned by fetch_sport_fixtures (top-level
    with a "fixtures" array). Passing the full response lets us ground league
    filtering on fixture names (BetMGM returns NCAAF/NCAAB in the same sport
    response for NFL/NBA).

    Returns (team_props, player_season_props).
    """
    sport = config.name
    season = config.season_year
    book = "betmgm"

    fixtures = data.get("fixtures", []) or []
    per_team: dict[str, dict] = {}
    season_props: list[PlayerSeasonProp] = []
    win_sides: dict[str, list] = defaultdict(list)  # team -> list of (line, 'over'|'under', odds)

    def team_key(tname: str):
        return tname

    for fixt in fixtures:
        fname_raw = fixt.get("name")
        fname = fname_raw.get("value") if isinstance(fname_raw, dict) else (fname_raw or "")
        fl = fname.lower()
        # Exclude college games that leak into the NFL/NBA sport response.
        if any(w in fl for w in ["college football", "ncaaf", "college basketball", "ncaab", "college"]):
            continue
        for game in (fixt.get("games") or []):
            gname_raw = game.get("name")
            gname = gname_raw.get("value") if isinstance(gname_raw, dict) else (gname_raw or "")
            results = game.get("results") or []
            if not gname or not results:
                continue
            tags = _category_tags(game)

            # ---- Player awards ----
            if _is_award_game(game):
                base_pt = _prop_type_from_award(gname, tags, sport)
                if not base_pt:
                    continue
                for r in results:
                    pname = _filter_nfl_award_player(_player_name(r) or "", sport, base_pt)
                    if not pname:
                        continue
                    odds = _american_odds(r)
                    if odds is None:
                        continue
                    season_props.append(PlayerSeasonProp(
                        sport=sport, season_year=season, player_name=pname,
                        team_name=None, prop_type=base_pt, bookmaker=book, odds=odds,
                    ))
                continue

            # ---- Team props (only if the market looks like a team market) ----
            if not _is_team_prop_game(game):
                continue
            if "parlay" in gname.lower():
                continue  # skip parlay combos (e.g. NFL 'to make the playoffs parlay')
            cls, team, is_main_champ = _classify_team_game(gname)
            if not cls:
                continue
            # Per-team playoff markets need a colon-split team (Yes/No results);
            # a no-colon playoff market is a league-wide parlay -> skip.
            if cls == "playoffs" and (":" not in gname):
                continue

            if cls == "championship":
                # One market = many result-teams (contenders). Each result team
                # gets championship_odds. Only the MAIN title counts for
                # championship_odds (Super Bowl / Finals / World Series);
                # conference/division winners have no schema column -> skip.
                if not is_main_champ:
                    continue
                for r in results:
                    tname_rcanon = _canonical_team_name(sport, _player_name(r) or "")
                    if not tname_rcanon:
                        continue
                    odds = _american_odds(r)
                    if odds is None:
                        continue
                    row = per_team.setdefault(tname_rcanon, {
                        "season_year": season, "team_name": tname_rcanon,
                        "championship_odds": None, "make_playoffs_odds": None,
                        "miss_playoffs_odds": None, "win_total": None,
                        "win_total_over_odds": None, "win_total_under_odds": None,
                    })
                    row["championship_odds"] = odds
                continue

            # Playoffs / win totals: one team per market (team in game name).
            if not team:
                continue
            team = _canonical_team_name(sport, team)
            if not team:
                continue
            if team not in per_team:
                per_team[team] = {
                    "season_year": season, "team_name": team,
                    "championship_odds": None, "make_playoffs_odds": None,
                    "miss_playoffs_odds": None, "win_total": None,
                    "win_total_over_odds": None, "win_total_under_odds": None,
                }
            row = per_team[team]
            if cls == "playoffs":
                # results are Yes / No (or Yes=make playoffs, No=miss)
                for r in results:
                    rn = (_player_name(r) or "").lower()
                    odds = _american_odds(r)
                    if odds is None:
                        continue
                    if rn in ("yes", "make", "make it", "make playoffs"):
                        row["make_playoffs_odds"] = odds
                    elif rn in ("no", "miss", "miss it", "miss playoffs"):
                        row["miss_playoffs_odds"] = odds
            elif cls == "win_total":
                # each result is one side: "Over 11.5" / "Under 11.5"; a team may
                # have many lines across separate games -> collect, pick main line.
                for r in results:
                    rn = (_player_name(r) or "").lower()
                    odds = _american_odds(r)
                    if odds is None:
                        continue
                    m = re.search(r"(over|under)\s*(\d+(?:\.\d+)?)", rn)
                    if m:
                        side = m.group(1)
                        line = Decimal(m.group(2))
                        win_sides[team].append((line, side, odds))

    # ---- Finalize win totals: pick the main line (closest to even) per team ----
    for team, sides in win_sides.items():
        row = per_team[team]
        # group sides by line
        lines: dict[Decimal, dict] = defaultdict(dict)
        for line, side, odds in sides:
            lines[line][side] = odds
        if not lines:
            continue
        # choose the line closest to even odds (|odds| ~ 100 implies vig-centered main line)
        best_line = None
        best_score = None
        for line, sd in lines.items():
            ov, un = sd.get("over"), sd.get("under")
            if ov is None or un is None:
                continue
            score = min(abs(ov), abs(un))
            if best_score is None or score < best_score:
                best_score = score
                best_line = line
        if best_line is None:
            # no complete over/under pair; take the most common line
            best_line = Counter(l for l, s, o in sides).most_common(1)[0][0]
        row["win_total"] = best_line
        row["win_total_over_odds"] = lines[best_line].get("over")
        row["win_total_under_odds"] = lines[best_line].get("under")

    team_props = [TeamProp(sport=sport, bookmaker=book, **v) for v in per_team.values()]
    return team_props, season_props
