import { Metadata } from "next";

import PropsClient from "./PropsClient";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  return hubMetadata(sport, "Player Props & Odds", "Compare {label} player props and betting odds from Earl Knows Ball.", `/${sport}/props`);
}

export default function PropsPage() {
  return <PropsClient />;
}
