/**
 * Team abbreviation -> full display name, used to build readable game-page
 * slugs in internal links. Must match the full names used by the backend
 * /seo/game-meta slugifier, so links point exactly at the canonical slug URL.
 *
 * Kept authoritative here for the three sports Earl covers.
 */

export const TEAM_FULL_NAMES: Record<string, Record<string, string>> = {
  mlb: {
    ARI: "Arizona Diamondbacks",
    ATL: "Atlanta Braves",
    BAL: "Baltimore Orioles",
    BOS: "Boston Red Sox",
    CHC: "Chicago Cubs",
    CWS: "Chicago White Sox",
    CIN: "Cincinnati Reds",
    CLE: "Cleveland Guardians",
    COL: "Colorado Rockies",
    DET: "Detroit Tigers",
    HOU: "Houston Astros",
    KC: "Kansas City Royals",
    LAA: "Los Angeles Angels",
    LAD: "Los Angeles Dodgers",
    MIA: "Miami Marlins",
    MIL: "Milwaukee Brewers",
    MIN: "Minnesota Twins",
    NYM: "New York Mets",
    NYY: "New York Yankees",
    OAK: "Oakland Athletics",
    PHI: "Philadelphia Phillies",
    PIT: "Pittsburgh Pirates",
    SD: "San Diego Padres",
    SF: "San Francisco Giants",
    SEA: "Seattle Mariners",
    STL: "St. Louis Cardinals",
    TB: "Tampa Bay Rays",
    TEX: "Texas Rangers",
    TOR: "Toronto Blue Jays",
    WSH: "Washington Nationals",
  },
  nfl: {
    ARI: "Arizona Cardinals",
    ATL: "Atlanta Falcons",
    BAL: "Baltimore Ravens",
    BUF: "Buffalo Bills",
    CAR: "Carolina Panthers",
    CHI: "Chicago Bears",
    CIN: "Cincinnati Bengals",
    CLE: "Cleveland Browns",
    DAL: "Dallas Cowboys",
    DEN: "Denver Broncos",
    DET: "Detroit Lions",
    GB: "Green Bay Packers",
    HOU: "Houston Texans",
    IND: "Indianapolis Colts",
    JAX: "Jacksonville Jaguars",
    KC: "Kansas City Chiefs",
    LV: "Las Vegas Raiders",
    LAC: "Los Angeles Chargers",
    LAR: "Los Angeles Rams",
    MIA: "Miami Dolphins",
    MIN: "Minnesota Vikings",
    NE: "New England Patriots",
    NO: "New Orleans Saints",
    NYG: "New York Giants",
    NYJ: "New York Jets",
    PHI: "Philadelphia Eagles",
    PIT: "Pittsburgh Steelers",
    SF: "San Francisco 49ers",
    SEA: "Seattle Seahawks",
    TB: "Tampa Bay Buccaneers",
    TEN: "Tennessee Titans",
    WAS: "Washington Commanders",
  },
  nba: {
    ATL: "Atlanta Hawks",
    BOS: "Boston Celtics",
    BKN: "Brooklyn Nets",
    CHA: "Charlotte Hornets",
    CHI: "Chicago Bulls",
    CLE: "Cleveland Cavaliers",
    DAL: "Dallas Mavericks",
    DEN: "Denver Nuggets",
    DET: "Detroit Pistons",
    GSW: "Golden State Warriors",
    HOU: "Houston Rockets",
    IND: "Indiana Pacers",
    LAC: "LA Clippers",
    LAL: "Los Angeles Lakers",
    MEM: "Memphis Grizzlies",
    MIA: "Miami Heat",
    MIL: "Milwaukee Bucks",
    MIN: "Minnesota Timberwolves",
    NO: "New Orleans Pelicans",
    NOP: "New Orleans Pelicans",
    NYK: "New York Knicks",
    OKC: "Oklahoma City Thunder",
    ORL: "Orlando Magic",
    PHI: "Philadelphia 76ers",
    PHX: "Phoenix Suns",
    POR: "Portland Trail Blazers",
    SAC: "Sacramento Kings",
    SA: "San Antonio Spurs",
    SAS: "San Antonio Spurs",
    TOR: "Toronto Raptors",
    UTA: "Utah Jazz",
    UTAH: "Utah Jazz",
    WSH: "Washington Wizards",
    WAS: "Washington Wizards",
  },
};

/** Mirror of the backend slugify: lowercase, drop punctuation, dashes. */
export function slugifyTeam(name: string): string {
  return name
    .replace(/[^\w\s-]/g, "") // drop punctuation incl. periods (St. -> St)
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "-")
    .replace(/-+$/, "");
}

/**
 * Build the canonical game-page slug from the schedule card's fields.
 * Returns null if team names or date can't be resolved (caller falls back to
 * the numeric id URL, which the server will still redirect to canonical).
 */
export function buildGameSlug(
  sport: "mlb" | "nfl" | "nba",
  homeAbbr: string,
  awayAbbr: string,
  dateIso: string,
  gameId: number | string
): string | null {
  const map = TEAM_FULL_NAMES[sport];
  const home = map[homeAbbr?.toUpperCase()];
  const away = map[awayAbbr?.toUpperCase()];
  const dateStr = String(dateIso || "").slice(0, 10); // YYYY-MM-DD
  if (!home || !away || !dateStr) return null;
  return `${slugifyTeam(home)}-vs-${slugifyTeam(away)}-${dateStr}-${gameId}`;
}
