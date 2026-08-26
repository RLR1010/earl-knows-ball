"use client";

import TeamLogo from "./TeamLogo";
import { MatchupResponse } from "../lib/api";

type Sport = "nfl" | "nba" | "mlb";

// Metrics where a LOWER value is better (highlight leader accordingly).
const LOWER_IS_BETTER = new Set([
  "OPPG",
  "turnovers",
  "TOs",
  "DRTG",
  "defensive rating",
  "ERA",
  "WHIP",
  "opp_avg",
  "Team ERA",
  "Team WHIP",
]);

// Metrics that should always display as 3-decimal precision (hitting + some
// pitching rate stats), regardless of being < 1.5.
const THREE_DECIMAL = new Set([
  "Batting AVG",
  "Batting OBP",
  "Batting SLG",
  "Batting OPS",
  "K Rate",
  "BB Rate",
  "AVG",
  "OBP",
  "SLG",
  "OPS",
  "WHIP",
  "Team WHIP",
  "avg5",
  "obp5",
  "slg5",
  "ops5",
  "avg10",
  "obp10",
  "slg10",
  "ops10",
  "whip5",
  "whip10",
  "k9_5",
  "k9_10",
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
  avg5: "AVG L10",
  avg10: "AVG L10",
  OBP: "On-Base %",
  obp5: "OBP L10",
  obp10: "OBP L10",
  SLG: "Slugging",
  slg5: "SLG L10",
  slg10: "SLG L10",
  OPS: "OPS",
  ops5: "OPS L10",
  ops10: "OPS L10",
  HR: "Home Runs",
  "R/Game": "Runs / Game",
  RA: "Runs Allowed",
  "RA/G": "Runs Allowed / G",
  ERA: "ERA",
  era5: "ERA L10",
  era10: "ERA L10",
  WHIP: "WHIP",
  whip5: "WHIP L10",
  whip10: "WHIP L10",
  k9_5: "K/9 L10",
  k9_10: "K/9 L10",
  "Team ERA": "Team ERA",
  "Team WHIP": "Team WHIP",
  "Team K/9": "Team K/9",
  "Team BB/9": "Team BB/9",
  "Batting AVG": "Batting AVG",
  "Batting OBP": "Batting OBP",
  "Batting SLG": "Batting SLG",
  "Batting OPS": "Batting OPS",
  "K Rate": "K Rate",
  "BB Rate": "BB Rate",
};

function metricLabel(key: string): string {
  return METRIC_LABELS[key] ?? key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function num(v: unknown): number | null {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

/** Format a value with metric-aware precision:
 *  - 3-decimal set (AVG/OBP/SLG/OPS/K rate/BB rate/WHIP) -> 0.241, 1.372
 *  - small ratios <=1.5 that aren't in the 3-dec set -> percentage (e.g. 0.24 -> 24%)
 *  - rates like ERA (2.5-9) -> keep 2 decimals
 *  - otherwise -> plain number. */
function fmtVal(v: unknown, metric?: string): string {
  const n = num(v);
  if (n === null) return String(v ?? "—");
  // 3-decimal set (AVG/OBP/SLG/OPS/K rate/BB rate/WHIP) -> 0.241, 1.372
  if (metric && THREE_DECIMAL.has(metric)) return n.toFixed(3);
  // ERA-style: values between 1.5 and 10 -> 2 decimals
  if (n > 1.5 && n < 10 && metric && (metric.includes("ERA") || metric.includes("WHIP"))) {
    return n.toFixed(2);
  }
  // Small ratio -> percent (e.g. 0.24 -> 24%)
  if (n > 0 && n <= 1.5 && n < 10) return `${(n * 100).toFixed(0)}%`;
  return String(n);
}

/** Shared 3-column side-by-side table (home | stat | away) with green = better.
 * Used for BOTH the "How they're playing" trends and head-to-head numbers. */
function ThreeColTable({
  rows,
  homeAbbr,
  awayAbbr,
  note,
}: {
  rows: {
    av: number | null;
    bv: number | null;
    aBest: boolean;
    bBest: boolean;
    label: string;
    metricKey?: string;
    displayA?: string | null;
    displayB?: string | null;
  }[];
  homeAbbr: string;
  awayAbbr: string;
  note?: string;
}) {
  if (rows.length === 0) {
    return <p className="text-xs text-gray-500">No data available.</p>;
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
          <div key={r.label} className="grid grid-cols-[1fr_auto_1fr] items-center gap-2 px-3 py-1.5 text-sm">
            <span className={`text-right tabular-nums ${r.aBest ? "font-bold text-emerald-300" : "text-gray-300"}`}>
              {r.av === null ? "—" : r.displayA ?? fmtVal(r.av, r.metricKey)}
            </span>
            <span className="w-24 text-center text-[11px] text-gray-500">{r.label}</span>
            <span className={`tabular-nums ${r.bBest ? "font-bold text-emerald-300" : "text-gray-300"}`}>
              {r.bv === null ? "—" : r.displayB ?? fmtVal(r.bv, r.metricKey)}
            </span>
          </div>
        ))}
      </div>
      {note && (
        <div className="border-t border-gray-800 bg-gray-900/40 px-3 py-1.5 text-[11px] text-gray-500">
          <span className="font-medium text-emerald-300">Green</span> {note}
        </div>
      )}
    </div>
  );
}

