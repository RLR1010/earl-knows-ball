"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";

// ── Types ───────────────────────────────────────────────────────────────────

interface PickTypeStats {
  games: number;
  wins: number;
  losses: number;
  pushes: number;
  win_pct: number;
}

interface YearlyEntry {
  year: number;
  ats: PickTypeStats;
  ou: PickTypeStats;
  ml: PickTypeStats;
}

interface YearlyData {
  sport: string;
  yearly: YearlyEntry[];
}

interface Summary {
  sport: string;
  ats: PickTypeStats;
  ou: PickTypeStats;
  ml: PickTypeStats;
}

interface CalBin {
  bin_lo: number;
  bin_hi: number;
  label: string;
  total: number;
  wins: number;
  losses: number;
  pushes: number;
  profit: number;
}

interface CalByYear {
  sport: string;
  overall: Record<string, CalBin[]>;
  years: Record<string, Record<string, CalBin[]>>;
}

interface EvBin {
  ev_lo: number;
  ev_hi: number;
  label: string;
  total: number;
  wins: number;
  losses: number;
  profit: number;
}

interface EvData {
  sport: string;
  ev: Record<string, EvBin[]>;
}

interface EvByYear {
  sport: string;
  overall: Record<string, EvBin[]>;
  years: Record<string, Record<string, EvBin[]>>;
}

// ── Model metadata ──────────────────────────────────────────────────────────

const MODEL_META: Record<string, { label: string; color: string; short: string }> = {
  ats: { label: "Against the Spread", color: "text-blue-400", short: "ATS" },
  ou:  { label: "Over / Under",       color: "text-purple-400", short: "OU" },
  ml:  { label: "Moneyline",          color: "text-green-400",  short: "ML" },
};

const MODEL_CHART_COLORS: Record<string, string> = {
  ats: "#60a5fa",
  ou:  "#a78bfa",
  ml:  "#34d399",
};

const SPORT_LABELS: Record<string, string> = {
  nfl: "NFL", nba: "NBA", mlb: "MLB",
};

// ── Helpers ──────────────────────────────────────────────────────────────────

function mergeBins(bins: CalBin[], n: number = 10): CalBin[] {
  if (!bins || bins.length === 0) return [];
  const perGroup = Math.floor(bins.length / n);
  const result: CalBin[] = [];
  for (let i = 0; i < n; i++) {
    const group = bins.slice(i * perGroup, (i + 1) * perGroup);
    result.push({
      bin_lo: group[0].bin_lo,
      bin_hi: group[group.length - 1].bin_hi,
      label: `${(group[0].bin_lo * 100).toFixed(0)}-${(group[group.length - 1].bin_hi * 100).toFixed(0)}%`,
      total: group.reduce((s, b) => s + b.total, 0),
      wins: group.reduce((s, b) => s + b.wins, 0),
      losses: group.reduce((s, b) => s + b.losses, 0),
      pushes: group.reduce((s, b) => s + b.pushes, 0),
      profit: group.reduce((s, b) => s + b.profit, 0),
    });
  }
  return result;
}

function mergeEvBins(bins: EvBin[], n: number = 8): EvBin[] {
  if (!bins || bins.length === 0) return [];
  const nonEmpty = bins.filter((b) => b.total > 0);
  const perGroup = Math.max(Math.ceil(nonEmpty.length / n), 1);
  const result: EvBin[] = [];
  for (let i = 0; i < nonEmpty.length; i += perGroup) {
    const group = nonEmpty.slice(i, i + perGroup);
    result.push({
      ev_lo: group[0].ev_lo,
      ev_hi: group[group.length - 1].ev_hi,
      label: `${group[0].ev_lo}-${group[group.length - 1].ev_hi}`,
      total: group.reduce((s, b) => s + b.total, 0),
      wins: group.reduce((s, b) => s + b.wins, 0),
      losses: group.reduce((s, b) => s + b.losses, 0),
      profit: group.reduce((s, b) => s + b.profit, 0),
    });
  }
  return result;
}

function fmtEvLabel(evLo: number, evHi: number): string {
  const lo = evLo >= 0 ? `+${evLo}` : `${evLo}`;
  const hi = evHi >= 0 ? `+${evHi}` : `${evHi}`;
  return `${lo} TO ${hi}`;
}

