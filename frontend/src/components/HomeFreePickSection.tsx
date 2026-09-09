"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

interface FreePick {
  sport: string;
  game_id: number;
  title: string;
  slug?: string | null;
  free_featured_at?: string | null;
  preview_image?: string | null;
  premium_social_card?: string | null;
  content?: {
    title?: string;
    content?: string;
    preview_image?: string | null;
    premium_social_card?: string | null;
  } | null;
  has_premium?: boolean;
  teams?: {
    away?: { abbr?: string; name?: string; record?: string; logo_url?: string } | null;
    home?: { abbr?: string; name?: string; record?: string; logo_url?: string } | null;
  } | null;
}

function MatchupSide({ team }: { team?: { abbr?: string; name?: string; record?: string; logo_url?: string } | null }) {
  if (!team) return <div className="h-24 w-24 md:h-28 md:w-28" />;
  const logo = team.logo_url;
  return (
    <div className="flex flex-col items-center gap-2.5 text-center">
      <div className="flex h-24 w-24 items-center justify-center rounded-full bg-gradient-to-b from-white to-slate-200 shadow-lg md:h-28 md:w-28">
        {logo ? (
          /* eslint-disable-next-line @next/next/no-img-element */
          <img src={logo} alt={team.name || ""} className="h-14 w-14 object-contain md:h-16 md:w-16" />
        ) : (
          <span className="text-lg font-black uppercase text-slate-400">{team.abbr || "?"}</span>
        )}
      </div>
      {team.name ? (
        <div className="max-w-[8.5rem] text-sm font-bold leading-tight text-white">{team.name}</div>
      ) : null}
      {team.record ? (
        <div className="text-sm font-semibold tabular-nums text-gray-400">({team.record})</div>
      ) : null}
    </div>
  );
}

function teaser(md?: string | null, max = 150): string {
  if (!md) return "";
  const txt = md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[#>*_`~|-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return txt.length > max ? txt.slice(0, max).trimEnd() + "…" : txt;
}

const SPORT_LABEL: Record<string, string> = {
  mlb: "MLB",
  nfl: "NFL",
  nba: "NBA",
};

/**
 * "Free Pick" giveaway block on the homepage. When an admin features a game writeup
 * (free_pick router -> is_free_feature), this surfaces it prominently above the editorials
 * with its social card and a link straight into the (now unlocked for everyone) write-up.
 * Renders nothing when no Free Pick is currently featured.
 */
export default function HomeFreePickSection() {
  const [pick, setPick] = useState<FreePick | null | undefined>(undefined);

  useEffect(() => {
    let active = true;
    fetch(`/api/writeups/free-pick`, { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => active && setPick(d || null))
      .catch(() => active && setPick(null));
    return () => {
      active = false;
    };
  }, []);

  // Loading or nothing featured -> don't take vertical space.
  if (!pick || !pick.slug) return null;

  const sport = pick.sport?.toLowerCase() || "";
  const href = `/${sport}/analysis/${pick.slug}`;
  const title = pick.content?.title || pick.title;
  const body = pick.content?.content || "";
  const snippet = teaser(body);
  const label = SPORT_LABEL[sport] ? `${SPORT_LABEL[sport]} · Free Pick` : "Free Pick";

  return (
    <section aria-label="Free Pick of the game" className="w-full mb-12">
      <div className="max-w-6xl mx-auto px-4">
        <Link
          href={href}
          className="group relative block overflow-hidden rounded-3xl border border-earl-700/50 bg-gradient-to-br from-earl-900/70 via-slate-900 to-black hover:border-earl-500/70 transition"
        >
          <div className="flex flex-col md:flex-row">
            {/* Text half */}
            <div className="flex flex-1 flex-col justify-center gap-4 p-6 md:p-9">
              <span className="inline-flex w-fit items-center gap-2 rounded-full bg-gradient-to-r from-green-500 to-earl-500 px-3 py-1 text-xs font-black uppercase tracking-widest text-white shadow-lg">
                <svg
                  className="h-3.5 w-3.5"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                  <polyline points="7 10 12 15 17 10" />
                  <line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                {label}
              </span>

              <h2 className="font-display text-2xl font-black leading-tight tracking-tight text-white md:text-4xl">
                {title || "Free Pick of the game"}
              </h2>

              {snippet ? (
                <p className="max-w-xl text-sm text-gray-300 md:text-base">{snippet}</p>
              ) : null}

              <span className="inline-flex w-fit items-center gap-2 rounded-full bg-earl-600 px-5 py-2.5 text-sm font-bold text-white transition group-hover:bg-earl-500">
                Read the free pick
                <svg
                  className="h-4 w-4"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="5" y1="12" x2="19" y2="12" />
                  <polyline points="12 5 19 12 12 19" />
                </svg>
              </span>

              <p className="text-xs font-medium uppercase tracking-wider text-gray-500">
                No subscription required — this one&apos;s on the house.
              </p>
            </div>

            {/* Matchup half (teams) */}
            <div className="flex items-center justify-center gap-5 px-6 pb-9 md:px-10 md:py-9">
              <MatchupSide team={pick.teams?.away} />
              <span className="text-2xl font-black italic text-earl-400 md:text-4xl">VS</span>
              <MatchupSide team={pick.teams?.home} />
            </div>
          </div>
        </Link>
      </div>
    </section>
  );
}
