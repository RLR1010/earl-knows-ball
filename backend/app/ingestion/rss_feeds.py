"""
Shared RSS feed definitions with team mappings for all three sports.

Each sport has:
  - RSS_FEEDS_{SPORT}: {source_name: url} dict (same as articles*.py)
  - FEED_TEAM_MAP_{SPORT}: {source_name: abbreviation} for team-specific feeds
  - TEAM_FEEDS_{SPORT}: {abbreviation: [{name, url}]} reverse map
  - GENERAL_FEEDS_{SPORT}: [{name, url}] for league-wide sources
"""
from app.ingestion.articles import RSS_FEEDS
from app.ingestion.articles_nba import RSS_FEEDS_NBA
from app.ingestion.articles_mlb import RSS_FEEDS_MLB

# ── NFL Feed → Team Mapping ──────────────────────────────────────────
NFL_TEAM_MAP: dict[str, str] = {
    # SB Nation
    # SB Nation
    "Bills (Buffalo Rumblings)": "BUF",
    "Dolphins (The Phinsider)": "MIA",
    "Patriots (Pats Pulpit)": "NE",
    "Jets (Gang Green Nation)": "NYJ",
    "Ravens (Baltimore Beatdown)": "BAL",
    "Bengals (Cincy Jungle)": "CIN",
    "Browns (Dawgsports)": "CLE",
    "Steelers (Behind the Steel Curtain)": "PIT",
    "Texans (Battle Red Blog)": "HOU",
    "Colts (Stampede Blue)": "IND",
    "Jaguars (Big Cat Country)": "JAX",
    "Titans (Music City Miracles)": "TEN",
    "Broncos (Mile High Report)": "DEN",
    "Chiefs (Arrowhead Pride)": "KC",
    "Raiders (Silver & Black Pride)": "LV",
    "Chargers (Bolts from the Blue)": "LAC",
    "Cowboys (Blogging the Boys)": "DAL",
    "Giants (Big Blue View)": "NYG",
    "Eagles (Bleeding Green Nation)": "PHI",
    "Commanders (Hogs Haven)": "WAS",
    "Bears (Windy City Gridiron)": "CHI",
    "Lions (Pride of Detroit)": "DET",
    "Packers (Acme Packing Company)": "GB",
    "Vikings (Daily Norseman)": "MIN",
    "Falcons (The Falcoholic)": "ATL",
    "Panthers (Cat Scratch Reader)": "CAR",
    "Saints (Canal Street Chronicles)": "NO",
    "Buccaneers (Bucs Nation)": "TB",
    "Cardinals (Revenge of the Birds)": "ARI",
    "Rams (Turf Show Times)": "LAR",
    "49ers (Niners Nation)": "SF",
    "Seahawks (Field Gulls)": "SEA",
    # FanSided
    "FanSided Bills (BuffaLowDown)": "BUF",
    "FanSided Dolphins (Phin Phanatic)": "MIA",
    "FanSided Patriots (Musket Fire)": "NE",
    "FanSided Jets (The Jet Press)": "NYJ",
    "FanSided Ravens (Ebony Bird)": "BAL",
    "FanSided Bengals (Stripe Hype)": "CIN",
    "FanSided Browns (Dawg Pound Daily)": "CLE",
    "FanSided Steelers (Still Curtain)": "PIT",
    "FanSided Texans (Toro Times)": "HOU",
    "FanSided Colts (Horseshoe Heroes)": "IND",
    "FanSided Jaguars (Black and Teal)": "JAX",
    "FanSided Titans (Titan Sized)": "TEN",
    "FanSided Broncos (Predominantly Orange)": "DEN",
    "FanSided Chiefs (Arrowhead Addict)": "KC",
    "FanSided Raiders (Just Blog Baby)": "LV",
    "FanSided Chargers (Bolt Beat)": "LAC",
    "FanSided Cowboys (The Landry Hat)": "DAL",
    "FanSided Giants (GMEN HQ)": "NYG",
    "FanSided Eagles (Inside the Iggles)": "PHI",
    "FanSided Commanders (Riggo's Rag)": "WAS",
    "FanSided Bears (Bear Goggles On)": "CHI",
    "FanSided Lions (SideLion Report)": "DET",
    "FanSided Packers (Lombardi Ave)": "GB",
    "FanSided Vikings (The Viking Age)": "MIN",
    "FanSided Falcons (Blogging Dirty)": "ATL",
    "FanSided Panthers (Cat Crave)": "CAR",
    "FanSided Saints (Who Dat Dish)": "NO",
    "FanSided Buccaneers (The Pewter Plank)": "TB",
    "FanSided Cardinals (Raising Zona)": "ARI",
    "FanSided Rams (Ramblin' Fan)": "LAR",
    "FanSided 49ers (Niner Noise)": "SF",
    "FanSided Seahawks (12th Man Rising)": "SEA",
}

