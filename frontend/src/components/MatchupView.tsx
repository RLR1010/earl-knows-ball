"use client";

import TeamLogo from "./TeamLogo";
import { MatchupResponse } from "../lib/api";

type Sport = "nfl" | "nba" | "mlb";

// Metrics where a LOWER value is better (highlight leader accordingly).
const LOWER_IS_BETTER = new Set([
  "OPPG",
  "pace",
  "turnovers",
  "TOs",
  "DRTG",
  "defensive rating",
  "ERA",
  "WHIP",
  "opp_avg",
]);

const METRIC_LABELS: Record<string, string> = {
  PPG: "Points / Game",
  OPPG: "Opponent Points",
  pace: "Pace (poss/g)",
  "Net Rating": "Net Rating",
  NETRTG: "Net Rating",
  FG_PCT: "FG %",
  "FG%": "FG %",
  "3PT%": "3PT %",
  "FT%": "FT %",
  TRB: "Rebounds",
  rebound: "Rebounds",
  AST: "Assists",
  assist: "Assists",
  TOs: "Turnovers",
  turnovers: "Turnovers",
  STL: "Steals",
  BLK: "Blocks",
  "Points For": "Points For",
  "Points Against": "Points Against",
  runs: "Runs / Game",
  AVG: "Batting Avg",
  avg: "Batting Avg",
  OBP: "On-Base %",
  SLG: "Slugging",
  OPS: "OPS",
  HR: "Home Runs",
  "R/Game": "Runs / Game",
  RA: "Runs Allowed",
  "RA/G": "Runs Allowed / G",
  ERA: "ERA",
  WHIP: "WHIP",
};

