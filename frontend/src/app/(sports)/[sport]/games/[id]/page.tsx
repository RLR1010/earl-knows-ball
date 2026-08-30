import { Metadata } from "next";
import { permanentRedirect } from "next/navigation";

import GameDetailPage from "./GameClient";
import { gameMetadata } from "@/lib/seo-content";
import { gameStructuredData } from "@/lib/structured-data";
import { gameIdFromSegment } from "@/lib/game-slug";
import { backendBaseForPath } from "@/lib/backend-url";
import JsonLd from "@/components/JsonLd";

// Server component wrapper.
//
// `generateMetadata` runs server-side so the raw HTML carries the real
// "Chicago Cubs vs St. Louis Cardinals Prediction, Odds & Picks" title instead
// of a generic app title that would only be injected client-side post-hydration
// (which non-JS crawlers & Google's initial HTML pass never see). The heavy UI
// lives in ./GameClient.tsx (a "use client" component); this wrapper stays
// server-only and thin.
//
// URL — canonical SEO form is a single-segment slug:
//   /{sport}/games/{home-full}-vs-{away-full}-{YYYY-MM-DD}-{gid}
//   e.g. /mlb/games/chicago-cubs-vs-st-louis-cardinals-2026-08-26-49070
// The trailing numeric game id is the authority. A legacy numeric URL
// (/games/49070) or a wrong/outdated slug permanently redirects to canonical.

type Props = {
  params: Promise<{ sport: string; id: string }>;
};

/** Resolve the canonical slug for a game id from the backend (nullable). */
async function canonicalSlugFor(
  sport: string,
  gameId: string
): Promise<string | null> {
  try {
    const base = backendBaseForPath(`/seo/game-meta/${sport}/${gameId}`);
    const res = await fetch(
      `${base}/seo/game-meta/${sport}/${encodeURIComponent(gameId)}`,
      { next: { revalidate: 0 }, cache: "no-store" }
    );
    if (!res.ok) return null;
    const data = (await res.json()) as { slug?: string | null };
    return data?.slug || null;
  } catch {
    return null;
  }
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, id } = await params;
  return gameMetadata(sport, id);
}

export default async function GamePage({ params }: Props) {
  const { sport, id } = await params;

  // 1. Every incoming path must produce a numeric game id. For a slug segment
  //    the numeric id is the trailing token; a purely-numeric legacy id passes
  //    straight through (still resolved, but redirected to canonical below).
  const gameId = gameIdFromSegment(id);
  // 2. If we can't extract a numeric id there's nothing to render — bail to
  //    the sport hub instead of a broken game page.
  if (!gameId) {
    permanentRedirect(`/${sport}/schedule`);
    // redirect throws; this next line is unreachable but satisfies TS.
    return null;
  }

  // 3. Canonical slug comes from the backend (single source of truth). Redirect
  //    permanently to it whenever the requested segment isn't canonical — this
  //    consolidates legacy numeric URLs AND wrong/old slugs onto one URL.
  //    Fail-open: if the backend is unreachable we still render (no redirect),
  //    so metadata resolution never breaks the page.
  const canonical = await canonicalSlugFor(sport, gameId);
  if (canonical && id !== canonical) {
    permanentRedirect(`/${sport}/games/${canonical}`);
  }

  const jsonLd = await gameStructuredData(sport, gameId);
  return (
    <>
      {jsonLd ? <JsonLd data={jsonLd} /> : null}
      <GameDetailPage gameId={gameId} />
    </>
  );
}
