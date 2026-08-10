"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ScheduleGameCard, { type ScheduleGameLike, type CardSport } from "@/components/ScheduleGameCard";

interface UpcomingGame extends ScheduleGameLike {
  sport: CardSport;
}

const SPORT_LABELS: Record<string, string> = {
  mlb: "MLB",
  nba: "NBA",
  nfl: "NFL",
};

export default function UpcomingGames() {
  const [games, setGames] = useState<UpcomingGame[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/home/upcoming-games")
      .then((res) => res.json())
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

  // Group the cross-sport list by sport so the home page keeps per-sport context.
  const order: CardSport[] = ["mlb", "nba", "nfl"];
  const grouped = new Map<CardSport, UpcomingGame[]>();
  for (const g of games) {
    const s = g.sport;
    if (!grouped.has(s)) grouped.set(s, []);
    grouped.get(s)!.push(g);
  }
  const orderedGroups = order
    .map((s) => [s, grouped.get(s) ?? []] as const)
    .filter(([, list]) => list.length > 0);

  return (
    <section className="max-w-5xl mx-auto px-4 mb-12">
      <h2 className="text-3xl font-bold mb-6 text-center">Upcoming Games</h2>

      {orderedGroups.map(([sport, list]) => (
        <div key={sport} className="mb-8 last:mb-0">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-xl font-bold text-gray-300">
              {SPORT_LABELS[sport] ?? sport.toUpperCase()}
            </h3>
            <Link
              href={`/${sport}/schedule`}
              className="text-sm text-earl-400 hover:underline"
            >
              {SPORT_LABELS[sport] ?? sport.toUpperCase()} schedule →
            </Link>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {list.map((g) => (
              <ScheduleGameCard
                key={`${g.sport}-${g.id}`}
                game={g}
                sport={g.sport}
                href={`/${g.sport}/games/${g.id}`}
              />
            ))}
          </div>
        </div>
      ))}
    </section>
  );
}
