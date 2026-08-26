"use client";

import { useCallback, useEffect, useState } from "react";
import TeamLogo from "./TeamLogo";
import { api, StandingsResponse, StandingsTeam } from "../lib/api";

type StandingsSport = "nfl" | "nba" | "mlb";

const SPORT_META: Record<StandingsSport, { label: string; color: string }> = {
  nfl: { label: "NFL", color: "text-emerald-400" },
  nba: { label: "NBA", color: "text-orange-400" },
  mlb: { label: "MLB", color: "text-red-400" },
};

function formatGB(gb: number) {
  if (gb <= 0.01) return "-";
  return gb.toFixed(1);
}

function StreakBadge({ streak }: { streak: number }) {
  if (streak === 0) return <span className="text-gray-500">—</span>;
  const won = streak > 0;
  const label = won ? `${streak} W` : `${Math.abs(streak)} L`;
  return (
    <span className={won ? "text-emerald-400" : "text-red-400"}>{label}</span>
  );
}

export default function StandingsWidget({
  sport,
  conference,
  title,
  subtitle,
  limitPerDivision = 5,
  containerClassName = "max-w-6xl mx-auto px-4",
  hideIfEmpty = false,
  onlyWhenOffseason = false,
}: {
  sport: StandingsSport;
  conference?: string;
  title?: string;
  subtitle?: string;
  limitPerDivision?: number;
  containerClassName?: string;
  hideIfEmpty?: boolean;
  /** When true, render nothing if the sport is currently in season. */
  onlyWhenOffseason?: boolean;
}) {
  const [data, setData] = useState<StandingsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setData(null);
    try {
      const res = await api.standings.get({ sport, conference });
      setData(res ?? null);
    } catch (e: any) {
      setError(e?.message ?? "Failed to load standings");
    }
  }, [sport, conference]);

  useEffect(() => {
    load();
  }, [load]);

  const meta = SPORT_META[sport] ?? SPORT_META.nfl;
  const heading = title ?? `${meta.label} Standings`;

  // Off-season-only widget: hide entirely when the league is live.
  if (onlyWhenOffseason && data?.in_season) return null;

  if (error) {
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="text-sm text-red-400">
          Couldn't load standings: {error}
        </div>
      </section>
    );
  }

  if (data === null) {
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="h-24 animate-pulse rounded-xl bg-gray-800/60" />
      </section>
    );
  }

  if (!data.conferences?.length) {
    if (hideIfEmpty) return null;
    return (
      <section className={`${containerClassName} py-6`}>
        <div className="text-sm text-gray-400">No standings available yet.</div>
      </section>
    );
  }

  return (
    <section className={`${containerClassName} py-8`}>
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h2 className={`text-xl font-bold sm:text-2xl ${meta.color}`}>
            {heading}
          </h2>
          <p className="mt-1 text-xs text-gray-400">
            {subtitle ?? `${data.season} season · W-L · Games back · Streak · Last 10`}
          </p>
        </div>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        {data.conferences.map((conf) => (
          <div
            key={conf.name ?? "other"}
            className="rounded-2xl border border-gray-800 bg-gray-900/50 p-4"
          >
            <h3 className="mb-3 text-sm font-semibold uppercase tracking-wide text-gray-300">
              {conf.name ?? "Other"}
            </h3>
            <div className="space-y-4">
              {conf.divisions.map((div) => (
                <div key={div.division ?? "other"}>
                  <div className="mb-1 flex items-baseline justify-between text-[11px] text-gray-500">
                    <span className="font-medium uppercase tracking-wide">
                      {div.division ?? "Division"}
                    </span>
                  </div>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-gray-700 text-[10px] uppercase tracking-wide text-gray-500">
                        <th className="py-1 pr-1 w-7" />
                        <th className="py-1 pr-2 text-left font-medium">Team</th>
                        <th className="py-1 pr-2 text-right font-medium tabular-nums">REC</th>
                        <th className="py-1 pr-2 text-right font-medium tabular-nums">GB</th>
                        <th className="py-1 pr-2 text-right font-medium tabular-nums">STK</th>
                        <th className="py-1 text-right font-medium tabular-nums">L10</th>
                      </tr>
                    </thead>
                    <tbody>
                      {div.teams.slice(0, limitPerDivision).map((t) => (
                        <StandingsRow key={t.team_id} t={t} sport={sport} />
                      ))}
                    </tbody>
                  </table>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function StandingsRow({ t, sport }: { t: StandingsTeam; sport: StandingsSport }) {
  return (
    <tr className="border-b border-gray-800/60 last:border-0">
      <td className="py-1.5 pr-1 w-7">
        <TeamLogo abbr={t.abbreviation} sport={sport} name={t.team_name} size={22} />
      </td>
      <td className="py-1.5 pr-2">
        <span className="font-semibold text-gray-100">{t.abbreviation}</span>
        <span className="hidden sm:inline text-gray-500"> · {t.team_name}</span>
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums text-gray-200">
        {t.wins}-{t.losses}
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums text-gray-400">
        {formatGB(t.games_back)}
      </td>
      <td className="py-1.5 pr-2 text-right tabular-nums">
        <StreakBadge streak={t.streak} />
      </td>
      <td className="py-1.5 text-right tabular-nums text-gray-400">
        <span className="inline-flex">
          <span className={t.last10.wins >= t.last10.losses ? "text-emerald-400" : "text-gray-400"}>
            {t.last10.wins}
          </span>
          <span className="text-gray-600">-</span>
          <span className={t.last10.losses > t.last10.wins ? "text-red-400" : "text-gray-400"}>
            {t.last10.losses}
          </span>
        </span>
      </td>
    </tr>
  );
}
