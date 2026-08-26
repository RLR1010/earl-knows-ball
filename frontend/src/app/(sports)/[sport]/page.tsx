import type { Metadata } from "next";
import SportUpcomingGames from "@/components/SportUpcomingGames";
import RecentContent from "@/components/RecentContent";
import DailyPicksSection from "@/components/DailyPicksSection";
import BestBetsPanel from "@/components/BestBetsPanel";
import StandingsWidget from "@/components/StandingsWidget";
import type { CardSport } from "@/components/ScheduleGameCard";

const SPORT_INFO: Record<string, { name: string }> = {
  nfl: { name: "NFL" },
  nba: { name: "NBA" },
  mlb: { name: "MLB" },
};

const SPORT_META: Record<string, { desc: string; kw: string[] }> = {
  nfl: {
    desc: "NFL picks, betting lines, spreads, over/under predictions, and AI handicapping. Earl Knows Ball covers every game with data-backed analysis.",
    kw: ["NFL picks", "NFL betting", "football spread predictions", "NFL over under", "NFL AI handicapper"],
  },
  nba: {
    desc: "NBA picks, betting lines, spreads, over/under predictions, and AI handicapping. Earl Knows Ball breaks down every basketball matchup.",
    kw: ["NBA picks", "NBA betting", "basketball spread predictions", "NBA over under", "NBA AI handicapper"],
  },
  mlb: {
    desc: "MLB picks, betting lines, run lines, over/under predictions, and AI handicapping. Earl Knows Ball handicaps every baseball game.",
    kw: ["MLB picks", "MLB betting", "baseball predictions", "MLB run line", "MLB AI handicapper"],
  },
};

const VALID_SPORTS = ["nfl", "nba", "mlb"];

export async function generateMetadata({ params }: { params: Promise<{ sport: string }> }): Promise<Metadata> {
  const { sport } = await params;
  const name = (SPORT_INFO[sport]?.name || sport?.toUpperCase() || "Sports").toUpperCase();
  const meta = SPORT_META[sport] || {
    desc: "Sports picks and AI handicapping from Earl Knows Ball.",
    kw: ["sports picks", "AI handicapping"],
  };
  return {
    title: `${name} Picks, Odds & AI Handicapping`,
    description: meta.desc,
    keywords: meta.kw,
    openGraph: {
      title: `${name} Picks, Odds & AI Handicapping`,
      description: meta.desc,
      url: `https://earlknowsball.com/${sport}`,
      siteName: "Earl Knows Ball",
      type: "website",
    },
  };
}

export default async function SportHomePage({ params }: { params: Promise<{ sport: string }> }) {
  const { sport } = await params;

  if (!VALID_SPORTS.includes(sport)) {
    return <div className="text-center py-24 text-gray-500">Sport not found</div>;
  }

  return (
    <div className="space-y-16">
      {/* Earl's Best Picks — sport-filtered single value pick per game */}
      <BestBetsPanel
        sport={sport as "mlb" | "nba" | "nfl"}
        showSport={false}
        limit={4}
        containerClassName=""
        title="Earl's Best Picks"
        hideIfEmpty
      />

      {/* Upcoming Games */}
      <SportUpcomingGames sport={sport as CardSport} />

      {/* Daily Picks (MLB home page special section) */}
      {sport === "mlb" && <DailyPicksSection sport={sport} />}

      {/* Recent Articles & Game Previews */}
      <RecentContent sport={sport} />

      {/* Standings — only surfaced on a sport's home page while that sport
          is OUT of season (e.g. NBA during the summer). In-season sports get
          standings on the dedicated /standings page instead. */}
      <StandingsWidget
        sport={sport as "nfl" | "nba" | "mlb"}
        containerClassName=""
        title={`${SPORT_INFO[sport]?.name ?? sport.toUpperCase()} Standings`}
        subtitle="W-L · Games back · Streak · Last 10"
        hideIfEmpty
        onlyWhenOffseason
      />
    </div>
  );
}
