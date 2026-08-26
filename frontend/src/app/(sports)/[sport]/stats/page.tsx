import { Metadata } from "next";

import StatsClient from "./StatsClient";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  return hubMetadata(sport, "Stats & Leaders", "Explore {label} stats, leaderboards, and advanced numbers on Earl Knows Ball.", `/${sport}/stats`);
}

export default function StatsPage() {
  return <StatsClient />;
}
