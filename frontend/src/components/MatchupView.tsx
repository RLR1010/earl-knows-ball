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

/** Glanceable per-team trend chips (sport-specific, mapped to real data shapes). */
export function TrendChips({ trends, sport }: { trends: Record<string, unknown> | null; sport: Sport }) {
  if (!trends) return <p className="text-xs text-gray-500">No trend data available.</p>;
  const chips: { label: string; value: React.ReactNode; good?: boolean }[] = [];

  const pct = (v: unknown) => {
    const n = num(v);
    return n === null || n > 1.5 ? null : `${(n * 100).toFixed(0)}%`;
  };

  // ---- NBA (last_5 / last_10 are N-game rolling windows) ----
  if (trends.last_10 && typeof trends.last_10 === "object") {
    const l10 = trends.last_10 as Record<string, unknown>;
    const l5 = (trends.last_5 as Record<string, unknown>) ?? null;
    const rec = (o: Record<string, unknown> | null, n: number) => {
      if (!o || typeof o.wins !== "number") return null;
      return `${o.wins}-${n - o.wins}`;
    };
    const rec10 = rec(l10, 10);
    if (rec10) chips.push({ label: "Last 10", value: rec10 });
    const rec5 = rec(l5, 5);
    if (rec5) chips.push({ label: "L5", value: rec5 });

    const ats = (o: Record<string, unknown> | null, n: number) => {
      if (!o || typeof o.ats_wins !== "number") return null;
      return `${o.ats_wins}-${n - o.ats_wins}`;
    };
    const ats10 = ats(l10, 10);
    if (ats10) chips.push({ label: "ATS L10", value: ats10 });
    const ats5 = ats(l5, 5);
    if (ats5) chips.push({ label: "ATS L5", value: ats5 });

    const ovr = (o: Record<string, unknown> | null, n: number) => {
      if (!o || typeof o.ou_over_wins !== "number") return null;
      return `${o.ou_over_wins}-${n - o.ou_over_wins}`;
    };
    const ovr10 = ovr(l10, 10);
    if (ovr10) chips.push({ label: "Over L10", value: ovr10 });
    const ovr5 = ovr(l5, 5);
    if (ovr5) chips.push({ label: "Over L5", value: ovr5 });

    const nr = num(l10.net_rating);
    if (nr !== null) {
      chips.push({ label: "Net rtg L10", value: `${nr > 0 ? "+" : ""}${nr}`, good: nr > 0 });
    }
    const pace = num(l10.pace);
    if (pace !== null) chips.push({ label: "Pace", value: pace });

    // Weighted recent scoring (last-3-weighted)
    const w3 = (trends.recent_weighted_3 ?? {}) as Record<string, unknown>;
    const wppg = num(w3.ppg);
    if (wppg !== null) chips.push({ label: "PPG (recent)", value: wppg });
  }

  // ---- MLB (latest_summary holds fixed-window pitch/hit metrics) ----
  if (trends.latest_summary && typeof trends.latest_summary === "object") {
    const s = trends.latest_summary as Record<string, unknown>;
    const wp = pct(s.win_pct_10 ?? s.win_pct_5 ?? s.win_pct);
    if (wp) chips.push({ label: "Win% L10", value: wp });
    const ov = pct(s.over_pct_10 ?? s.over_pct_5);
    if (ov) chips.push({ label: "Over% L10", value: ov });
    const ops = num(s.ops10 ?? s.ops5);
    if (ops !== null) chips.push({ label: "OPS L10", value: ops });
    const slg = num(s.slg10 ?? s.slg5);
    if (slg !== null) chips.push({ label: "SLG L10", value: slg });
    const avg = num(s.avg10 ?? s.avg5);
    if (avg !== null) chips.push({ label: "AVG L10", value: avg });
    const era = num(s.era10 ?? s.era5);
    if (era !== null) chips.push({ label: "ERA L10", value: era });
    const whip = num(s.whip10 ?? s.whip5);
    if (whip !== null) chips.push({ label: "WHIP L10", value: whip });
    const k9 = num(s.k9_10 ?? s.k9_5);
    if (k9 !== null) chips.push({ label: "K/9 L10", value: k9 });
  }

  if (chips.length === 0) return <p className="text-xs text-gray-500">No trending stats for this team yet.</p>;
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
            <TrendChips trends={team.trends} sport={sport} />
          </div>
        );
      })}
    </div>
  );
}

/** Side-by-side comparison table with the better side highlighted.
 * Row values are keyed by team ABBREVIATION (e.g. {DET:0.241, TB:0.26}),
 * so we index with homeAbbr/awayAbbr — NEVER teamA/teamB, which may be
 * "Full Name (ABBR)" for some sports (MLB) vs plain abbr (NBA). */
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
    const av = num(row[homeAbbr]);
    const bv = num(row[awayAbbr]);
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
