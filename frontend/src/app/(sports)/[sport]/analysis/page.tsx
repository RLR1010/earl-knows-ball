import { Metadata } from "next";

import AnalysisListClient from "./AnalysisListClient";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  const label = sport.toUpperCase();
  return hubMetadata(
    sport,
    "Analysis",
    `Premium AI handicapping analysis for ${label} from Earl Knows Ball — detailed game breakdowns and picks for premium members.`,
    `/${sport}/analysis`
  );
}

export default function AnalysisPage({ params }: Props) {
  return <AnalysisListClient params={params} />;
}
