"use client";

import Image from "next/image";
import { useCallback, useEffect, useState } from "react";
import { api, Game } from "../lib/api";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "./LoginModal";
import type { CardSport } from "./ScheduleGameCard";

type BestBetSport = "all" | "mlb" | "nba" | "nfl";

const SPORT_META: Record<"mlb" | "nba" | "nfl", { label: string; color: string }> = {
  mlb: { label: "MLB", color: "text-red-400" },
  nba: { label: "NBA", color: "text-orange-400" },
  nfl: { label: "NFL", color: "text-emerald-400" },
};

const BET_TYPE_LABEL: Record<string, string> = {
  ats: "Spread",
  ou: "Total",
  ml: "Moneyline",
};


function formatTime(iso: string) {
  const d = new Date(iso);
  return (
    d.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
      timeZone: "America/Chicago",
    }) + " CT"
  );
}

function formatDate(iso: string) {
  const d = new Date(iso);
  const today = new Date();
  const tomorrow = new Date();
  tomorrow.setDate(today.getDate() + 1);
  const isToday = d.toDateString() === today.toDateString();
  const isTomorrow = d.toDateString() === tomorrow.toDateString();
  if (isToday) return "Today";
  if (isTomorrow) return "Tomorrow";
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function BestBetsPanel({
  sport = "all",
  showSport = true,
  limit = 5,
  title = "Earl's Best Bets",
  subtitle,
  containerClassName = "max-w-6xl mx-auto px-4",
  hideIfEmpty = false,
}: {
  sport?: BestBetSport;
  showSport?: boolean;
  limit?: number;
  title?: string;
  subtitle?: string;
  containerClassName?: string;
  hideIfEmpty?: boolean;
}) {
  const { user, loading: authLoading } = useAuth();
  const [games, setGames] = useState<Game[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loginModalOpen, setLoginModalOpen] = useState(false);

  const load = useCallback(async () => {
    setError(null);
    setGames(null);
    try {
      const data = await api.bestBets.list({ sport, limit });
      setGames(data ?? []);
    } catch (e: any) {
      setError(e?.message ?? "Failed to load best bets");
    }
  }, [sport, limit]);

  useEffect(() => {
    load();
  }, [load]);

  const isPremium =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";

  if (error) {
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="text-sm text-red-400">
          Couldn't load best bets: {error}
        </div>
      </section>
    );
  }

  if (games === null || authLoading) {
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="flex items-center gap-2 text-gray-400 text-sm">
          <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-gray-500 border-t-transparent" />
          Loading Earl's best bets…
        </div>
      </section>
    );
  }

  if (games.length === 0) {
    // On sport pages, hide the feature entirely when no games qualify.
    if (hideIfEmpty) {
      return null;
    }
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="rounded-xl border border-white/10 bg-white/[0.02] p-6 text-center">
          <h2 className="text-lg font-bold text-white">{title}</h2>
          <p className="mt-1 text-sm text-gray-400">
            {sport === "all"
              ? "No high-value picks right now — check back closer to game day."
              : `No high-value picks in ${SPORT_META[sport]?.label ?? sport.toUpperCase()} right now.`}
          </p>
        </div>
      </section>
    );
  }

  // Free users: show the panel header + how many best bets are available, but
  // gate the actual picks/edges behind Premium (matches the rest of the site).
  if (!isPremium) {
    const sportsPresent = Array.from(new Set(games.map((g) => g.sport).filter(Boolean))) as (
      | "mlb"
      | "nba"
      | "nfl"
    )[];
    return (
      <section className={`${containerClassName} py-8`}>
        <div className="rounded-2xl border border-white/10 bg-gradient-to-br from-amber-400/[0.06] via-white/[0.02] to-transparent overflow-hidden">
          <div className="p-6 md:p-8 grid md:grid-cols-[auto_1fr] gap-6 md:gap-10 items-start">
            {/* Earl mascot — mirror the un-gated version */}
            <div className="hidden md:block shrink-0">
              <div className="w-40 h-60 rounded-2xl overflow-hidden border border-white/10 shadow-lg shadow-amber-900/20">
                <Image
                  src="/earl-with-bat.png"
                  alt="Earl, your AI handicapper"
                  width={160}
                  height={240}
                  className="w-full h-full object-cover"
                  priority={false}
                />
              </div>
            </div>

            <div className="min-w-0">
              {/* Mobile: title on left, Earl on the right (same height as title block) */}
              <div className="flex items-start gap-3 md:block">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="inline-flex items-center gap-1 rounded-md bg-amber-400/15 text-amber-300 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest">
                      ★ Earl's Picks
                    </span>
                  </div>
                  <h2 className="mt-2 text-2xl font-bold text-white">{title}</h2>
                  {subtitle ? (
                    <p className="hidden sm:block text-sm text-gray-400 mt-0.5">{subtitle}</p>
                  ) : null}
                </div>
                {/* Earl on the right for small screens, height-matched to the title block */}
                <div className="md:hidden shrink-0">
                  <div className="h-32 w-auto overflow-hidden">
                    <Image
                      src="/earl-with-bat.png"
                      alt="Earl, your AI handicapper"
                      width={96}
                      height={128}
                      className="h-full w-auto object-cover object-top"
                      priority={false}
                    />
                  </div>
                </div>
              </div>

              {/* Premium CTA — replaces the ranked list for free users */}
              <div className="mt-5 text-center">
                <p className="text-sm text-gray-200">
                  Earl has{" "}
                  <span className="font-bold text-amber-300">
                    {games.length} high-value
                  </span>{" "}
                  best bets right now
                  {sportsPresent.length === 1
                    ? ` in ${SPORT_META[sportsPresent[0]]?.label ?? sportsPresent[0]?.toUpperCase()}`
                    : sportsPresent.length > 1
                      ? ` across ${sportsPresent
                          .map((s) => SPORT_META[s]?.label ?? s.toUpperCase())
                          .join(" & ")}`
                      : ""}
                  .
                </p>
                <p className="mt-1 text-xs text-gray-400">
                  The picks and their edges are a Premium perk.
                </p>
                <div className="mt-4">
                  {user ? (
                    <a
                      href="/pricing"
                      className="inline-block py-2.5 px-6 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
                    >
                      Upgrade to Premium
                    </a>
                  ) : (
                    <button
                      onClick={() => setLoginModalOpen(true)}
                      className="py-2.5 px-6 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
                    >
                      Sign In to See the Picks
                    </button>
                  )}
                </div>
              </div>
            </div>
          </div>
        </div>
        <LoginModal open={loginModalOpen} onClose={() => setLoginModalOpen(false)} />
      </section>
    );
  }

  return (
    <section className={`${containerClassName} py-8`}>
      <div className="rounded-2xl border border-white/10 bg-gradient-to-br from-amber-400/[0.06] via-white/[0.02] to-transparent overflow-hidden">
        <div className="p-6 md:p-8 grid md:grid-cols-[auto_1fr] gap-6 md:gap-10 items-start">
          {/* Earl mascot — the visual anchor that gives Best Bets its own identity */}
          <div className="hidden md:block shrink-0">
            <div className="w-40 h-60 rounded-2xl overflow-hidden border border-white/10 shadow-lg shadow-amber-900/20">
              <Image
                src="/earl-with-bat.png"
                alt="Earl, your AI handicapper"
                width={160}
                height={240}
                className="w-full h-full object-cover"
                priority={false}
              />
            </div>
          </div>

          <div className="min-w-0">
            {/* Mobile: title on left, Earl on the right (same height as title block) */}
            <div className="flex items-start gap-3 md:block">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="inline-flex items-center gap-1 rounded-md bg-amber-400/15 text-amber-300 px-2 py-0.5 text-[10px] font-bold uppercase tracking-widest">
                    ★ Earl's Picks
                  </span>
                </div>
                <h2 className="mt-2 text-2xl font-bold text-white">Earl's Best Picks</h2>
                {subtitle ? (
                  <p className="hidden sm:block text-sm text-gray-400 mt-0.5">{subtitle}</p>
                ) : null}
              </div>
              {/* Earl on the right for small screens, height-matched to the title block */}
              <div className="md:hidden shrink-0">
                <div className="h-32 w-auto overflow-hidden">
                  <Image
                    src="/earl-with-bat.png"
                    alt="Earl, your AI handicapper"
                    width={96}
                    height={128}
                    className="h-full w-auto object-cover object-top"
                    priority={false}
                  />
                </div>
              </div>
            </div>

            {/* Ranked best-bet list — compact rows, no full game cards */}
            <div className="mt-5 flex flex-col divide-y divide-white/10">
              {games.map((g, idx) => {
                const csport: CardSport = (g.sport as CardSport) ?? "mlb";
                const sportLabel = SPORT_META[csport]?.label ?? csport?.toUpperCase();
                const met = SPORT_META[csport];
                const edge = g.best_bet_edge_pct;
                const pickLabel = g.best_bet_label;
                const betType = BET_TYPE_LABEL[g.best_bet_type ?? ""] ?? "";
                return (
                  <a
                    key={`${g.sport}-${g.id}`}
                    href={`/${g.sport}/games/${g.id}`}
                    className="group flex items-start gap-3 lg:gap-x-0 py-3 first:pt-1 last:pb-1 hover:bg-white/[0.03] transition rounded-lg -mx-2 px-2"
                  >
                    {/* Rank */}
                    <div className="shrink-0 w-7 text-center pt-0.5">
                      <span
                        className={`text-lg font-extrabold ${
                          idx === 0
                            ? "text-amber-300"
                            : "text-gray-600 group-hover:text-gray-400"
                        }`}
                      >
                        {idx + 1}
                      </span>
                    </div>

                    {/* Sport tag */}
                    <div className="shrink-0 w-10 text-center pt-1">
                      <span
                        className={`text-[10px] font-bold uppercase tracking-wider ${met?.color ?? "text-gray-400"}`}
                      >
                        {sportLabel}
                      </span>
                    </div>

                    {/* The pick — different layouts on mobile vs desktop; pushed right on large screens */}
                    <div className="min-w-0 flex-1 lg:pl-8">
                      {/* MOBILE: two-column stacked (pick | conf, matchup | implied, edge | EV) */}
                      <div className="sm:hidden">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-bold text-white">{pickLabel}</span>
                          {betType ? (
                            <span className="text-[10px] uppercase tracking-wider text-gray-500">
                              {betType}
                            </span>
                          ) : null}
                          <span className="ml-auto text-[11px] text-gray-400">
                            Conf <span className="text-gray-200">{g.best_bet_confidence_pct}%</span>
                          </span>
                        </div>
                        <div className="mt-0.5 flex items-center gap-2">
                          <span className="truncate text-xs text-gray-500">
                            {g.away_team} @ {g.home_team}
                          </span>
                          <span className="ml-auto shrink-0 text-[11px] text-gray-400">
                            Implied {g.best_bet_implied_pct}%
                          </span>
                        </div>
                        <div className="mt-0.5 flex items-center gap-2">
                          {edge != null && (
                            <span className="text-xs font-bold text-amber-300">
                              {edge}% edge
                            </span>
                          )}
                          {typeof g.best_bet_ev === "number" && (
                            <span
                              className={`ml-auto shrink-0 text-[11px] font-semibold ${
                                g.best_bet_ev >= 0 ? "text-emerald-400" : "text-red-400"
                              }`}
                            >
                              EV {g.best_bet_ev >= 0 ? "+" : ""}
                              {g.best_bet_ev.toFixed(2)}
                            </span>
                          )}
                        </div>
                      </div>

                      {/* DESKTOP: original horizontal layout — pick + type, team + records */}
                      <div className="hidden sm:block">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-bold text-white group-hover:text-amber-200 transition">
                            {pickLabel}
                          </span>
                          {betType ? (
                            <span className="text-[10px] uppercase tracking-wider text-gray-500">
                              {betType}
                            </span>
                          ) : null}
                        </div>
                        <div className="mt-0.5 truncate text-xs text-gray-500">
                          {g.away_team} @ {g.home_team}
                          {g.away_record ? ` · ${g.away_record}` : ""}
                          {g.home_record ? ` vs ${g.home_record}` : ""}
                        </div>
                      </div>
                    </div>

                    {/* MIDDLE column: edge % inline with the edge bar (bar only on lg+), vertically centered */}
                    <div className="hidden sm:block shrink-0 lg:ml-6 self-center">
                      {edge != null && (
                        <div className="flex items-center gap-2">
                          <span className="text-xs font-bold text-amber-300 leading-tight">
                            {edge}% edge
                          </span>
                          <div className="hidden lg:block w-20 h-1.5 rounded-full bg-white/10 overflow-hidden mr-10">
                            <div
                              className="h-full rounded-full bg-gradient-to-r from-amber-400 to-orange-400"
                              style={{ width: `${Math.min(edge ?? 0, 35) * 2.8}%` }}
                            />
                          </div>
                        </div>
                      )}
                    </div>

                    {/* RIGHT column: Conf / Implied / EV (desktop only) — anchored to the right edge */}
                    <div className="hidden sm:block shrink-0 text-right lg:ml-auto">
                      <div className="text-[11px] text-gray-400">
                        Conf <span className="text-gray-200">{g.best_bet_confidence_pct}%</span>
                      </div>
                      <div className="text-[11px] text-gray-500">Implied {g.best_bet_implied_pct}%</div>
                      {typeof g.best_bet_ev === "number" && (
                        <div
                          className={`mt-0.5 text-[11px] font-semibold ${
                            g.best_bet_ev >= 0 ? "text-emerald-400" : "text-red-400"
                          }`}
                        >
                          EV {g.best_bet_ev >= 0 ? "+" : ""}
                          {g.best_bet_ev.toFixed(2)}
                        </div>
                      )}
                    </div>
                  </a>
                );
              })}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
