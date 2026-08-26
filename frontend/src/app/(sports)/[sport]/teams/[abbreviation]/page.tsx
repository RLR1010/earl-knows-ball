import { Metadata } from "next";

import TeamDetailPage from "./TeamClient";
import { teamMetadata } from "@/lib/seo-content";

// Server component wrapper — see ./games/[id]/page.tsx for the why.
// Resolves the real team name server-side so crawlers see
// "Chicago Bears — NFL Team: Schedule, Odds & Stats" in the raw HTML.

type Props = {
  params: Promise<{ sport: string; abbreviation: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, abbreviation } = await params;
  return teamMetadata(sport, abbreviation);
}

export default function TeamPage() {
  return <TeamDetailPage />;
}
