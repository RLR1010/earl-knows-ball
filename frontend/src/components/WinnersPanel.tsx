"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, Winner } from "../lib/api";
import { getTeamLogoUrl } from "@/lib/team_logos";

/** Which winners to show (mirrors BestBetsPanel's sport scoping). */
type WinnersSport = "all" | "mlb" | "nba" | "nfl";

const MARKET_LABEL: Record<string, string> = {
  spread: "ATS",
  total: "O/U",
  ml: "ML",
};

function SpoofWinnerLogo({ abbr, sport }: { abbr?: string | null; sport: string }) {
  if (!abbr) return <div className="h-9 w-9 rounded bg-white/5" />;
  const url = getTeamLogoUrl(abbr, sport);
  if (!url) return <div className="h-9 w-9 rounded bg-white/5 ring-1 ring-white/10" />;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={url}
      alt={abbr}
      className="h-9 w-9 rounded object-contain"
      loading="lazy"
      onError={(e) => {
        (e.currentTarget as HTMLImageElement).style.visibility = "hidden";
      }}
    />
  );
}

/** Map mlb/nba/nfl -> source key used by getTeamLogoUrl. WNBA not present here. */
function logoSport(sport: string): "mlb" | "nba" | "nfl" {
  return sport as "mlb" | "nba" | "nfl";
}

const SPORT_META: Record<"mlb" | "nba" | "nfl", { label: string; tag: string }> = {
  mlb: { label: "MLB", tag: "bg-red-500/15 text-red-300 ring-red-400/30" },
  nba: { label: "NBA", tag: "bg-orange-500/15 text-orange-300 ring-orange-400/30" },
  nfl: { label: "NFL", tag: "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30" },
};

