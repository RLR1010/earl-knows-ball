"use client";

import { useEffect, useState } from "react";

interface PropBet {
  player_name: string;
  team_id?: number | null;
  prop_type: string;
  line: number | null;
  odds: number | null;
  direction: string | null;
}

interface PropBetsTabProps {
  sport: "nfl" | "nba" | "mlb";
  gameId: string | number;
  /** Optional map of team_id -> team name/abbreviation for labeling. */
  teamById?: Record<string | number, string>;
}

/**
 * Fetch and render player prop bets for a game. The backend already filters
 * to a single sportsbook, so no book is exposed to the user here.
 */
export default function PropBetsTab({ sport, gameId, teamById }: PropBetsTabProps) {
  const [bets, setBets] = useState<PropBet[] | null>(null);

  const apiPath =
    sport === "mlb"
      ? `/api/mlb/games/${gameId}/prop-bets`
      : sport === "nba"
      ? `/api/nba/games/${gameId}/prop-bets`
      : `/api/games/${gameId}/prop-bets`;

  useEffect(() => {
    let cancelled = false;
    fetch(apiPath)
      .then(r => r.json())
      .then(data => {
        if (!cancelled) setBets(Array.isArray(data) ? data : []);
      })
      .catch(() => {
        if (!cancelled) setBets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [apiPath]);

  if (bets === null) {
    return (
      <div className="p-6 text-sm text-gray-400">
        <div className="animate-pulse">Loading prop bets…</div>
      </div>
    );
  }

  if (bets.length === 0) {
    return <div className="p-6 text-sm text-gray-400">No prop bets available for this game.</div>;
  }

  const teamName = (id?: number | null) =>
    id != null && teamById && teamById[id] ? teamById[id] : "";

  // Group by player, then by market (prop_type). Each market has an Over and/or
  // Under leg which we combine into a single row showing both odds.
  const byPlayer = bets.reduce<Record<string, PropBet[]>>((acc, b) => {
    const key = b.player_name;
    (acc[key] = acc[key] || []).push(b);
    return acc;
  }, {});

  const formatOdds = (o: number | null | undefined) => {
    if (o == null) return "";
    return o > 0 ? `+${o}` : `${o}`;
  };

  const marketLabel = (t: string) =>
    t
      .replace(/_/g, " ")
      .replace(/\b\w/g, c => c.toUpperCase());

  const OddsPill = ({ odds }: { odds: number | null | undefined }) => (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-semibold ${
        (odds ?? 0) < 0
          ? "bg-red-500/15 text-red-300"
          : "bg-green-500/15 text-green-300"
      }`}
    >
      {formatOdds(odds)}
    </span>
  );

  const renderMarket = (mb: PropBet[]) => {
    const byDir = (d: string) => mb.find(b => (b.direction || "").toLowerCase() === d);
    const over = byDir("over");
    const under = byDir("under");
    const side = over || under;
    const line = side?.line ?? "";
    return (
      <div key={`${mb[0].prop_type}-${line}`} className="px-4 py-2 text-sm flex items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="text-gray-200">{marketLabel(mb[0].prop_type)}</div>
          <div className="text-xs text-gray-500">Total {line}</div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {over && (
            <div className="flex items-center gap-1.5 text-gray-400">
              <span className="text-xs">Over</span>
              <OddsPill odds={over.odds} />
            </div>
          )}
          {under && (
            <div className="flex items-center gap-1.5 text-gray-400">
              <span className="text-xs">Under</span>
              <OddsPill odds={under.odds} />
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <div className="p-4 sm:p-6">
      <div className="space-y-5">
        {Object.entries(byPlayer).map(([player, playerBets]) => {
          const markets = playerBets.reduce<Record<string, PropBet[]>>((acc, b) => {
            (acc[b.prop_type] = acc[b.prop_type] || []).push(b);
            return acc;
          }, {});
          const count = playerBets.length;
          return (
            <div
              key={player}
              className="rounded-lg border border-white/10 bg-white/5 overflow-hidden"
            >
              <div className="px-4 py-2.5 bg-white/10 flex items-center gap-2">
                <span className="font-semibold text-white text-sm">{player}</span>
                {teamName(playerBets[0].team_id) && (
                  <span className="text-xs text-gray-400">{teamName(playerBets[0].team_id)}</span>
                )}
                <span className="ml-auto text-xs text-gray-500">{count} props</span>
              </div>
              <div className="divide-y divide-white/5">
                {Object.values(markets).map(renderMarket)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
