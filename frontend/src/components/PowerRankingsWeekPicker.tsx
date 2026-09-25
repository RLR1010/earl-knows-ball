"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";

type WeekRow = { season: number; week: number };

// Oldest season surfaced in the UI (data exists back to 2015; we only show 2022+).
const MIN_SEASON = 2022;

export default function PowerRankingsWeekPicker({
  sport,
  weeks,
  season,
  week,
}: {
  sport: string;
  weeks: WeekRow[];
  season: number;
  week: number;
}) {
  const router = useRouter();

  const seasons = useMemo(
    () =>
      Array.from(new Set(weeks.map((w) => w.season)))
        .filter((s) => s >= MIN_SEASON || s === season)
        .sort((a, b) => b - a),
    [weeks, season],
  );
  const [selSeason, setSelSeason] = useState(season);
  const seasonWeeks = useMemo(
    () =>
      weeks
        .filter((w) => w.season === selSeason)
        .map((w) => w.week)
        .sort((a, b) => b - a),
    [weeks, selSeason],
  );
  const [selWeek, setSelWeek] = useState(week);

  // Chronological list of (season, week) ascending, for prev/next (2022+ only).
  const ordered = useMemo(
    () =>
      weeks
        .filter((w) => w.season >= MIN_SEASON || w.season === season)
        .sort((a, b) => a.season - b.season || a.week - b.week),
    [weeks, season],
  );
  const idx = ordered.findIndex((w) => w.season === season && w.week === week);
  const prev = idx > 0 ? ordered[idx - 1] : null;
  const next = idx >= 0 && idx < ordered.length - 1 ? ordered[idx + 1] : null;

  const go = (s: number, w: number) =>
    router.push(`/${sport}/power-rankings/week/${w}?season=${s}`);

  const selCls =
    "bg-white/5 border border-white/10 rounded px-2 py-1.5 text-sm text-gray-200";

  return (
    <div className="flex flex-wrap items-center gap-2 mb-5">
      <button
        type="button"
        disabled={!prev}
        onClick={() => prev && go(prev.season, prev.week)}
        className="px-3 py-1.5 rounded bg-white/5 hover:bg-white/10 text-sm text-gray-200 disabled:opacity-30"
        aria-label="Previous week"
      >
        ← Prev
      </button>

      <select
        className={selCls}
        value={selSeason}
        onChange={(e) => {
          const s = parseInt(e.target.value, 10);
          setSelSeason(s);
          const wks = weeks
            .filter((w) => w.season === s)
            .map((w) => w.week)
            .sort((a, b) => b - a);
          if (wks.length) {
            setSelWeek(wks[0]);
            go(s, wks[0]);
          }
        }}
        aria-label="Season"
      >
        {seasons.map((s) => (
          <option key={s} value={s} className="text-gray-900">
            {s}
          </option>
        ))}
      </select>

      <select
        className={selCls}
        value={selWeek}
        onChange={(e) => {
          const w = parseInt(e.target.value, 10);
          setSelWeek(w);
          go(selSeason, w);
        }}
        aria-label="Week"
      >
        {seasonWeeks.map((w) => (
          <option key={w} value={w} className="text-gray-900">
            Week {w}
          </option>
        ))}
      </select>

      <button
        type="button"
        disabled={!next}
        onClick={() => next && go(next.season, next.week)}
        className="px-3 py-1.5 rounded bg-white/5 hover:bg-white/10 text-sm text-gray-200 disabled:opacity-30"
        aria-label="Next week"
      >
        Next →
      </button>

      {next ? (
        <span className="text-xs text-gray-500">
          latest is Week {next.week}, {next.season}
        </span>
      ) : (
        <span className="text-xs text-emerald-400">latest snapshot</span>
      )}
    </div>
  );
}
