"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ScheduleGameCard, { type ScheduleGameLike, type CardSport } from "@/components/ScheduleGameCard";

interface SportUpcomingGame extends ScheduleGameLike {
  sport: CardSport;
}

export default function SportUpcomingGames({ sport }: { sport: CardSport }) {
  const [games, setGames] = useState<SportUpcomingGame[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch(`/api/home/upcoming-games?sport=${sport}&days=7&with_predictions=true`)
      .then((res) => (res.ok ? res.json() : []))
      .then((data) => {
        if (!cancelled) {
          setGames(data || []);
          setLoading(false);
        }
      })
      .catch(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [sport]);

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