function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function num(v: unknown): number | null {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function fmtVal(v: unknown): string {
  const n = num(v);
  if (n === null) return String(v ?? "—");
  if (n > 0 && n <= 1.5 && n < 10) return `${(n * 100).toFixed(0)}%`;
  return String(n);
}

/** Glanceable per-team trend chips (normalized across sports). */
export function TrendChips({ trends }: { trends: Record<string, unknown> | null }) {
  if (!trends) return <p className="text-xs text-gray-500">No trend data available.</p>;
  const chips: { label: string; value: React.ReactNode; good?: boolean }[] = [];

  // NBA-style
  if (trends.last_10 && typeof trends.last_10 === "object") {
    const l10 = trends.last_10 as Record<string, unknown>;
    const l5 = (trends.last_5 as Record<string, unknown>) ?? undefined;
    const record = (o: Record<string, unknown>) =>
      typeof o.wins === "number" ? `${o.wins}-${num(o.losses) ?? 0}` : null;
    const rec10 = record(l10);
    if (rec10) chips.push({ label: "Last 10", value: rec10 });
    if (l5) {
      const a = typeof l5.ats_wins === "number" ? l5.ats_wins : null;
      const total = typeof l5.ats_total === "number" ? l5.ats_total : null;
      if (a !== null && total) chips.push({ label: "ATS L5", value: `${a}-${total - a}` });
      const ou = typeof l5.ou_over_wins === "number" ? l5.ou_over_wins : null;
      if (ou !== null && total) chips.push({ label: "Over L5", value: `${ou}/${total}` });
    }
    const rw5 = (trends.recent_weighted_5 ?? {}) as Record<string, unknown>;
    const nr = num(l10.net_rating ?? rw5.net_rating);
    if (nr !== null) {
      chips.push({ label: "Net rtg", value: `${nr > 0 ? "+" : ""}${nr}`, good: nr > 0 });
    }
  }

  // MLB-style
  if (trends.latest_summary && typeof trends.latest_summary === "object") {
    const s = trends.latest_summary as Record<string, unknown>;
    const rr = num(s.runs_game);
    if (rr !== null) chips.push({ label: "Runs/G", value: rr });
    const wp = num(s.win_pct);
    if (wp !== null && wp <= 1) chips.push({ label: "Win%", value: `${(wp * 100).toFixed(0)}%` });
    const slp = num(s.slg);
    if (slp !== null) chips.push({ label: "SLG", value: slp });
  }

  if (chips.length === 0) {
    return <p className="text-xs text-gray-500">No trending stats for this team yet.</p>;
  }
  return (
    <div className="flex flex-wrap gap-1.5">
      {chips.map((c) => (
        <span
          key={c.label}
          className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium tabular-nums ${
            c.good === undefined
              ? "border-gray-700 bg-gray-800/60 text-gray-200"
              : c.good
                ? "border-emerald-800/60 bg-emerald-950/40 text-emerald-300"
                : "border-red-900/50 bg-red-950/30 text-red-300"
          }`}
        >
          <span className="text-gray-500">{c.label}</span>
          <span>{c.value}</span>
        </span>
      ))}
    </div>
  );
}

/** Two-column trends grid (home/away). */
export function MatchupTrendsGrid({
  teams,
  sport,
}: {
  teams: MatchupResponse["teams"];
  sport: Sport;
}) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {(["home", "away"] as const).map((side) => {
        const team = teams[side];
        return (
          <div
            key={side}
            className="rounded-xl border border-gray-800 bg-gray-900/40 p-3"
          >
            <div className="mb-2 flex items-center gap-2">
              <TeamLogo abbr={team.abbr} sport={sport} name={team.name} size={20} />
              <div className="text-sm font-semibold text-white">
                {team.abbr}
                <span className="ml-1.5 text-[10px] font-medium uppercase tracking-wide text-gray-500">
                  {side === "home" ? "Home" : "Away"}
                </span>
              </div>
            </div>
            <TrendChips trends={team.trends} />
          </div>
        );
      })}
    </div>
  );
}

/** Side-by-side comparison table with the better side highlighted. */
export function MatchupComparisonTable({
  compare,
  teamA,
  teamB,
  homeAbbr,
  awayAbbr,
}: {
  compare: Record<string, Record<string, number>>;
  teamA: string;
  teamB: string;
  homeAbbr: string;
  awayAbbr: string;
}) {
  const metrics = Object.keys(compare).filter((k) => k !== "team_a" && k !== "team_b");
  const rows = metrics.map((key) => {
    const row = compare[key] ?? {};
    const av = num(row[teamA]);
    const bv = num(row[teamB]);
    const lowerBetter = LOWER_IS_BETTER.has(key);
    let aBest = false;
    let bBest = false;
    if (av !== null && bv !== null) {
      aBest = lowerBetter ? av < bv : av > bv;
      bBest = lowerBetter ? bv < av : bv > av;
    }
    return { key, av, bv, aBest, bBest };
  });

  if (rows.length === 0) {
    return <p className="text-xs text-gray-500">No comparison data available.</p>;
  }

  return (
    <div className="overflow-hidden rounded-xl border border-gray-800">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 border-b border-gray-800 bg-gray-900/60 px-3 py-2 text-xs">
        <span className="truncate font-semibold text-white">{homeAbbr}</span>
        <span className="text-[10px] font-bold uppercase tracking-widest text-gray-500">Stat</span>
        <span className="truncate text-right font-semibold text-white">{awayAbbr}</span>
      </div>
      <div className="divide-y divide-gray-800/60">
        {rows.map((r) => (
          <div
            key={r.key}
            className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 px-3 py-1.5 text-sm"
          >
            <span className={`text-right tabular-nums ${r.aBest ? "font-bold text-emerald-300" : "text-gray-300"}`}>
              {r.av === null ? "—" : fmtVal(r.av)}
            </span>
            <span className="w-24 text-center text-[11px] text-gray-500">{metricLabel(r.key)}</span>
            <span className={`tabular-nums ${r.bBest ? "font-bold text-emerald-300" : "text-gray-300"}`}>
              {r.bv === null ? "—" : fmtVal(r.bv)}
            </span>
          </div>
        ))}
      </div>
      <div className="border-t border-gray-800 bg-gray-900/40 px-3 py-1.5 text-[11px] text-gray-500">
        <span className="font-medium text-emerald-300">Green</span> = better on that stat
      </div>
    </div>
  );
}
