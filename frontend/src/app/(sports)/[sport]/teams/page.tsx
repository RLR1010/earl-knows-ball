import Image from "next/image";
import Link from "next/link";
import { getTeamLogoUrl } from "@/lib/team_logos";

const NFL_TEAMS = [
  { abbr: "ARI", name: "Cardinals", conf: "NFC", div: "West" },
  { abbr: "ATL", name: "Falcons", conf: "NFC", div: "South" },
  { abbr: "BAL", name: "Ravens", conf: "AFC", div: "North" },
  { abbr: "BUF", name: "Bills", conf: "AFC", div: "East" },
  { abbr: "CAR", name: "Panthers", conf: "NFC", div: "South" },
  { abbr: "CHI", name: "Bears", conf: "NFC", div: "North" },
  { abbr: "CIN", name: "Bengals", conf: "AFC", div: "North" },
  { abbr: "CLE", name: "Browns", conf: "AFC", div: "North" },
  { abbr: "DAL", name: "Cowboys", conf: "NFC", div: "East" },
  { abbr: "DEN", name: "Broncos", conf: "AFC", div: "West" },
  { abbr: "DET", name: "Lions", conf: "NFC", div: "North" },
  { abbr: "GB", name: "Packers", conf: "NFC", div: "North" },
  { abbr: "HOU", name: "Texans", conf: "AFC", div: "South" },
  { abbr: "IND", name: "Colts", conf: "AFC", div: "South" },
  { abbr: "JAX", name: "Jaguars", conf: "AFC", div: "South" },
  { abbr: "KC", name: "Chiefs", conf: "AFC", div: "West" },
  { abbr: "LAC", name: "Chargers", conf: "AFC", div: "West" },
  { abbr: "LAR", name: "Rams", conf: "NFC", div: "West" },
  { abbr: "LV", name: "Raiders", conf: "AFC", div: "West" },
  { abbr: "MIA", name: "Dolphins", conf: "AFC", div: "East" },
  { abbr: "MIN", name: "Vikings", conf: "NFC", div: "North" },
  { abbr: "NE", name: "Patriots", conf: "AFC", div: "East" },
  { abbr: "NO", name: "Saints", conf: "NFC", div: "South" },
  { abbr: "NYG", name: "Giants", conf: "NFC", div: "East" },
  { abbr: "NYJ", name: "Jets", conf: "AFC", div: "East" },
  { abbr: "PHI", name: "Eagles", conf: "NFC", div: "East" },
  { abbr: "PIT", name: "Steelers", conf: "AFC", div: "North" },
  { abbr: "SEA", name: "Seahawks", conf: "NFC", div: "West" },
  { abbr: "SF", name: "49ers", conf: "NFC", div: "West" },
  { abbr: "TB", name: "Buccaneers", conf: "NFC", div: "South" },
  { abbr: "TEN", name: "Titans", conf: "AFC", div: "South" },
  { abbr: "WAS", name: "Commanders", conf: "NFC", div: "East" },
];