/** Build aligned [home, stat, away] trend rows from both teams' trend payloads.
 * Each sport has its own shape, so we extract per-sport dimension rows. */
function buildTrendRows(
  homeTrends: Record<string, unknown> | null,
  awayTrends: Record<string, unknown> | null,
  sport: Sport
): { av: number | null; bv: number | null; aBest: boolean; bBest: boolean; label: string; metricKey?: string; displayA?: string | null; displayB?: string | null }[] {
  const rows: { av: number | null; bv: number | null; aBest: boolean; bBest: boolean; label: string; metricKey?: string; displayA?: string | null; displayB?: string | null }[] = [];

  const get = (t: Record<string, unknown> | null, path: (string)[]): number | null => {
    if (!t) return null;
    let cur: unknown = t;
    for (const k of path) {
      if (cur && typeof cur === "object") cur = (cur as Record<string, unknown>)[k];
      else return null;
    }
    return num(cur);
  };

  const row = (label: string, path: string[], lowerBetter = false, metricKey?: string) => {
    const av = get(homeTrends, path);
    const bv = get(awayTrends, path);
    if (av === null && bv === null) return;
    let aBest = false;
    let bBest = false;
    if (av !== null && bv !== null) {
      aBest = lowerBetter ? av < bv : av > bv;
      bBest = lowerBetter ? bv < av : bv > av;
    }
    rows.push({ av, bv, aBest, bBest, label, metricKey });
  };

  if (sport === "nba") {
    const n = (t: Record<string, unknown> | null) => {
      const l10 = (t?.last_10 ?? {}) as Record<string, unknown>;
      const l5 = (t?.last_5 ?? {}) as Record<string, unknown>;
      return { l10, l5 };
    };
    const h = n(homeTrends);
    const a = n(awayTrends);
    const wins = (o: Record<string, unknown>) => (typeof o.wins === "number" ? o.wins : null);
    // Win records: display as X-Y, green = more wins.
    const hw10 = wins(h.l10);
    const aw10 = wins(a.l10);
    if (hw10 !== null || aw10 !== null) {
      const fmt = (w: number | null) => (w === null ? "—" : `${w}-${10 - w}`);
      rows.push({
        av: hw10, bv: aw10,
        aBest: hw10 !== null && aw10 !== null ? hw10 > aw10 : false,
        bBest: hw10 !== null && aw10 !== null ? aw10 > hw10 : false,
        label: "Last 10",
        displayA: hw10 === null ? null : fmt(hw10),
        displayB: aw10 === null ? null : fmt(aw10),
      });
    }
    row("Net rating L10", ["last_10", "net_rating"], false, "Net Rating");
    row("Pace", ["last_10", "pace"], false, "pace");
    // ATS / Over: display X-Y where higher win count is better.
    const ats = (o: Record<string, unknown>) => (typeof o.ats_wins === "number" ? o.ats_wins : null);
    const hwAts = ats(h.l10), awAts = ats(a.l10);
    if (hwAts !== null || awAts !== null) {
      const fmt = (w: number | null) => (w === null ? null : `${w}-${10 - w}`);
      rows.push({
        av: hwAts, bv: awAts,
        aBest: hwAts !== null && awAts !== null ? hwAts > awAts : false,
        bBest: hwAts !== null && awAts !== null ? awAts > hwAts : false,
        label: "ATS L10",
        displayA: fmt(hwAts),
        displayB: fmt(awAts),
      });
    }
  }

  if (sport === "mlb") {
    const s = (t: Record<string, unknown> | null) =>
      (t?.latest_summary ?? {}) as Record<string, unknown>;
    const h = s(homeTrends);
    const a = s(awayTrends);
    row("Win% L10", ["latest_summary", "win_pct_10"], false, "win_pct_10");
    row("Over% L10", ["latest_summary", "over_pct_10"], false, "over_pct_10");
    row("AVG L10", ["latest_summary", "avg10"], false, "avg10");
    row("OPS L10", ["latest_summary", "ops10"], false, "ops10");
    row("SLG L10", ["latest_summary", "slg10"], false, "slg10");
    row("ERA L10", ["latest_summary", "era10"], true, "era10");
    row("WHIP L10", ["latest_summary", "whip10"], true, "whip10");
    row("K/9 L10", ["latest_summary", "k9_10"], false, "k9_10");
  }

  if (sport === "nfl") {
    // NFL trends populate once the regular season starts (rolling stats are
    // REG+POST only; preseason has none).
    row("Win% L10", ["last_10", "win_pct_10"], false, "win_pct_10");
  }

  return rows;
}