function fmtLocaleDate(d: string | null | undefined): string {
  if (!d) return "";
  const [y, m, day] = d.slice(0, 10).split("-");
  const dt = new Date(Date.UTC(Number(y), Number(m) - 1, Number(day)));
  return dt.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/** Optional recap article shown in a column BESIDE the winner cards on home. */
export interface WinnersRecapSlot {
  title: string;
  snippet: string;
  href: string;
  kicker?: string;
  footnote?: string;
}

interface WinnersPanelProps {
  sport: WinnersSport;
  showSport?: boolean;
  title?: string;
  subtitle?: string;
  containerClassName?: string;
  hideIfEmpty?: boolean;
  limit?: number;
  /** When set, renders a two-column body: recap column beside the winner cards. */
  recap?: WinnersRecapSlot | null;
}

export default function WinnersPanel({
  sport,
  showSport = true,
  title = "Earl's Winners",
  subtitle = "Recent calls that cashed.",
  containerClassName = "",
  hideIfEmpty = true,
  limit = 8,
  recap,
}: WinnersPanelProps) {
  const [winners, setWinners] = useState<Winner[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let active = true;
    api.winners
      .list({ sport, limit })
      .then((res) => {
        if (!active) return;
        setWinners(res.winners ?? []);
      })
      .catch(() => {
        if (active) setError(true);
      });
    return () => {
      active = false;
    };
  }, [sport, limit]);

  if (error) return null;
  if (winners === null) return null; // let parent server-render a stable container/loader
  if (hideIfEmpty && winners.length === 0) return null;

  return (
    <section className={`w-full ${containerClassName}`}>
      <div className="mx-auto max-w-7xl px-4 py-6">
        <div className="flex items-end justify-between gap-3">
          <div>
            <h2 className="text-lg font-bold tracking-tight text-white sm:text-xl">
              <span className="mr-1.5">🏆</span>
              {title}
            </h2>
            {subtitle ? <p className="mt-0.5 text-sm text-zinc-400">{subtitle}</p> : null}
          </div>
        </div>

        <div
          className={
            recap
              ? "mt-4 grid grid-cols-1 gap-4 sm:grid-cols-2 sm:items-stretch"
              : ""
          }
        >
        {recap ? (
          <div className="min-w-0">
            <Link href={recap.href} className="group flex h-full flex-col rounded-2xl border border-white/10 bg-gradient-to-br from-white/[0.06] to-white/[0.01] p-5 transition-colors hover:border-zinc-400/40">
              {recap.kicker ? (
                <span className="mb-3 inline-flex w-fit items-center gap-1 rounded-full bg-zinc-500/15 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-zinc-300 ring-1 ring-white/10">
                  {recap.kicker}
                </span>
              ) : null}
              <h3 className="text-lg font-extrabold leading-snug tracking-tight text-white transition-colors group-hover:text-zinc-200">
                {recap.title}
              </h3>
              <p className="mt-2 line-clamp-3 text-sm leading-relaxed text-zinc-400">
                {recap.snippet}
              </p>
              {recap.footnote ? (
                <span className="mt-auto inline-flex items-center gap-1.5 pb-0 pt-3 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  {recap.footnote}
                  <svg viewBox="0 0 16 16" fill="currentColor" className="h-3 w-3"><path d="M5 3l5 5-5 5V3z" /></svg>
                </span>
              ) : null}
            </Link>
          </div>
        ) : null}

        <div
          className={
            recap
              ? "grid min-w-0 auto-rows-fr grid-cols-2 gap-3"
              : "mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4"
          }
        >
          {winners.map((w) => {
            const csport = logoSport(w.sport);
            const meta = SPORT_META[csport] ?? SPORT_META.mlb;
            const home = w.home_team ?? "H";
            const away = w.away_team ?? "A";
            const pickUp = (w.pick_text ?? "").toUpperCase();
            return (
              <div
                key={`${w.sport}-${w.game_id}-${w.market}`}
                className="group relative overflow-hidden rounded-xl border border-white/10 bg-gradient-to-b from-white/[0.04] to-white/[0.01] p-3 transition-colors hover:border-emerald-400/40"
              >
                {/* subtle green corner glow = "cashed" cue */}
                <div className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full bg-emerald-500/10 blur-2xl" />

                {showSport && (
                  <span
                    className={`mb-2 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide ring-1 ${meta.tag}`}
                  >
                    {meta.label}
                  </span>
                )}

                {/* Team lines — score pinned to a FIXED right column so both
                    rows align vertically regardless of abbreviation length. */}
                <div className="mt-2 flex flex-col gap-1.5">
                  {[home, away].map((ab, idx) => {
                    const sc = idx === 0 ? w.home_score : w.away_score;
                    return (
                      <div key={idx} className="flex items-center gap-2">
                        <SpoofWinnerLogo abbr={ab} sport={w.sport} />
                        <span className="flex-1 truncate text-sm font-semibold text-white">{ab}</span>
                        <span className="w-16 shrink-0 text-right text-sm font-semibold text-zinc-100 tabular-nums">
                          {sc ?? "–"}
                        </span>
                      </div>
                    );
                  })}
                </div>

                <div className="mt-3 flex items-center justify-between">
                  <div className="flex min-w-0 items-center gap-1.5 text-emerald-400">
                    <svg
                      viewBox="0 0 20 20"
                      fill="currentColor"
                      className="h-4 w-4 shrink-0"
                      aria-hidden="true"
                    >
                      <path
                        fillRule="evenodd"
                        d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4l2.8 2.79 6.8-6.79a1 1 0 0 1 1.4 0Z"
                        clipRule="evenodd"
                      />
                    </svg>
                    <span className="truncate text-sm font-extrabold tracking-tight">{pickUp}</span>
                  </div>
                  <span
                    className="inline-flex shrink-0 items-center gap-1 rounded-md bg-emerald-400/15 px-1.5 py-0.5 text-[11px] font-bold text-emerald-300 ring-1 ring-emerald-400/30"
                    title="Model expected value of this pick at tip time ($ per 100 stake)"
                  >
                    EV +{(w.ev ?? 0).toFixed(2)}
                  </span>
                </div>

                <div className="mt-2 flex items-center justify-between text-[11px] text-zinc-400">
                  <span className="inline-flex items-center gap-1 rounded bg-white/[0.05] px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-zinc-300 ring-1 ring-white/10">
                    {MARKET_LABEL[w.market] ?? w.market}
                  </span>
                  <span className="flex items-center gap-2">
                    <span>{fmtLocaleDate(w.game_date)}</span>
                    {w.odds_at_tip ? (
                      <span className="rounded bg-white/[0.07] px-1.5 py-0.5 font-medium text-zinc-200">
                        {w.odds_at_tip}
                      </span>
                    ) : null}
                  </span>
                </div>
              </div>
            );
          })}
        </div>
        </div>
      </div>
    </section>
  );
}
