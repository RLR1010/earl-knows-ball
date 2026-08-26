import { Metadata } from "next";

import ResultsClient from "./ResultsClient";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  return hubMetadata(sport, "Betting Results & Records", "Review {label} betting results, records, and historical picks performance on Earl Knows Ball.", `/${sport}/results`);
}

export default function ResultsPage() {
  return <ResultsClient />;
}