// ── Stat card (no PnL) ──────────────────────────────────────────────────────

function StatCard({ label, data }: { label: string; data: PickTypeStats }) {
  const pctColor = data.win_pct >= 53 ? "text-green-400" : data.win_pct >= 50 ? "text-yellow-400" : "text-red-400";
  return (
    <div className="bg-white/[0.03] border border-white/5 rounded-xl p-5 flex flex-col gap-3">
      <div className="text-xs text-gray-500 uppercase tracking-widest font-semibold">{label}</div>
      <div className="flex items-baseline gap-2">
        <span className={`text-3xl font-bold ${pctColor}`}>{data.win_pct.toFixed(1)}%</span>
        <span className="text-sm text-gray-500">win rate</span>
      </div>
      <div className="text-sm text-gray-400">
        {data.wins}-{data.losses}{data.pushes > 0 ? `-${data.pushes}` : ""}
        <span className="text-gray-600"> · </span>
        {data.games} games
      </div>
    </div>
  );
}

// ── Yearly breakdown table (no PnL) ─────────────────────────────────────────

function YearlyTable({ yearly }: { yearly: YearlyEntry[] }) {
  if (!yearly.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-gray-500 uppercase tracking-wider text-xs border-b border-white/5">
            <th className="text-left py-2 pr-4">Year</th>
            <th className="py-2 px-2 text-center">ATS W-L</th>
            <th className="py-2 px-2 text-center">ATS%</th>
            <th className="py-2 px-2 text-center">OU W-L</th>
            <th className="py-2 px-2 text-center">OU%</th>
            <th className="py-2 px-2 text-center">ML W-L</th>
            <th className="py-2 px-2 text-center">ML%</th>
          </tr>
        </thead>
        <tbody>
          {yearly.map((y) => (
            <tr key={y.year} className="border-b border-white/[0.02] hover:bg-white/[0.02]">
              <td className="py-2.5 pr-4 font-medium text-white/80">{y.year}</td>
              <td className="py-2.5 px-2 text-center text-gray-300">{y.ats.wins}-{y.ats.losses}{y.ats.pushes > 0 ? `-${y.ats.pushes}` : ""}</td>
              <td className={`py-2.5 px-2 text-center ${y.ats.win_pct >= 53 ? "text-green-400" : y.ats.win_pct >= 50 ? "text-yellow-400" : "text-red-400"}`}>
                {y.ats.win_pct.toFixed(1)}%
              </td>
              <td className="py-2.5 px-2 text-center text-gray-300">{y.ou.wins}-{y.ou.losses}{y.ou.pushes > 0 ? `-${y.ou.pushes}` : ""}</td>
              <td className={`py-2.5 px-2 text-center ${y.ou.win_pct >= 53 ? "text-green-400" : y.ou.win_pct >= 50 ? "text-yellow-400" : "text-red-400"}`}>
                {y.ou.win_pct.toFixed(1)}%
              </td>
              <td className="py-2.5 px-2 text-center text-gray-300">{y.ml.wins}-{y.ml.losses}</td>
              <td className={`py-2.5 px-2 text-center ${y.ml.win_pct >= 53 ? "text-green-400" : y.ml.win_pct >= 50 ? "text-yellow-400" : "text-red-400"}`}>
                {y.ml.win_pct.toFixed(1)}%
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Calibration chart ────────────────────────────────────────────────────────

function CalibrationChart({ bins, model }: { bins: CalBin[]; model: string }) {
  if (!bins || bins.length === 0) return null;

  const merged = mergeBins(bins, 10)
    .filter((b) => b.total > 0)
    .map((b) => ({
      ...b,
      calConf: b.total > 0 ? b.wins / b.total : 0,
      rawRange: `${(b.bin_lo * 100).toFixed(0)}–${(b.bin_hi * 100).toFixed(0)}%`,
    }))
    .sort((a, b) => a.calConf - b.calConf);

  if (merged.length === 0) return null;

  const W = 1000, H = 480, PL = 60, PR = 30, PT = 48, PB = 90;
  const CW = W - PL - PR, CH = H - PT - PB;
  const groupW = CW / merged.length;
  const barW = Math.max(groupW * 0.28, 22);

  const maxVal = Math.max(...merged.map((b) => Math.max(b.wins, b.losses)), 1);
  const yMax = Math.ceil(maxVal / 5) * 5 || 1;
  const yTicks = [0, Math.round(yMax / 2), yMax];

  const groups = merged.map((b, i) => {
    const cx = PL + i * groupW + groupW / 2;
    const calPct = (b.calConf * 100).toFixed(1);
    const winH = (b.wins / yMax) * CH, lossH = (b.losses / yMax) * CH;
    const wx = cx - groupW * 0.3, lx = cx + groupW * 0.02;
    const baseY = PT + CH;

    return (
      <g key={i}>
        <rect x={wx} y={baseY - winH} width={barW} height={Math.max(winH, 2)} fill={MODEL_CHART_COLORS[model]} fillOpacity={0.75} rx={2} />
        {b.wins > 0 && <text x={wx + barW / 2} y={baseY - winH - 8} textAnchor="middle" fill={MODEL_CHART_COLORS[model]} fontSize={14} fontWeight={700}>{b.wins}</text>}
        <rect x={lx} y={baseY - lossH} width={barW} height={Math.max(lossH, 2)} fill="#ef4444" fillOpacity={0.65} rx={2} />
        {b.losses > 0 && <text x={lx + barW / 2} y={baseY - lossH - 8} textAnchor="middle" fill="#ef4444" fontSize={14} fontWeight={700}>{b.losses}</text>}
        <text x={cx} y={H - PB + 24} textAnchor="middle" fill="white" fillOpacity={0.9} fontSize={15} fontWeight={700}>{calPct}%</text>
        <text x={cx} y={H - PB + 44} textAnchor="middle" fill="white" fillOpacity={0.25} fontSize={12}>({b.rawRange})</text>
      </g>
    );
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
      <rect x={PL} y={PT} width={CW} height={CH} fill="white" fillOpacity={0.02} rx={6} />
      {yTicks.map((t) => {
        const y = PT + CH - (t / yMax) * CH;
        return (<g key={t}>
          <line x1={PL} y1={y} x2={W - PR} y2={y} stroke="white" strokeOpacity={0.08} strokeWidth={1} />
          <text x={PL - 10} y={y + 4} textAnchor="end" fill="white" fillOpacity={0.35} fontSize={12}>{t}</text>
        </g>);
      })}
      {groups}
      <rect x={PL} y={H - 18} width={14} height={14} rx={2} fill={MODEL_CHART_COLORS[model]} fillOpacity={0.75} />
      <text x={PL + 20} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Wins</text>
      <rect x={PL + 80} y={H - 18} width={14} height={14} rx={2} fill="#ef4444" fillOpacity={0.65} />
      <text x={PL + 100} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Losses</text>
      <text x={W / 2} y={H - 4} textAnchor="middle" fill="white" fillOpacity={0.3} fontSize={12}>
        Calibrated Confidence (actual win rate) · raw range in parentheses
      </text>
    </svg>
  );
}

// ── EV PnL chart: single bar per bucket, green/red by profit ─────────────────

function EvPnLChart({ bins, model }: { bins: EvBin[]; model: string }) {
  if (!bins || bins.length === 0) return null;

  const merged = mergeEvBins(bins, 8)
    .filter((b) => b.total > 0)
    .sort((a, b) => a.ev_lo - b.ev_lo);

  if (merged.length === 0) return null;

  const W = 1000, H = 480, PL = 60, PR = 30, PT = 48, PB = 90;
  const CW = W - PL - PR, CH = H - PT - PB;
  const groupW = CW / merged.length;
  const barW = Math.max(groupW * 0.5, 30);

  // Find max absolute profit for Y-axis scaling
  const maxAbsProfit = Math.max(...merged.map((b) => Math.abs(b.profit)), 1);
  const yMax = Math.ceil(maxAbsProfit / 5) * 5 || 1;
  const half = CH / 2;
  const midY = PT + half;

  const yTicks = [];
  for (let t = -yMax; t <= yMax; t += Math.ceil(yMax / 2)) {
    if (t !== 0) yTicks.push(t);
  }

  const groups = merged.map((b, i) => {
    const cx = PL + i * groupW + groupW / 2;
    const profitH = (Math.abs(b.profit) / yMax) * half;
    const label = fmtEvLabel(b.ev_lo, b.ev_hi);
    const wr = b.total > 0 ? (b.wins / b.total * 100).toFixed(1) : "0.0";
    const isPositive = b.profit >= 0;
    const color = isPositive ? "#34d399" : "#ef4444";
    const barY = isPositive ? midY - profitH : midY;
    const barHeight = Math.max(profitH, 2);

    return (
      <g key={i}>
        <rect x={cx - barW / 2} y={barY} width={barW} height={barHeight} fill={color} fillOpacity={0.75} rx={2} />
        {/* $ amount label */}
        {Math.abs(b.profit) > 0 && (
          <text x={cx} y={isPositive ? barY - 8 : barY + barHeight + 18} textAnchor="middle" fill={color} fontSize={14} fontWeight={700}>
            {b.profit >= 0 ? "+" : ""}${Math.round(b.profit).toLocaleString()}
          </text>
        )}
        {/* X-axis label */}
        <text x={cx} y={H - PB + 24} textAnchor="middle" fill="white" fillOpacity={0.9} fontSize={14} fontWeight={700}>
          {label}
        </text>
        {/* Win rate below */}
        <text x={cx} y={H - PB + 44} textAnchor="middle" fill="white" fillOpacity={0.25} fontSize={12}>
          {wr}% WR · {b.total}g
        </text>
      </g>
    );
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
      <rect x={PL} y={PT} width={CW} height={CH} fill="white" fillOpacity={0.02} rx={6} />
      {/* Zero line */}
      <line x1={PL} y1={midY} x2={W - PR} y2={midY} stroke="#fbbf24" strokeOpacity={0.5} strokeWidth={2} strokeDasharray="6 4" />
      <text x={PL - 35} y={midY + 4} textAnchor="end" fill="#fbbf24" fillOpacity={0.5} fontSize={11}>$0</text>
      {/* Y-axis grid */}
      {[Math.round(yMax/2), yMax].map((t) => {
        for (const sign of [1, -1]) {
          const val = sign * t;
          const y = midY - (val / yMax) * half;
          return (
            <g key={val}>
              <line x1={PL} y1={y} x2={W - PR} y2={y} stroke="white" strokeOpacity={0.06} strokeWidth={1} />
              <text x={PL - 10} y={y + 4} textAnchor="end" fill="white" fillOpacity={0.3} fontSize={12}>
                {val >= 0 ? "+" : ""}${val.toLocaleString()}
              </text>
            </g>
          );
        }
      })}
      {groups}
      {/* Legend */}
      <rect x={PL} y={H - 18} width={14} height={14} rx={2} fill="#34d399" fillOpacity={0.75} />
      <text x={PL + 20} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Profit</text>
      <rect x={PL + 90} y={H - 18} width={14} height={14} rx={2} fill="#ef4444" fillOpacity={0.65} />
      <text x={PL + 110} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Loss</text>
      <text x={W / 2} y={H - 4} textAnchor="middle" fill="white" fillOpacity={0.3} fontSize={12}>
        EV Score Range · PnL per bucket · WR and games below each bucket
      </text>
    </svg>
  );
}

// ── EV Record chart: wins/losses grouped bars by EV score ────────────────────

function EvRecordChart({ bins, model }: { bins: EvBin[]; model: string }) {
  if (!bins || bins.length === 0) return null;

  const merged = mergeEvBins(bins, 8)
    .filter((b) => b.total > 0)
    .sort((a, b) => a.ev_lo - b.ev_lo);

  if (merged.length === 0) return null;

  const W = 1000, H = 480, PL = 60, PR = 30, PT = 48, PB = 90;
  const CW = W - PL - PR, CH = H - PT - PB;
  const groupW = CW / merged.length;
  const barW = Math.max(groupW * 0.28, 22);

  const maxVal = Math.max(...merged.map((b) => Math.max(b.wins, b.losses)), 1);
  const yMax = Math.ceil(maxVal / 5) * 5 || 1;
  const yTicks = [0, Math.round(yMax / 2), yMax];

  const groups = merged.map((b, i) => {
    const cx = PL + i * groupW + groupW / 2;
    const winH = (b.wins / yMax) * CH, lossH = (b.losses / yMax) * CH;
    const wx = cx - groupW * 0.3, lx = cx + groupW * 0.02;
    const baseY = PT + CH;
    const label = fmtEvLabel(b.ev_lo, b.ev_hi);
    const wr = b.total > 0 ? (b.wins / b.total * 100).toFixed(1) : "0.0";

    return (
      <g key={i}>
        <rect x={wx} y={baseY - winH} width={barW} height={Math.max(winH, 2)} fill={MODEL_CHART_COLORS[model]} fillOpacity={0.75} rx={2} />
        {b.wins > 0 && <text x={wx + barW / 2} y={baseY - winH - 8} textAnchor="middle" fill={MODEL_CHART_COLORS[model]} fontSize={14} fontWeight={700}>{b.wins}</text>}
        <rect x={lx} y={baseY - lossH} width={barW} height={Math.max(lossH, 2)} fill="#ef4444" fillOpacity={0.65} rx={2} />
        {b.losses > 0 && <text x={lx + barW / 2} y={baseY - lossH - 8} textAnchor="middle" fill="#ef4444" fontSize={14} fontWeight={700}>{b.losses}</text>}
        <text x={cx} y={H - PB + 24} textAnchor="middle" fill="white" fillOpacity={0.9} fontSize={14} fontWeight={700}>{label}</text>
        <text x={cx} y={H - PB + 44} textAnchor="middle" fill="white" fillOpacity={0.25} fontSize={12}>{wr}% WR</text>
      </g>
    );
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto">
      <rect x={PL} y={PT} width={CW} height={CH} fill="white" fillOpacity={0.02} rx={6} />
      {yTicks.map((t) => {
        const y = PT + CH - (t / yMax) * CH;
        return (<g key={t}>
          <line x1={PL} y1={y} x2={W - PR} y2={y} stroke="white" strokeOpacity={0.08} strokeWidth={1} />
          <text x={PL - 10} y={y + 4} textAnchor="end" fill="white" fillOpacity={0.35} fontSize={12}>{t}</text>
        </g>);
      })}
      {groups}
      <rect x={PL} y={H - 18} width={14} height={14} rx={2} fill={MODEL_CHART_COLORS[model]} fillOpacity={0.75} />
      <text x={PL + 20} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Wins</text>
      <rect x={PL + 80} y={H - 18} width={14} height={14} rx={2} fill="#ef4444" fillOpacity={0.65} />
      <text x={PL + 100} y={H - 6} fill="white" fillOpacity={0.7} fontSize={14}>Losses</text>
      <text x={W / 2} y={H - 4} textAnchor="middle" fill="white" fillOpacity={0.3} fontSize={12}>
        EV Score Range · WR below each bucket
      </text>
    </svg>
  );
}

// ── EV Explanation ───────────────────────────────────────────────────────────

function EvExplanation() {
  return (
    <div className="bg-white/[0.03] border border-white/5 rounded-xl p-6 max-w-4xl">
      <h3 className="text-lg font-semibold mb-4">How Expected Value (EV) Works</h3>
      <div className="space-y-4 text-sm text-gray-300 leading-relaxed">
        <p>
          <strong className="text-white">Expected Value (EV)</strong> is a betting metric
          that measures the expected profit or loss on a $100 bet, using the
          model&apos;s <strong className="text-white">calibrated confidence</strong> (actual
          observed win rate at the prediction&apos;s confidence level) and the available odds.
        </p>

        <div className="bg-white/[0.05] rounded-xl p-4 font-mono text-xs text-gray-400 space-y-1">
          <p className="text-gray-300 font-semibold text-sm">The Formula</p>
          <p>EV = (CalibratedWinProb × ProfitAtOdds) − (LossProb × $100)</p>
          <p className="text-gray-500 mt-2">
            Calibrated Win Probability = the actual observed win rate for predictions at
            that confidence level, not the model&apos;s raw confidence.
          </p>
        </div>

        <div className="bg-white/[0.05] rounded-xl p-4 space-y-1">
          <p className="text-gray-300 font-semibold text-sm">Example</p>
          <p className="font-mono text-xs text-gray-400">
            The model says 60% confident on Chiefs -7 at -110 odds.
          </p>
          <p className="font-mono text-xs text-gray-400">
            But historically, predictions at 58–62% confidence won at 57.2% — that&apos;s the calibrated confidence.<br />
            EV = (0.572 × $90.91) − (0.428 × $100) = <strong className="text-green-400">+$9.12</strong>
          </p>
          <p className="text-xs text-gray-500 mt-1">
            Each $100 bet is expected to profit $9.12 on average, using real historical calibration.
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="bg-green-900/20 border border-green-500/20 rounded-xl p-4">
            <h4 className="text-green-400 font-semibold text-sm mb-2">+EV (Positive Value)</h4>
            <p className="text-xs text-gray-400">
              The calibrated win probability exceeds the implied odds probability.
              The model has found real mathematical edge. <strong className="text-green-400">We bet +EV</strong>.
            </p>
          </div>
          <div className="bg-red-900/20 border border-red-500/20 rounded-xl p-4">
            <h4 className="text-red-400 font-semibold text-sm mb-2">-EV (Negative Value)</h4>
            <p className="text-xs text-gray-400">
              The odds don&apos;t offer enough value relative to the calibrated win probability.
              Not profitable over time. <strong className="text-red-400">We avoid -EV.</strong>
            </p>
          </div>
        </div>

        <div className="border-t border-white/5 pt-3 mt-1">
          <p className="text-gray-400 text-xs">
            <strong className="text-white">Reading the chart:</strong> The x-axis shows EV score ranges.
            Bars above the yellow $0 line are profitable buckets; bars below are losing buckets.
            A well-calibrated model should show profit in +EV buckets and losses in -EV buckets.
          </p>
        </div>
      </div>
    </div>
  );
}

// ── Collapsible year section (reusable) ──────────────────────────────────────

function YearSection({ year, content }: { year: string; content: React.ReactNode }) {
  return (
    <details className="border border-white/[0.06] rounded-xl mb-3 group">
      <summary className="flex items-center justify-between px-5 py-3 cursor-pointer hover:bg-white/[0.02] transition-colors list-none">
        <span className="font-semibold">{year}</span>
        <svg className="w-4 h-4 text-gray-500 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </summary>
      <div className="border-t border-white/[0.06] px-5 py-4 space-y-6">
        {content}
      </div>
    </details>
  );
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function ResultsPage() {
  const params = useParams();
  const router = useRouter();
  const sport = (params.sport as string || "nfl").toLowerCase();

  const [summary, setSummary] = useState<Summary | null>(null);
  const [yearly, setYearly] = useState<YearlyData | null>(null);
  const [calByYear, setCalByYear] = useState<CalByYear | null>(null);
  const [evByYear, setEvByYear] = useState<EvByYear | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!["nfl", "nba", "mlb"].includes(sport)) {
      router.replace("/nfl/results");
      return;
    }
    setLoading(true);
    setError(null);

    const apiBase = process.env.NEXT_PUBLIC_API_URL || "";
    Promise.all([
      fetch(`${apiBase}/api/results/${sport}/summary`).then((r) => { if (!r.ok) throw new Error("Failed to load results"); return r.json(); }),
      fetch(`${apiBase}/api/results/${sport}/yearly`).then((r) => r.ok ? r.json() : null),
      fetch(`${apiBase}/api/results/${sport}/calibration-by-year`).then((r) => r.ok ? r.json() : null),
      fetch(`${apiBase}/api/results/${sport}/ev-distribution-by-year`).then((r) => r.ok ? r.json() : null),
    ])
      .then(([s, y, c, e]) => {
        setSummary(s);
        setYearly(y);
        setCalByYear(c);
        setEvByYear(e);
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }, [sport, router]);

  const sportLinks = ["nfl", "nba", "mlb"].filter((s) => s !== sport);

  return (
    <div className="max-w-6xl mx-auto px-4 py-8 space-y-10">
      {/* ── Header ── */}
      <div>
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-2xl font-bold">
            {SPORT_LABELS[sport] || sport.toUpperCase()} Prediction Results
          </h1>
          <div className="flex gap-2">
            {sportLinks.map((s) => (
              <Link key={s} href={`/${s}/results`}
                className="text-xs bg-white/[0.05] hover:bg-white/[0.1] px-2.5 py-1 rounded-full text-gray-400 hover:text-white transition-colors">
                {SPORT_LABELS[s] || s.toUpperCase()}
              </Link>
            ))}
          </div>
        </div>
        <p className="text-sm text-gray-500">
          How our picks perform — win rates, record by calibrated confidence, PnL by EV score, and year-by-year breakdown.
        </p>
      </div>

      {loading && (
        <div className="flex items-center justify-center py-20">
          <div className="animate-spin h-8 w-8 border-2 border-blue-400 border-t-transparent rounded-full" />
        </div>
      )}

      {error && (
        <div className="bg-red-900/30 border border-red-500/30 rounded-xl p-6 text-center">
          <p className="text-red-400 text-sm">{error}</p>
        </div>
      )}

      {!loading && !error && summary && (
        <>
          {/* ── Overall Performance ── */}
          <section>
            <h2 className="text-lg font-semibold mb-4">Overall Performance</h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <StatCard label="Against the Spread (ATS)" data={summary.ats} />
              <StatCard label="Over / Under (O/U)" data={summary.ou} />
              <StatCard label="Moneyline (ML)" data={summary.ml} />
            </div>
          </section>

          {/* ── Year-by-Year Table ── */}
          {yearly && yearly.yearly.length > 0 && (
            <section>
              <h2 className="text-lg font-semibold mb-4">Year-by-Year</h2>
              <YearlyTable yearly={yearly.yearly} />
            </section>
          )}

          {/* ── Record by Calibrated Confidence ── */}
          {calByYear && (
            <section className="space-y-8">
              <div>
                <h2 className="text-lg font-semibold mb-2">Record by Calibrated Confidence</h2>
                <p className="text-sm text-gray-500 max-w-3xl mb-6">
                  For each confidence bucket, the bold number on the x-axis is the
                  calibrated confidence — the actual observed win rate. The parentheses show
                  the model&apos;s raw confidence range. Groups are sorted by calibrated confidence.
                </p>
                {["ats", "ou", "ml"].map((model) => {
                  const bins = calByYear.overall[model];
                  if (!bins || !bins.some((b) => b.total > 0)) return null;
                  return (
                    <div key={model} className="bg-white/[0.02] border border-white/5 rounded-xl p-4 mb-6">
                      <div className="flex items-center justify-between mb-3">
                        <h4 className={`font-semibold ${MODEL_META[model]?.color || "text-gray-300"}`}>
                          {MODEL_META[model]?.label || model.toUpperCase()}
                        </h4>
                        <span className="text-xs text-gray-500">All Years Combined</span>
                      </div>
                      <CalibrationChart bins={bins} model={model} />
                    </div>
                  );
                })}
              </div>

              {/* ── Year-by-Year Calibration ── */}
              <div>
                <h3 className="text-md font-semibold mb-4">Year by Year</h3>
                {Object.entries(calByYear.years).map(([year, binsByModel]) => (
                  <YearSection key={year} year={year} content={
                    <>{[["ats"], ["ou"], ["ml"]].flat().map((model) => {
                      const bins = binsByModel[model];
                      if (!bins || !bins.some((b) => b.total > 0)) return null;
                      return (
                        <div key={model as string}>
                          <h4 className={`text-xs font-semibold uppercase tracking-wider mb-2 ${MODEL_META[model]?.color || "text-gray-300"}`}>
                            {MODEL_META[model]?.label || model}
                          </h4>
                          <CalibrationChart bins={bins} model={model as string} />
                        </div>
                      );
                    })}</>
                  } />
                ))}
              </div>
            </section>
          )}

          {/* ── Record by EV Score ── */}
          {evByYear && (
            <section className="space-y-8">
              <div>
                <h2 className="text-lg font-semibold mb-2">Record by EV Score</h2>
                <p className="text-sm text-gray-500 max-w-3xl mb-6">
                  Wins and losses grouped by Expected Value (EV) score. EV uses the
                  model&apos;s <strong>calibrated confidence</strong> (actual win rate at
                  that confidence level) and available odds. Green bars are wins, red bars
                  are losses in each EV bucket.
                </p>

                {["ats", "ou", "ml"].map((model) => {
                  const bins = evByYear.overall[model];
                  if (!bins || !bins.some((b) => b.total > 0)) return null;
                  const totalWins = bins.reduce((s, b) => s + b.wins, 0);
                  const totalLosses = bins.reduce((s, b) => s + b.losses, 0);
                  return (
                    <div key={model} className="bg-white/[0.02] border border-white/5 rounded-xl p-4 mb-6">
                      <div className="flex items-center justify-between mb-3">
                        <h4 className={`font-semibold ${MODEL_META[model]?.color || "text-gray-300"}`}>
                          {MODEL_META[model]?.label || model.toUpperCase()}
                        </h4>
                        <span className="text-xs text-gray-500">
                          {totalWins}-{totalLosses}
                        </span>
                      </div>
                      <EvRecordChart bins={bins} model={model} />
                    </div>
                  );
                })}
              </div>

              {/* ── Year-by-Year Record ── */}
              <div>
                <h3 className="text-md font-semibold mb-4">Year by Year</h3>
                {Object.entries(evByYear.years).map(([year, binsByModel]) => (
                  <YearSection key={year} year={year} content={
                    <>{"ats ou ml".split(" ").map((model) => {
                      const bins = binsByModel[model];
                      if (!bins || !bins.some((b) => b.total > 0)) return null;
                      return (
                        <div key={model}>
                          <h4 className={`text-xs font-semibold uppercase tracking-wider mb-2 ${MODEL_META[model]?.color || "text-gray-300"}`}>
                            {MODEL_META[model]?.label || model}
                          </h4>
                          <EvRecordChart bins={bins} model={model} />
                        </div>
                      );
                    })}</>
                  } />
                ))}
              </div>

              <div className="border-t border-white/[0.06] pt-8">
                <h2 className="text-lg font-semibold mb-2">PnL by EV Score</h2>
                <p className="text-sm text-gray-500 max-w-3xl mb-6">
                  Predictions grouped by Expected Value (EV) score. EV uses the model&apos;s
                  <strong> calibrated confidence</strong> (actual win rate at that confidence level)
                  to calculate expected profit per $100 bet. Bars above the yellow $0 line are
                  profitable; bars below are losses. A well-calibrated model shows profit in +EV
                  buckets and losses in -EV buckets. WR and game count shown below each bucket.
                </p>

                {["ats", "ou", "ml"].map((model) => {
                  const bins = evByYear.overall[model];
                  if (!bins || !bins.some((b) => b.total > 0)) return null;
                  const totalProfit = bins.reduce((s, b) => s + b.profit, 0);
                  return (
                    <div key={model} className="bg-white/[0.02] border border-white/5 rounded-xl p-4 mb-6">
                      <div className="flex items-center justify-between mb-3">
                        <h4 className={`font-semibold ${MODEL_META[model]?.color || "text-gray-300"}`}>
                          {MODEL_META[model]?.label || model.toUpperCase()}
                        </h4>
                        <span className="text-xs text-gray-500">
                          PnL: <span className={totalProfit >= 0 ? "text-green-400" : "text-red-400"}>
                            {totalProfit >= 0 ? "+" : ""}${totalProfit.toLocaleString()}
                          </span>
                        </span>
                      </div>
                      <EvPnLChart bins={bins} model={model} />
                    </div>
                  );
                })}
              </div>

              {/* ── Year-by-Year EV ── */}
              <div>
                <h3 className="text-md font-semibold mb-4">Year by Year</h3>
                {Object.entries(evByYear.years).map(([year, binsByModel]) => (
                  <YearSection key={year} year={year} content={
                    <>{[["ats"], ["ou"], ["ml"]].flat().map((model) => {
                      const bins = binsByModel[model];
                      if (!bins || !bins.some((b) => b.total > 0)) return null;
                      return (
                        <div key={model as string}>
                          <h4 className={`text-xs font-semibold uppercase tracking-wider mb-2 ${MODEL_META[model]?.color || "text-gray-300"}`}>
                            {MODEL_META[model]?.label || model}
                          </h4>
                          <EvPnLChart bins={bins} model={model as string} />
                        </div>
                      );
                    })}</>
                  } />
                ))}
              </div>

              {/* ── EV Explanation ── */}
              <EvExplanation />
            </section>
          )}
        </>
      )}

      {!loading && !error && !summary && (
        <div className="text-center py-20 text-gray-500">
          <p className="text-lg mb-2">No prediction data available yet</p>
          <p className="text-sm">Results will appear once games have been predicted and settled.</p>
        </div>
      )}
    </div>
  );
}
