"use client";

import { useCallback, useEffect, useState } from "react";
import { RefreshCw, AlertTriangle } from "lucide-react";
import TeamLogo from "./TeamLogo";
import { MatchupTrendsGrid, MatchupComparisonTable } from "./MatchupView";
import { api, MatchupResponse } from "../lib/api";

type Sport = "nfl" | "nba" | "mlb";

/**
 * Inline matchup block used inside the game details page's Detailed Analysis
 * section — same data as the VS button's modal, rendered without a portal so
 * it can live inside the page flow.
 */
export default function MatchupPanel({
  sport,
  gameId,
  homeAbbr,
  awayAbbr,
  homeName,
  awayName,
}: {
  sport: Sport;
  gameId: number;
  homeAbbr: string;
  awayAbbr: string;
  homeName?: string;
  awayName?: string;
}) {
  const [data, setData] = useState<MatchupResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    setData(null);
    try {
      const res = await api.matchup.get({ sport, gameId, home: homeName ?? homeAbbr, away: awayName ?? awayAbbr });
      setData(res ?? null);
    } catch (e: any) {
      setError(e?.message ?? "Couldn't load matchup");
    } finally {
      setLoading(false);
    }
  }, [sport, gameId, homeName, awayName, homeAbbr, awayAbbr]);

  useEffect(() => {
    load();
  }, [load]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-8 text-gray-400">
        <RefreshCw className="mr-2 h-4 w-4 animate-spin" />
        <span className="text-sm">Loading matchup...</span>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="flex items-center gap-2 rounded-xl border border-red-900/50 bg-red-950/30 px-4 py-3 text-sm text-red-300">
        <AlertTriangle className="h-4 w-4 shrink-0" />
        {error ?? "Matchup data unavailable."}
      </div>
    );
  }

  const home = data.teams.home;
  const away = data.teams.away;

  return (
    <div className="space-y-5">
      {/* Header strip */}
      <div className="flex items-center justify-between gap-3 rounded-xl border border-gray-800 bg-gray-900/40 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <TeamLogo abbr={home.abbr} sport={sport} name={home.name} size={26} />
          <div className="min-w-0">
            <div className="truncate text-sm font-bold text-white">{home.abbr}</div>
            <div className="hidden truncate text-[11px] text-gray-500 sm:block">{home.name}</div>
          </div>
        </div>
        <span className="text-xs font-black uppercase tracking-widest text-gray-500">vs</span>
        <div className="flex min-w-0 flex-row-reverse items-center gap-2 text-right">
          <TeamLogo abbr={away.abbr} sport={sport} name={away.name} size={26} />
          <div className="min-w-0">
            <div className="truncate text-sm font-bold text-white">{away.abbr}</div>
            <div className="hidden truncate text-[11px] text-gray-500 sm:block">{away.name}</div>
          </div>
        </div>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <section>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-bold uppercase tracking-widest text-gray-400">How they&apos;re playing</span>
            <span className="h-px flex-1 bg-gray-800" />
          </div>
          <MatchupTrendsGrid teams={data.teams} sport={sport} />
        </section>

        <section>
          <div className="mb-2 flex items-center gap-2">
            <span className="text-xs font-bold uppercase tracking-widest text-gray-400">Head-to-head numbers</span>
            <span className="h-px flex-1 bg-gray-800" />
          </div>
          {data.comparison ? (
            <MatchupComparisonTable
              compare={data.comparison.compare}
              teamA={data.comparison.team_a}
              teamB={data.comparison.team_b}
              homeAbbr={home.abbr}
              awayAbbr={away.abbr}
            />
          ) : (
            <div className="rounded-xl border border-gray-800 p-4 text-xs text-gray-500">
              {data.comparison_error ?? "Comparison data unavailable."}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
