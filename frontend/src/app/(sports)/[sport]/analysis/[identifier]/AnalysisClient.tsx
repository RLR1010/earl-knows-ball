"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSeo } from "@/components/Seo";
import PremiumGate from "@/components/PremiumGate";

interface AnalysisDetail {
  id: number;
  slug?: string | null;
  game_id: number;
  title: string;
  content: string;
  matchup: string;
  published_at: string | null;
  game_date: string | null;
}

const VALID_SPORTS = ["nfl", "nba", "mlb"];

function formatDate(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

export default function SportAnalysisDetailPage({
  params,
}: {
  params: Promise<{ sport: string; identifier: string }>;
}) {
  const [sport, setSport] = useState<string>("");
  const [identifier, setIdentifier] = useState<string>("");
  const [preview, setPreview] = useState<AnalysisDetail | null>(null);
  const [missing, setMissing] = useState(false);
  const [premiumRequired, setPremiumRequired] = useState(false);

  useEffect(() => {
    let active = true;
    params.then(({ sport, identifier }) => {
      setSport(sport.toLowerCase());
      setIdentifier(identifier);
      const normalized = sport.toLowerCase();
      // Use the relative Caddy-proxied /api path so the browser can reach the
      // backend (a raw localhost:8001 URL is server-only and fails in the client).
      const token = typeof window !== "undefined" ? localStorage.getItem("earl_token") : null;
      fetch(`/api/writeups/${normalized}/${identifier}?tier=premium`, {
        cache: "no-store",
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      })
        .then(async (r) => {
          if (r.status === 403) {
            if (active) setPremiumRequired(true);
            return null;
          }
          if (!r.ok) {
            if (active) setMissing(true);
            return null;
          }
          const d = await r.json();
          return (d?.writeup ?? d) || null;
        })
        .then((d) => active && d && setPreview(d))
        .catch(() => active && setMissing(true));
    });
    return () => {
      active = false;
    };
  }, [params]);

  const name = (SPORT_NAMES[sport] || sport || "sports").toUpperCase();

  useSeo({
    title: preview
      ? `${preview.title || "Analysis"} — ${name} | Earl Knows Ball`
      : `${name} Analysis | Earl Knows Ball`,
    description: preview
      ? `${preview.title || "Premium"} premium analysis from Earl Knows Ball.`
      : `Premium ${name} analysis from Earl Knows Ball.`,
  });

  if (missing) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-24 text-center text-gray-400">
        Analysis not found.
      </div>
    );
  }

  if (premiumRequired) {
    return (
      <PremiumGate
        title="Premium Analysis"
        message="This analysis is for Premium members. Upgrade to unlock the full write-up."
      >
        <></>
      </PremiumGate>
    );
  }

  if (!preview) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-24 text-center text-gray-500">Loading…</div>
    );
  }

  const title = preview.title?.trim() || (preview.matchup ? `${preview.matchup} Analysis` : "Analysis");
  const dateStr = formatDate(preview.game_date || preview.published_at);

  return (
    <div className="max-w-3xl mx-auto px-4 py-12">
      <div className="text-sm text-gray-500 mb-6">
        <Link href={`/${sport}`} className="hover:text-earl-400 transition">
          {sport.toUpperCase()}
        </Link>
        <span className="mx-2 text-gray-600">·</span>
        <Link href={`/${sport}/analysis`} className="hover:text-earl-400 transition">
          Analysis
        </Link>
      </div>

      <PremiumGate
        title="Premium Analysis"
        message="Unlock detailed handicapping breakdowns, Earl's picks, and full-game analysis for every matchup."
      >
        <article>
          <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-2">{title}</h1>
          <div className="text-sm text-gray-500 mb-8">
            <span className="text-gray-300">by Earl</span>
            <span className="mx-2 text-gray-600">·</span>
            {dateStr}
          </div>

          <div className="writeup-content">
            <div className="text-gray-300 leading-relaxed">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content}</ReactMarkdown>
            </div>
          </div>
        </article>
      </PremiumGate>

      <div className="mt-12 pt-6 border-t border-white/10 text-center">
        <Link
          href={`/${sport}/analysis`}
          className="text-sm text-earl-400 hover:text-earl-300 transition"
        >
          More {sport.toUpperCase()} analysis →
        </Link>
      </div>
    </div>
  );
}

const SPORT_NAMES: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };
