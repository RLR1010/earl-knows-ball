import { Metadata } from "next";

import SportAnalysisDetailPage from "./AnalysisClient";
import ServerWriteupBody from "./ServerWriteupBody";
import { writeupMetadata, writeupContent } from "@/lib/seo-content";
import { writeupStructuredData } from "@/lib/structured-data";
import JsonLd from "@/components/JsonLd";

// Server component wrapper — emits a real <title> (the writeup's own
// headline) in raw HTML instead of a generic app title.
//
// SEO (2026-09-10): we also SERVER-RENDER the body of FREE-FEATURE writeups.
// Previously the page was fully client-fetched, so even the one crawlable
// writeup class reached Googlebot as an empty loading shell — the classic
// "crawled - currently not indexed" trap. Now:
//   * free-feature (backend 200) -> body rendered server-side (ServerWriteupBody)
//   * paywalled (backend 403) / missing -> delegate to the interactive client
//     component (paywall page; deliberately NOT in the sitemap).
// Middleware.ts still 301s old published slugs to the canonical slug.
//
// If the requested identifier is an OLD published slug that maps to a writeup
// whose canonical slug changed (manual regen with a new title), middleware.ts
// 301s to the live canonical URL so browser links + crawlers converge.

type Props = {
  params: Promise<{ sport: string; identifier: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, identifier } = await params;
  return writeupMetadata(sport, identifier);
}

export default async function AnalysisPage({ params }: Props) {
  const { sport, identifier } = await params;

  // Server-side content fetch, gated exactly like the client (free=200, paywalled=403).
  const [jsonLd, content] = await Promise.all([
    writeupStructuredData(sport, identifier),
    writeupContent(sport, identifier),
  ]);

  // Only the free-feature writeup's body is server-rendered for crawlers.
  const renderServerBody = content.ok && (content.data?.is_free_feature ?? true);

  return (
    <>
      {jsonLd ? <JsonLd data={jsonLd} /> : null}
      {renderServerBody ? (
        <ServerWriteupBody sport={sport} identifier={identifier} content={content} />
      ) : (
        <SportAnalysisDetailPage params={params} />
      )}
    </>
  );
}
