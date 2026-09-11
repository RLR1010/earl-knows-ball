"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import ScheduleGameCard, { type ScheduleGameLike, type CardSport } from "@/components/ScheduleGameCard";
import { usePollingRefresh } from "@/lib/usePollingRefresh";

interface SportUpcomingGame extends ScheduleGameLike {
  sport: CardSport;
}

export default function SportUpcomingGames({ sport }: { sport: CardSport }) {
  const [games, setGames] = useState<SportUpcomingGame[]>([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/home/upcoming-games?sport=${sport}&days=7&with_predictions=true`);
      const data = res.ok ? await res.json() : [];
      setGames(data || []);
    } catch {
      // keep last-known-good list on transient errors
    } finally {
      setLoading(false);
    }
  }, [sport]);

  useEffect(() => {
    load();
  }, [load]);

  // Auto-refresh so scores/picks update without a manual reload (mirrors schedule).
  usePollingRefresh(load);

  // Hide the entire section when there are no upcoming games (after load).
  if (!loading && games.length === 0) return null;

  return (
    <section>
      <div className="flex items-center justify-between mb-1">
        <h2 className="font-display text-3xl font-bold">Upcoming Games</h2>
        <Link href={`/${sport}/schedule`} className="text-sm text-earl-400 hover:underline">
          Full schedule →
        </Link>
      </div>
      <p className="text-sm text-gray-400 mb-5">Pick accuracy improves closer to game time.</p>

      {loading ? (
        <div className="text-center py-12 text-gray-500 border border-white/10 rounded-xl bg-white/5">
          Loading upcoming games...
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {games.map((g) => (
            <ScheduleGameCard
              key={g.id}
              game={g}
              sport={sport}
              href={`/${sport}/games/${g.id}`}
            />
          ))}
        </div>
      )}
    </section>
  );
}
