import { Metadata } from "next";

import SportAnalysisDetailPage from "./AnalysisClient";
import { writeupMetadata } from "@/lib/seo-content";
import { writeupStructuredData } from "@/lib/structured-data";
import JsonLd from "@/components/JsonLd";

// Server component wrapper — emits a real <title> (the writeup's own
// headline) in raw HTML instead of a generic app title. UI lives in
// ./AnalysisClient.tsx. Takes params (Promise) and forwards them down;
// the client resolves them in useEffect as before.

type Props = {
  params: Promise<{ sport: string; identifier: string }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, identifier } = await params;
  return writeupMetadata(sport, identifier);
}

export default async function AnalysisPage({ params }: Props) {
  const { sport, identifier } = await params;
  const jsonLd = await writeupStructuredData(sport, identifier);
  return (
    <>
      {jsonLd ? <JsonLd data={jsonLd} /> : null}
      <SportAnalysisDetailPage params={params} />
    </>
  );
}
