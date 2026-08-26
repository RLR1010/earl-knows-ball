import type { Metadata } from "next";
import StandingsWidget from "@/components/StandingsWidget";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  const label = sport.toUpperCase();
  return hubMetadata(
    sport,
    "Standings",
    `Current ${label} standings: records, games back, winning streaks, last-10 form, and division races from Earl Knows Ball.`,
    `/${sport}/standings`
  );
}

export default async function StandingsPage({ params }: Props) {
  const { sport } = await params;
  const valid: Record<string, "nfl" | "nba" | "mlb"> = {
    nfl: "nfl",
    nba: "nba",
    mlb: "mlb",
  } as const;

  if (!valid[sport]) {
    return <div className="text-center py-24 text-gray-500">Sport not found</div>;
  }

  return (
    <div className="max-w-6xl mx-auto px-4">
      <StandingsWidget
        sport={valid[sport]}
        title={`${sport.toUpperCase()} Standings`}
        subtitle="W-L · Games back · Streak · Last 10"
      />
    </div>
  );
}