# ── NBA Feed → Team Mapping ──────────────────────────────────────────
NBA_TEAM_MAP: dict[str, str] = {
    # SB Nation
    "Bucks (Brew Hoop)": "MIL",
    "Bulls (Blog a Bull)": "CHI",
    "Cavaliers (Fear the Sword)": "CLE",
    "Celtics (CelticsBlog)": "BOS",
    "Clippers (Clips Nation)": "LAC",
    "Grizzlies (Grizzly Bear Blues)": "MEM",
    "Hawks (Peachtree Hoops)": "ATL",
    "Heat (Hot Hot Hoops)": "MIA",
    "Hornets (At the Hive)": "CHA",
    "Jazz (SLC Dunk)": "UTA",
    "Kings (Sactown Royalty)": "SAC",
    "Knicks (Posting and Toasting)": "NYK",
    "Lakers (Silver Screen and Roll)": "LAL",
    "Magic (Orlando Pinstriped Post)": "ORL",
    "Mavericks (Mavs Moneyball)": "DAL",
    "Nets (Nets Daily)": "BKN",
    "Nuggets (Denver Stiffs)": "DEN",
    "Pacers (Indy Cornrows)": "IND",
    "Pelicans (The Bird Writes)": "NOP",
    "Pistons (Detroit Bad Boys)": "DET",
    "Raptors (Raptors HQ)": "TOR",
    "Rockets (The Dream Shake)": "HOU",
    "Sixers (Liberty Ballers)": "PHI",
    "Spurs (Pounding the Rock)": "SAS",
    "Suns (Bright Side of the Sun)": "PHX",
    "Thunder (Welcome to Loud City)": "OKC",
    "Timberwolves (Canis Hoopus)": "MIN",
    "Trail Blazers (Blazers Edge)": "POR",
    "Warriors (Golden State of Mind)": "GSW",
    "Wizards (Bullets Forever)": "WAS",
    # FanSided
    "FanSided Hawks (Soaring Down South)": "ATL",
    "FanSided Celtics (Hardwood Houdini)": "BOS",
    "FanSided Hornets (Swarm and Sting)": "CHA",
    "FanSided Bulls (Pippen Ain't Easy)": "CHI",
    "FanSided Cavaliers (King James Gospel)": "CLE",
    "FanSided Pistons (PistonPowered)": "DET",
    "FanSided Pacers (8 Points, 9 Seconds)": "IND",
    "FanSided Heat (All U Can Heat)": "MIA",
    "FanSided Bucks (Behind the Buck Pass)": "MIL",
    "FanSided Knicks (Daily Knicks)": "NYK",
    "FanSided Magic (Orlando Magic Daily)": "ORL",
    "FanSided 76ers (The Sixer Sense)": "PHI",
    "FanSided Raptors (Raptors Rapture)": "TOR",
    "FanSided Wizards (Wiz of Awes)": "WAS",
    "FanSided Mavericks (The Smoking Cuban)": "DAL",
    "FanSided Nuggets (Nugg Love)": "DEN",
    "FanSided Warriors (Blue Man Hoop)": "GSW",
    "FanSided Rockets (Space City Scoop)": "HOU",
    "FanSided Clippers (Clipperholics)": "LAC",
    "FanSided Lakers (Lake Show Life)": "LAL",
    "FanSided Grizzlies (Beale Street Bears)": "MEM",
    "FanSided Timberwolves (Dunking with Wolves)": "MIN",
    "FanSided Pelicans (Pelican Debrief)": "NOP",
    "FanSided Thunder (Thunderous Intentions)": "OKC",
    "FanSided Suns (Valley of the Suns)": "PHX",
    "FanSided Trail Blazers (Rip City Project)": "POR",
    "FanSided Kings (A Royal Pain)": "SAC",
    "FanSided Spurs (Air Alamo)": "SAS",
    "FanSided Jazz (The J-Notes)": "UTA",
}

