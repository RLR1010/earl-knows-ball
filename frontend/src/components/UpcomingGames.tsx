"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Image from "next/image";
import { getTeamLogoUrl } from "@/lib/team_logos";
import { formatSpreadAway, formatSpread, formatOverUnder } from "@/lib/api";

interface UpcomingGame {
  sport: "mlb" | "nba" | "nfl";
  id: number;
  date: string;
  status: string;
  home_team_name: string;
  away_team_name: string;
  home_score: number | null;
  away_score: number | null;
  home_pitcher_name: string | null;
  away_pitcher_name: string | null;
  venue: string | null;
  spread: number | null;
  over_under: number | null;
  home_moneyline: number | null;
  away_moneyline: number | null;
  opening_spread: number | null;
  opening_total: number | null;
  opening_home_moneyline: number | null;
  opening_away_moneyline: number | null;
  predicted_margin: number | null;
  pred_rl_result: string | null;
  pred_ml_result: string | null;
  pred_ou_result: string | null;
  pred_rl_pick: string | null;
}

function formatTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  }) + " ET";
}

function statusBadge(status: string): { label: string; cls: string } {
  switch (status.toLowerCase()) {
    case "final": return { label: "FINAL", cls: "text-green-400" };
    case "in_progress": return { label: "LIVE", cls: "text-red-400 animate-pulse" };
    case "postponed": return { label: "PPD", cls: "text-yellow-400" };
    case "cancelled": return { label: "CANC", cls: "text-gray-500" };
    default: return { label: "SCHEDULED", cls: "text-earl-400" };
  }
}

function predBadge(label: string, result: string | null): React.ReactNode {
  if (!result) return null;
  const resultLower = result.toLowerCase();
  let cls: string;
  if (resultLower === "win") {
    cls = "bg-green-900/40 text-green-400 border border-green-500/30";
  } else if (resultLower === "push") {
    cls = "bg-gray-700/40 text-gray-400 border border-gray-600/30";
  } else {
    cls = "bg-red-900/40 text-red-400 border border-red-500/30";
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${cls}`}>
      {label}
    </span>
  );
}

const SPORT_LABELS: Record<string, string> = {
  mlb: "MLB",
  nba: "NBA",
  nfl: "NFL",
};

const SPORT_COLORS: Record<string, string> = {
  mlb: "bg-blue-900/40 text-blue-400 border-blue-500/30",
  nba: "bg-red-900/40 text-red-400 border-red-500/30",
  nfl: "bg-green-900/40 text-green-400 border-green-500/30",
};

export default function UpcomingGames() {
  const [games, setGames] = useState<UpcomingGame[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/home/upcoming-games")
      .then((r) => r.json())
      .then((data) => {
        if (!cancelled) {
          setGames(data);
          setLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  if (loading) {
    return (
      <section className="max-w-5xl mx-auto px-4 mb-12">
        <h2 className="text-3xl font-bold mb-6 text-center">Upcoming Games</h2>
        <div className="text-center py-12 text-gray-500">Loading upcoming games...</div>
      </section>
    );
  }

  if (games.length === 0) {
    return (
      <section className="max-w-5xl mx-auto px-4 mb-12">
        <h2 className="text-3xl font-bold mb-6 text-center">Upcoming Games</h2>
        <div className="text-center py-12 text-gray-500">
          No upcoming games scheduled across MLB, NBA, and NFL.
        </div>
      </section>
    );
  }

  return (
    <section className="max-w-5xl mx-auto px-4 mb-12">
      <h2 className="text-3xl font-bold mb-6 text-center">Upcoming Games</h2>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {games.map((g) => {
          const badge = statusBadge(g.status);
          const homeLogo = getTeamLogoUrl(g.home_team_name, g.sport);
          const awayLogo = getTeamLogoUrl(g.away_team_name, g.sport);

          return (
            <Link
              key={`${g.sport}-${g.id}`}
              href={`/${g.sport}/games/${g.id}`}
              className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
            >
              {/* Sport badge — top left */}
              <div className="flex mb-2">
                <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider border ${SPORT_COLORS[g.sport] || "bg-gray-700/40 text-gray-400 border-gray-600/30"}`}>
                  {SPORT_LABELS[g.sport] || g.sport.toUpperCase()}
                </span>
              </div>

              {/* Status centered on its own row */}
              <div className="text-center mb-2">
                <span className={`text-xs font-bold tracking-wider ${badge.cls}`}>
                  {badge.label}
                </span>
              </div>

              {/* Teams — matches schedule page layout */}
              <div className="flex items-center justify-between gap-2 py-1">
                <div className="flex flex-col items-center gap-1 min-w-0">
                  {awayLogo ? (
                    <Image src={awayLogo} alt={g.away_team_name} width={28} height={28} className="w-7 h-7 object-contain" />
                  ) : (
                    <div className="w-7 h-7 bg-white/10 rounded-full" />
                  )}
                  <span className="text-[11px] text-gray-400 truncate max-w-[80px]">{g.away_team_name}</span>
                </div>

                <div className="flex flex-col items-center">
                  <span className="text-[10px] text-gray-500 uppercase mb-0.5">at</span>
                  <span className="text-xs text-gray-300">{formatTime(g.date)}</span>
                </div>

                <div className="flex flex-col items-center gap-1 min-w-0">
                  {homeLogo ? (
                    <Image src={homeLogo} alt={g.home_team_name} width={28} height={28} className="w-7 h-7 object-contain" />
                  ) : (
                    <div className="w-7 h-7 bg-white/10 rounded-full" />
                  )}
                  <span className="text-[11px] text-gray-400 truncate max-w-[80px]">{g.home_team_name}</span>
                </div>
              </div>

              {/* Pitchers (MLB only) — matches schedule page */}
              {g.sport === "mlb" && g.away_pitcher_name && (
                <div className="text-[11px] text-gray-500 mt-1">
                  {g.away_pitcher_name} vs {g.home_pitcher_name || "TBD"}
                </div>
              )}

              {/* Betting lines — exact match with schedule page */}
              <div className="mt-2 pt-2 border-t border-white/10 text-xs flex items-center justify-center gap-1">
                <span className="text-earl-300">{formatSpreadAway(g.spread, g.away_team_name || "")}</span>
                <span className="mx-1 text-gray-700">|</span>
                <span className="text-earl-400">{formatSpread(g.spread, g.home_team_name || "")}</span>
                {g.over_under != null && <>
                  <span className="mx-1 text-gray-700">|</span>
                  <span className="text-gray-400">{formatOverUnder(g.over_under)}</span>
                </>}
              </div>

              {/* Prediction badges — exact match with schedule page */}
              <div className="flex items-center justify-evenly mt-2">
                {predBadge("RL", g.pred_rl_result)}
                {predBadge("OU", g.pred_ou_result)}
                {predBadge("ML", g.pred_ml_result)}
              </div>
            </Link>
          );
        })}
      </div>
    </section>
  );
}
