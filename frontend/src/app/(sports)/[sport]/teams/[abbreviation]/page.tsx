import { Metadata } from "next";

import TeamDetailPage from "./TeamClient";
import ServerTeamBody from "./ServerTeamBody";
import { teamMetadata, teamContent } from "@/lib/seo-content";

// Server component wrapper — see ./games/[id]/page.tsx for the why.
// Resolves the real team name server-side so crawlers see
// "Chicago Bears — NFL Team: Schedule, Odds & Stats" in the raw HTML.
//
// SEO (2026-09-10): also SERVER-RENDER a non-premium team summary (name +
// schedule/results) into the initial HTML. TeamClient is fully client-fetched,
// so crawlers previously saw an empty shell. Picks / spreads / totals / EV are
// premium and deliberately EXCLUDED from the server block (see ServerTeamBody).

type Props = {
  params: Promise<{ sport: string; abbreviation: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, abbreviation } = await params;
  return teamMetadata(sport, abbreviation);
}

export default async function TeamPage({ params }: Props) {
  const { sport, abbreviation } = await params;
  const content = await teamContent(sport, abbreviation);
  return (
    <>
      <ServerTeamBody content={content} />
      <TeamDetailPage />
    </>
  );
}