# ── MLB Feed → Team Mapping ──────────────────────────────────────────
MLB_TEAM_MAP: dict[str, str] = {
    # SB Nation
    "Angels (Halos Heaven)": "LAA",
    "Astros (Crawfish Boxes)": "HOU",
    "Athletics (Athletics Nation)": "OAK",
    "Blue Jays (Bluebird Banter)": "TOR",
    "Braves (Battery Power)": "ATL",
    "Brewers (Brew Crew Ball)": "MIL",
    "Cardinals (Viva El Birdos)": "STL",
    "Cubs (Bleed Cubbie Blue)": "CHC",
    "D-backs (AZ Snake Pit)": "ARI",
    "Dodgers (True Blue LA)": "LAD",
    "Giants (McCovey Chronicles)": "SF",
    "Guardians (Covering the Corner)": "CLE",
    "Mariners (Lookout Landing)": "SEA",
    "Marlins (Fish Stripes)": "MIA",
    "Mets (Amazin' Avenue)": "NYM",
    "Nationals (Federal Baseball)": "WAS",
    "Orioles (Camden Chat)": "BAL",
    "Padres (Gaslamp Ball)": "SD",
    "Phillies (The Good Phight)": "PHI",
    "Pirates (Bucs Dugout)": "PIT",
    "Rangers (Lone Star Ball)": "TEX",
    "Rays (DRaysBay)": "TB",
    "Red Sox (Over the Monster)": "BOS",
    "Reds (Red Reporter)": "CIN",
    "Rockies (Purple Row)": "COL",
    "Royals (Royals Review)": "KC",
    "Tigers (Bless You Boys)": "DET",
    "Twins (Twinkie Town)": "MIN",
    "White Sox (South Side Sox)": "CWS",
    "Yankees (Pinstripe Alley)": "NYY",
    # FanSided
    "FanSided Orioles (Birds Watcher)": "BAL",
    "FanSided Red Sox (BoSox Injection)": "BOS",
    "FanSided White Sox (Southside Showdown)": "CWS",
    "FanSided Guardians (Away Back Gone)": "CLE",
    "FanSided Tigers (Motor City Bengals)": "DET",
    "FanSided Astros (Climbing Tal's Hill)": "HOU",
    "FanSided Royals (Kings of Kauffman)": "KC",
    "FanSided Angels (Halo Hangout)": "LAA",
    "FanSided Twins (Puckett's Pond)": "MIN",
    "FanSided Yankees (Yanks Go Yard)": "NYY",
    "FanSided Athletics (White Cleat Beat)": "OAK",
    "FanSided Mariners (SoDo Mojo)": "SEA",
    "FanSided Rays (Rays Colored Glasses)": "TB",
    "FanSided Rangers (Nolan Writin')": "TEX",
    "FanSided Blue Jays (Jays Journal)": "TOR",
    "FanSided D-backs (Venom Strikes)": "ARI",
    "FanSided Braves (House That Hank Built)": "ATL",
    "FanSided Cubs (Cubbies Crib)": "CHC",
    "FanSided Reds (Blog Red Machine)": "CIN",
    "FanSided Rockies (Rox Pile)": "COL",
    "FanSided Dodgers (Dodgers Way)": "LAD",
    "FanSided Marlins (Marlin Maniac)": "MIA",
    "FanSided Brewers (Reviewing the Brew)": "MIL",
    "FanSided Mets (Rising Apple)": "NYM",
    "FanSided Phillies (That Ball's Outta Here)": "PHI",
    "FanSided Pirates (Rum Bunter)": "PIT",
    "FanSided Padres (Friars on Base)": "SD",
    "FanSided Giants (Around the Foghorn)": "SF",
    "FanSided Cardinals (Redbird Rants)": "STL",
    "FanSided Nationals (District on Deck)": "WAS",
}

