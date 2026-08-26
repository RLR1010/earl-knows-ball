import { Metadata } from "next";

import ScheduleClient from "./ScheduleClient";
import { hubMetadata } from "@/lib/seo-content";

// Server component wrapper — see ./pages.tsx pattern. Emits a real sport-aware
// <title> in raw HTML (no client-side injection needed). UI in ScheduleClient.

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  return hubMetadata(sport, "Schedule, Odds & Lines", "View the {label} schedule, spreads, and betting lines on Earl Knows Ball.", `/${sport}/schedule`);
}

export default function SchedulePage() {
  return <ScheduleClient />;
}