/** "How they're playing" — rendered as a head-to-head-style 3-column table
 * (home | stat | away) with green = better, matching the comparison table. */
export function MatchupTrendsGrid({
  teams,
  sport,
}: {
  teams: MatchupResponse["teams"];
  sport: Sport;
}) {
  const h = teams.home.trends;
  const a = teams.away.trends;
  const homeUnavailable = teams.home.trends_error || !h;
  const awayUnavailable = teams.away.trends_error || !a;

  // If a side is unavailable, note it in the header row area only.
  const rows = buildTrendRows(h, a, sport);

  return (
    <div className="space-y-2">
      {rows.length === 0 ? (
        <p className="text-xs text-gray-500">
          {homeUnavailable && awayUnavailable
            ? "Trend data isn't available for this matchup yet."
            : "No trend data available for this matchup."}
        </p>
      ) : (
        <ThreeColTable rows={rows} homeAbbr={teams.home.abbr} awayAbbr={teams.away.abbr} note="= better (last 10)" />
      )}
      {(homeUnavailable || awayUnavailable) && rows.length > 0 && (
        <p className="text-[11px] text-gray-600">
          {homeUnavailable ? `${teams.home.name}: trend data unavailable. ` : ""}
          {awayUnavailable ? `${teams.away.name}: trend data unavailable. ` : ""}
        </p>
      )}
    </div>
  );
}

/** Side-by-side head-to-head numbers with the better side highlighted.
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
    return { av, bv, aBest, bBest, label: metricLabel(key), metricKey: key };
  });

  return <ThreeColTable rows={rows} homeAbbr={homeAbbr} awayAbbr={awayAbbr} note="= better on that stat" />;
}
