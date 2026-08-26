import { Metadata } from "next";

import GameDetailPage from "./GameClient";
import { gameMetadata } from "@/lib/seo-content";
import { gameStructuredData } from "@/lib/structured-data";
import JsonLd from "@/components/JsonLd";

// Server component wrapper.
//
// `generateMetadata` runs server-side so the raw HTML carries the real
// "Chicago Bears vs Tennessee Titans Prediction, Odds & Picks" title instead
// of a generic app title that would only be injected client-side post-hydration
// (which non-JS crawlers & Google's initial HTML pass never see). The heavy UI
// lives in ./GameClient.tsx (a "use client" component); this wrapper stays
// server-only and thin.

type Props = {
  params: Promise<{ sport: string; id: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, id } = await params;
  return gameMetadata(sport, id);
}

export default async function GamePage({ params }: Props) {
  const { sport, id } = await params;
  const jsonLd = await gameStructuredData(sport, id);
  return (
    <>
      {jsonLd ? <JsonLd data={jsonLd} /> : null}
      <GameDetailPage />
    </>
  );
}
