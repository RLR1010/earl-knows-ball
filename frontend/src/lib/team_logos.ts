/** Team logo URL lookup for all three sports. */

// MLB: statsapi.mlb.com team IDs → abbreviation
const MLB_TEAMS: Record<string, number> = {
  ARI: 109, ATL: 144, BAL: 110, BOS: 111, CHC: 112,
  CIN: 113, CLE: 114, COL: 115, CWS: 145, DET: 116,
  HOU: 117, KC: 118, LAA: 108, LAD: 119, MIA: 146,
  MIL: 158, MIN: 142, NYM: 121, NYY: 147, OAK: 133,
  PHI: 143, PIT: 134, SD: 135, SEA: 136, SF: 137,
  STL: 138, TB: 139, TEX: 140, TOR: 141, WAS: 120, WSH: 120,
};

// NBA: stats.nba.com team IDs → abbreviation
const NBA_TEAMS: Record<string, number> = {
  ATL: 1610612737, BOS: 1610612738, CLE: 1610612739, NOP: 1610612740,
  CHI: 1610612741, DAL: 1610612742, DEN: 1610612743, GSW: 1610612744,
  HOU: 1610612745, LAC: 1610612746, LAL: 1610612747, MIA: 1610612748,
  MIL: 1610612749, MIN: 1610612750, BKN: 1610612751, NYK: 1610612752,
  ORL: 1610612753, IND: 1610612754, PHI: 1610612755, PHX: 1610612756,
  POR: 1610612757, SAC: 1610612758, SAS: 1610612759, OKC: 1610612760,
  TOR: 1610612761, UTA: 1610612762, MEM: 1610612763, WAS: 1610612764,
  DET: 1610612765, CHA: 1610612766,
};

// Normalize NBA abbreviations returned by the game/team endpoints to the canonical
// logo-resolver keys (endpoints return GS/NY/SA/NO/WSH/UTAH/MEL etc.).
const NBA_ABBR_MAP: Record<string, string> = {
  GS: "GSW", NY: "NYK", SA: "SAS", NO: "NOP", WSH: "WAS",
  UTAH: "UTA", BK: "BKN", NOB: "NOP", PHO: "PHX",
  // also guard against raw stats-ids / misc spellings
  GSW: "GSW", NYK: "NYK", SAS: "SAS", NOP: "NOP", WAS: "WAS", UTA: "UTA",
};

// Normalize NFL abbreviations (e.g. ESPN uses WSH for Washington) to the
// canonical logo-file abbreviations (WAS).
const NFL_ABBR_MAP: Record<string, string> = {
  WSH: "WAS",
  // self-maps for safety
  WAS: "WAS", ARI: "ARI", ATL: "ATL", BAL: "BAL", BUF: "BUF", CAR: "CAR",
  CHI: "CHI", CIN: "CIN", CLE: "CLE", DAL: "DAL", DEN: "DEN", DET: "DET",
  GB: "GB", HOU: "HOU", IND: "IND", JAX: "JAX", KC: "KC", LAC: "LAC",
  LAR: "LAR", LV: "LV", MIA: "MIA", MIN: "MIN", NE: "NE", NO: "NO",
  NYG: "NYG", NYJ: "NYJ", PHI: "PHI", PIT: "PIT", SEA: "SEA", SF: "SF",
  TB: "TB", TEN: "TEN",
};

export function getTeamLogoUrl(abbr: string, sport: string): string | null {
  const upper = abbr.toUpperCase();
  if (sport === "nfl") {
    const key = NFL_ABBR_MAP[upper] ?? upper;
    return `/logos/${key}.png`;
  }
  if (sport === "mlb") {
    const id = MLB_TEAMS[upper];
    if (id) return `https://www.mlbstatic.com/team-logos/${id}.svg`;
    return null; // unknown team
  }
  if (sport === "nba") {
    const key = NBA_ABBR_MAP[upper] ?? upper;
    const id = NBA_TEAMS[key];
    if (id) return `https://cdn.nba.com/logos/nba/${id}/primary/L/logo.svg`;
    return null;
  }
  return null;
}