const NBA_TEAMS = [
  { abbr: "ATL", name: "Hawks", conf: "Eastern", div: "Southeast" },
  { abbr: "BOS", name: "Celtics", conf: "Eastern", div: "Atlantic" },
  { abbr: "BKN", name: "Nets", conf: "Eastern", div: "Atlantic" },
  { abbr: "CHA", name: "Hornets", conf: "Eastern", div: "Southeast" },
  { abbr: "CHI", name: "Bulls", conf: "Eastern", div: "Central" },
  { abbr: "CLE", name: "Cavaliers", conf: "Eastern", div: "Central" },
  { abbr: "DAL", name: "Mavericks", conf: "Western", div: "Southwest" },
  { abbr: "DEN", name: "Nuggets", conf: "Western", div: "Northwest" },
  { abbr: "DET", name: "Pistons", conf: "Eastern", div: "Central" },
  { abbr: "GSW", name: "Warriors", conf: "Western", div: "Pacific" },
  { abbr: "HOU", name: "Rockets", conf: "Western", div: "Southwest" },
  { abbr: "IND", name: "Pacers", conf: "Eastern", div: "Central" },
  { abbr: "LAC", name: "Clippers", conf: "Western", div: "Pacific" },
  { abbr: "LAL", name: "Lakers", conf: "Western", div: "Pacific" },
  { abbr: "MEM", name: "Grizzlies", conf: "Western", div: "Southwest" },
  { abbr: "MIA", name: "Heat", conf: "Eastern", div: "Southeast" },
  { abbr: "MIL", name: "Bucks", conf: "Eastern", div: "Central" },
  { abbr: "MIN", name: "Timberwolves", conf: "Western", div: "Northwest" },
  { abbr: "NOP", name: "Pelicans", conf: "Western", div: "Southwest" },
  { abbr: "NYK", name: "Knicks", conf: "Eastern", div: "Atlantic" },
  { abbr: "OKC", name: "Thunder", conf: "Western", div: "Northwest" },
  { abbr: "ORL", name: "Magic", conf: "Eastern", div: "Southeast" },
  { abbr: "PHI", name: "76ers", conf: "Eastern", div: "Atlantic" },
  { abbr: "PHX", name: "Suns", conf: "Western", div: "Pacific" },
  { abbr: "POR", name: "Trail Blazers", conf: "Western", div: "Northwest" },
  { abbr: "SAC", name: "Kings", conf: "Western", div: "Pacific" },
  { abbr: "SAS", name: "Spurs", conf: "Western", div: "Southwest" },
  { abbr: "TOR", name: "Raptors", conf: "Eastern", div: "Atlantic" },
  { abbr: "UTA", name: "Jazz", conf: "Western", div: "Northwest" },
  { abbr: "WAS", name: "Wizards", conf: "Eastern", div: "Southeast" },
];

const MLB_TEAMS = [
  { abbr: "ARI", name: "Diamondbacks", conf: "NL", div: "West" },
  { abbr: "ATL", name: "Braves", conf: "NL", div: "East" },
  { abbr: "BAL", name: "Orioles", conf: "AL", div: "East" },
  { abbr: "BOS", name: "Red Sox", conf: "AL", div: "East" },
  { abbr: "CHC", name: "Cubs", conf: "NL", div: "Central" },
  { abbr: "CIN", name: "Reds", conf: "NL", div: "Central" },
  { abbr: "CLE", name: "Guardians", conf: "AL", div: "Central" },
  { abbr: "COL", name: "Rockies", conf: "NL", div: "West" },
  { abbr: "CWS", name: "White Sox", conf: "AL", div: "Central" },
  { abbr: "DET", name: "Tigers", conf: "AL", div: "Central" },
  { abbr: "HOU", name: "Astros", conf: "AL", div: "West" },
  { abbr: "KC", name: "Royals", conf: "AL", div: "Central" },
  { abbr: "LAA", name: "Angels", conf: "AL", div: "West" },
  { abbr: "LAD", name: "Dodgers", conf: "NL", div: "West" },
  { abbr: "MIA", name: "Marlins", conf: "NL", div: "East" },
  { abbr: "MIL", name: "Brewers", conf: "NL", div: "Central" },
  { abbr: "MIN", name: "Twins", conf: "AL", div: "Central" },
  { abbr: "NYM", name: "Mets", conf: "NL", div: "East" },
  { abbr: "NYY", name: "Yankees", conf: "AL", div: "East" },
  { abbr: "OAK", name: "Athletics", conf: "AL", div: "West" },
  { abbr: "PHI", name: "Phillies", conf: "NL", div: "East" },
  { abbr: "PIT", name: "Pirates", conf: "NL", div: "Central" },
  { abbr: "SD", name: "Padres", conf: "NL", div: "West" },
  { abbr: "SEA", name: "Mariners", conf: "AL", div: "West" },
  { abbr: "SF", name: "Giants", conf: "NL", div: "West" },
  { abbr: "STL", name: "Cardinals", conf: "NL", div: "Central" },
  { abbr: "TB", name: "Rays", conf: "AL", div: "East" },
  { abbr: "TEX", name: "Rangers", conf: "AL", div: "West" },
  { abbr: "TOR", name: "Blue Jays", conf: "AL", div: "East" },
  { abbr: "WAS", name: "Nationals", conf: "NL", div: "East" },
];

