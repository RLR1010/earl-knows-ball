import { Metadata } from "next";

import TeamDetailPage from "../TeamClient";
import { teamMetadata } from "@/lib/seo-content";

type Props = {
  params: Promise<{ sport: string; abbreviation: string; season: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, abbreviation } = await params;
  return teamMetadata(sport, abbreviation);
}

export default function TeamSeasonPage() {
  return <TeamDetailPage />;
}
