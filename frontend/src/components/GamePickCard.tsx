"use client";

import { useEffect, useState } from "react";
import EarlsPicksPanel from "@/components/EarlsPicksPanel";

function formatOdds(odds: number | null): string {
  if (!odds) return "-";
  return odds > 0 ? `+${odds}` : `${odds}`;
}

interface Team {
  away_team?: string;
  home_team?: string;
  away_abbrev?: string;
  home_abbrev?: string;
  scheduled?: string;
}

// Reusable "game's pick card" (Betting Lines + Earl's Picks underneath), shown on the
// analysis/writeup pages ABOVE the article — the same content the game-details page shows.
// MLB pick_card shape is the canonical one we render; if a sport returns a different shape
// (no pick_card / no run_line picks) we render nothing, so non-MLB writeups degrade gracefully.
export default function GamePickCard({
  sport,
  gameId,
}: {
  sport: string;
  gameId?: number | string | null;
}) {
  const [data, setData] = useState<any>(null);
  const [state, setState] = useState<"loading" | "ready" | "empty">("loading");

  useEffect(() => {
    if (!gameId || gameId === "") {
      setState("empty");
      return;
    }
    let active = true;
    (async () => {
      try {
        const res = await fetch(`/api/${sport}/games/${gameId}/boxscore`);
        if (!res.ok) throw new Error("boxscore not found");
        const d = await res.json();
        if (!active) return;
        const pc = d?.pick_card;
        const bl = d?.betting_lines;
        const game = d?.game;
        // Only render when we have a usable MLB-style pick card (Earl's Picks) + team matchup.
        const usable =
          pc &&
          pc.picks &&
          game &&
          game.away_team &&
          game.home_team &&
          (pc.picks.run_line !== undefined || pc.picks.moneyline !== undefined);
        setData(usable ? d : null);
        setState(usable ? "ready" : "empty");
      } catch {
        if (active) {
          setData(null);
          setState("empty");
        }
      }
    })();
    return () => {
      active = false;
    };
  }, [sport, gameId]);

  if (state !== "ready" || !data) return null;

  const pc = data.pick_card;
  const bl = data.betting_lines;
  const game: Team = data.game;

  return (
    <div className="not-prose space-y-4">
      {/* Betting Lines - shown whenever available */}
      {bl?.length > 0 && (
        <div className="rounded-xl border border-white/10 bg-gradient-to-r from-white/5 to-white/0 p-4">
          <div className="mb-3 text-xs uppercase tracking-wider text-gray-500">Betting Lines</div>
          <div className="grid grid-cols-1 divide-y divide-white/10 md:grid-cols-3 md:divide-x md:divide-y-0">
            <div className="py-3 text-center md:px-3">
              <div className="text-[10px] uppercase text-gray-500">Run Line</div>
              <div className="mt-1 text-sm">
                {bl[0]?.spread != null ? (
                  <>
                    <span className="text-earl-400">{game.away_team}</span>{" "}
                    {(-1 * bl[0].spread) > 0 ? `+${-1 * bl[0].spread}` : -1 * bl[0].spread}
                    <span className="ml-1 text-xs text-gray-500">({formatOdds(bl[0]?.spread_away_odds ?? -110)})</span>
                    <span className="mx-1 text-gray-600">|</span>
                    <span className="text-gray-400">{game.home_team}</span>{" "}
                    {bl[0].spread > 0 ? `+${bl[0].spread}` : bl[0].spread}
                    <span className="ml-1 text-xs text-gray-500">({formatOdds(bl[0]?.spread_home_odds ?? -110)})</span>
                  </>
                ) : (
                  "-"
                )}
              </div>
            </div>
            <div className="py-3 text-center md:px-3">
              <div className="text-[10px] uppercase text-gray-500">Moneyline</div>
              <div className="mt-1 text-sm">
                <span className="text-earl-400">{game.away_team}</span> {formatOdds(bl[0]?.away_moneyline)}
                <span className="mx-2 text-gray-600">|</span>
                <span className="text-gray-400">{game.home_team}</span> {formatOdds(bl[0]?.home_moneyline)}
              </div>
            </div>
            <div className="py-3 text-center md:px-3">
              <div className="text-[10px] uppercase text-gray-500">Over/Under</div>
              <div className="mt-1 text-sm font-semibold">
                {bl[0]?.over_under != null ? (
                  <>
                    O/U {bl[0].over_under}
                    <span className="ml-2 text-xs font-normal text-gray-500">Over {formatOdds(bl[0]?.over_odds ?? -110)}</span>
                    <span className="text-gray-600"> | </span>
                    <span className="text-xs font-normal text-gray-500">Under {formatOdds(bl[0]?.under_odds ?? -110)}</span>
                  </>
                ) : (
                  "-"
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Earl's Picks - underneath */}
      {pc && (
        <EarlsPicksPanel
          title="Earl's Picks"
          ungated
          predicted={
            pc.predictions?.home_runs != null
              ? {
                  awayLabel: game.away_team!,
                  homeLabel: game.home_team!,
                  awayScore: pc.predictions.away_runs,
                  homeScore: pc.predictions.home_runs,
                  total: pc.predictions.total,
                  margin: pc.predictions.margin,
                }
              : null
          }
          items={[
            {
              label: "Run Line",
              pick: pc.picks?.run_line && pc.picks.run_line !== "-" ? pc.picks.run_line.toUpperCase() : "—",
              ev: pc.expected_value?.rl ?? null,
              line: pc.lines?.run_line != null ? `Run Line ${pc.lines.run_line}` : null,
              result: pc.results?.run_line || null,
              pickColor: "text-amber-400",
            },
            {
              label: "Over/Under",
              pick: pc.picks?.over_under && pc.picks.over_under !== "-" ? pc.picks.over_under.toUpperCase() : "—",
              ev: pc.expected_value?.ou ?? null,
              line: pc.lines?.over_under != null ? `O/U ${pc.lines.over_under}` : null,
              result: pc.results?.over_under || null,
              pickColor: "text-yellow-400",
            },
            {
              label: "Moneyline",
              pick: pc.picks?.moneyline && pc.picks.moneyline !== "-" ? pc.picks.moneyline.toUpperCase() : "—",
              ev: pc.expected_value?.ml ?? null,
              line:
                pc.lines?.home_moneyline != null
                  ? `ML ${formatOdds(pc.lines.away_moneyline)} | ${formatOdds(pc.lines.home_moneyline)}`
                  : null,
              result: pc.results?.moneyline || null,
              pickColor: "text-cyan-400",
            },
          ]}
        />
      )}
    </div>
  );
}