function groupBy<T>(items: T[], key: (item: T) => string): Record<string, T[]> {
  const groups: Record<string, T[]> = {};
  for (const item of items) {
    const k = key(item);
    if (!groups[k]) groups[k] = [];
    groups[k].push(item);
  }
  return groups;
}

const LOGO_SUFFIX: Record<string, string> = {
  // MLB teams with different logo naming
  CHC: "CHC", CWS: "CWS", LAA: "LAA", LAD: "LAD",
  NYM: "NYM", NYY: "NYY", OAK: "OAK", SD: "SD",
  SF: "SF", STL: "STL", TB: "TB", TEX: "TEX",
  TOR: "TOR", WAS: "WAS",
  // NBA/NFL use same abbreviation pattern
};

export default async function TeamsPage({ params }: { params: Promise<{ sport: string }> }) {
  const { sport } = await params;

  let teams: { abbr: string; name: string; conf: string; div: string }[];
  let order: string[];

  if (sport === "nba") {
    teams = NBA_TEAMS;
    order = [
      "Eastern Atlantic", "Eastern Central", "Eastern Southeast",
      "Western Northwest", "Western Pacific", "Western Southwest",
    ];
  } else if (sport === "mlb") {
    teams = MLB_TEAMS;
    order = [
      "AL East", "AL Central", "AL West",
      "NL East", "NL Central", "NL West",
    ];
  } else {
    teams = NFL_TEAMS;
    order = [
      "NFC East", "NFC North", "NFC South", "NFC West",
      "AFC East", "AFC North", "AFC South", "AFC West",
    ];
  }

  const byConfDiv = groupBy(teams, (t) => `${t.conf} ${t.div}`);

  return (
    <div className="space-y-8">
      <h1 className="font-display text-4xl font-bold">
        {sport.toUpperCase()} Teams
      </h1>
      <div className="grid md:grid-cols-2 gap-6">
        {order.map((key) => {
          const groupTeams = byConfDiv[key];
          if (!groupTeams) return null;
          return (
            <div key={key} className="border border-white/10 rounded-xl p-4 bg-white/5">
              <h2 className="text-sm font-semibold text-earl-400 uppercase tracking-wider mb-3">
                {key}
              </h2>
              <div className="space-y-1">
                {groupTeams.map((t) => (
                  <Link
                    key={t.abbr}
                    href={`/${sport}/teams/${t.abbr.toLowerCase()}`}
                    className="flex items-center gap-3 px-3 py-2 rounded-lg hover:bg-white/10 transition group"
                  >
                    <span className="w-8 h-8 rounded-full bg-white/10 flex items-center justify-center overflow-hidden">
                      {sport === "nfl" ? (
                        <Image
                          src={`/logos/${t.abbr}.png`}
                          alt={t.name}
                          width={32}
                          height={32}
                          className="object-contain"
                        />
                      ) : (
                        <img
                          src={getTeamLogoUrl(t.abbr, sport)}
                          alt={t.name}
                          width={32}
                          height={32}
                          className="object-contain"
                          style={{ filter: 'brightness(1.1)' }}
                        />
                      )}
                    </span>
                    <span className="font-medium group-hover:text-earl-400 transition">
                      {t.name}
                    </span>
                    <span className="text-xs text-gray-600 ml-auto">{t.abbr}</span>
                  </Link>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      {teams.length > 0 && teams[0] && (
        <div className="text-center text-xs text-gray-600 pb-4">
          {teams.length} teams
        </div>
      )}
    </div>
  );
}
