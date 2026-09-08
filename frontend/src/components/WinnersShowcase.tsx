"use client";

import { useEffect, useState } from "react";
import WinnersPanel, { WinnersRecapSlot } from "./WinnersPanel";

interface LatestArticle {
  id: number;
  title: string;
  slug: string;
  sport?: string | null;
  summary?: string | null;
  published_at?: string | null;
}

/**
 * Home wrapper for Earl's Winners. Pulls the most recent Earl's-Winners recap
 * article (articles whose admin auto-generation section is "Earl's Winners" /
 * DB section `earls_winners`, cadence ~every 2 days) and shows its headline +
 * snippet in a column BESIDE 4 winner cards. We filter by the earls_winners
 * section explicitly (NOT the newest article of any/all sections) so this
 * block always shows the real winners recap, never a Daily Picks or other
 * editorial. If no Earl's Winners recap exists yet, it gracefully falls back
 * to the winner cards alone so the block never looks broken.
 */
export default function WinnersShowcase() {
  const [recap, setRecap] = useState<WinnersRecapSlot | null>(null);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let active = true;
    fetch(`/api/original-articles/all?limit=5&section=earls_winners`)
      .then((r) => (r.ok ? r.json() : { articles: [] }))
      .then((d) => {
        if (!active) return;
        const list = Array.isArray(d.articles) ? d.articles : Array.isArray(d) ? d : [];
        const sorted = [...list].sort((a, b) =>
          String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")),
        );
        const latest = sorted.find((a) => a && a.title) as LatestArticle | undefined;
        if (latest) {
          setRecap({
            kicker: "Winners Recap",
            title: latest.title,
            snippet: latest.summary || "",
            href: `/${latest.sport ?? "all"}/articles/${latest.slug || latest.id}`,
            footnote: "Read the recap",
          });
        } else {
          setRecap(null);
        }
        setDone(true);
      })
      .catch(() => {
        if (active) {
          setRecap(null);
          setDone(true);
        }
      });
    return () => {
      active = false;
    };
  }, []);

  void done;

  // Winners always render; the recap column appears once the latest recap
  // article resolves (and vanishes again if none exists).
  return (
    <WinnersPanel
      sport="all"
      limit={4}
      showSport={false}
      recap={recap}
      containerClassName="max-w-6xl mx-auto px-4"
      subtitle="This week's calls that cashed — read the full lowdown."
    />
  );
}
