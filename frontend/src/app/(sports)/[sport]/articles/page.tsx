import { Metadata } from "next";

import ArticlesListClient from "./ArticlesListClient";
import { hubMetadata } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  const label = sport.toUpperCase();
  return hubMetadata(
    sport,
    "Articles & Game Previews",
    `Read ${label} original articles, game previews, analysis, and AI handicapping content from Earl Knows Ball.`,
    `/${sport}/articles`
  );
}

export default function ArticlesPage({ params }: Props) {
  return <ArticlesListClient params={params} />;
}