# ── General (League-Wide) Sources ────────────────────────────────────
# Sources that cover the whole league, not a single team.
NFL_GENERAL_NAMES = {
    "ESPN", "Yahoo Sports", "The Athletic", "SB Nation",
    "NFL Spin Zone", "Last Word on Sports", "ProFootballTalk", "Sportsnaut",
    "Fox Sports",
}
NBA_GENERAL_NAMES = {
    "ESPN", "Yahoo Sports", "CBS Sports", "NBC Sports", "The Athletic",
    "ClutchPoints", "Sportsnaut NBA", "BasketballNews.com", "SB Nation NBA",
    "Fox Sports", "FanSided NBA (Hoops Habit)",
}
MLB_GENERAL_NAMES = {
    "ESPN", "Yahoo Sports", "CBS Sports", "MLB.com", "The Athletic",
    "FanGraphs", "Baseball Prospectus", "Pitcher List", "MLB Trade Rumors",
    "Sportsnaut MLB", "SB Nation MLB",
    "Fox Sports", "FanSided MLB (Call to the Pen)",
}

# ── Build Feed Lists with Team Info ──────────────────────────────────

def _build_feeds(
    feeds_dict: dict[str, str],
    team_map: dict[str, str],
    general_names: set[str],
) -> tuple[list[dict], list[dict]]:
    """
    Separate feeds into team-specific and general lists.
    Returns (team_feeds, general_feeds) where each feed is {name, url, team?}.
    """
    team_feeds: list[dict] = []
    general_feeds: list[dict] = []

    for name, url in feeds_dict.items():
        if name in team_map:
            team_feeds.append({
                "name": name,
                "url": url,
                "team": team_map[name],
            })
        else:
            general_feeds.append({
                "name": name,
                "url": url,
                "team": None,
            })

    return team_feeds, general_feeds


NBA_TEAM_FEEDS, NBA_GENERAL_FEEDS = _build_feeds(RSS_FEEDS_NBA, NBA_TEAM_MAP, NBA_GENERAL_NAMES)
MLB_TEAM_FEEDS, MLB_GENERAL_FEEDS = _build_feeds(RSS_FEEDS_MLB, MLB_TEAM_MAP, MLB_GENERAL_NAMES)
NFL_TEAM_FEEDS, NFL_GENERAL_FEEDS = _build_feeds(RSS_FEEDS, NFL_TEAM_MAP, NFL_GENERAL_NAMES)


def get_feeds_for_sport(sport: str) -> tuple[list[dict], list[dict]]:
    """Return (team_feeds, general_feeds) for the given sport."""
    if sport == "nfl":
        return NFL_TEAM_FEEDS, NFL_GENERAL_FEEDS
    elif sport == "nba":
        return NBA_TEAM_FEEDS, NBA_GENERAL_FEEDS
    elif sport == "mlb":
        return MLB_TEAM_FEEDS, MLB_GENERAL_FEEDS
    raise ValueError(f"Unknown sport: {sport}")


def get_feeds_for_team(sport: str, abbreviation: str) -> list[dict]:
    """Get all RSS feeds that cover a specific team (team blogs only)."""
    team_feeds, _ = get_feeds_for_sport(sport)
    return [f for f in team_feeds if f["team"] == abbreviation.upper()]


def get_all_feeds(sport: str) -> list[dict]:
    """Get all feeds (team + general) sorted by name."""
    team_feeds, general_feeds = get_feeds_for_sport(sport)
    all_feeds = sorted(team_feeds + general_feeds, key=lambda f: f["name"].lower())
    return all_feeds


def get_teams_for_sport(sport: str) -> list[str]:
    """Get alphabetically sorted list of team abbreviations that have feeds."""
    team_feeds, _ = get_feeds_for_sport(sport)
    teams = sorted(set(f["team"] for f in team_feeds))
    return teams
