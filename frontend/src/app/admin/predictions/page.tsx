"use client";

import { useEffect, useState, useCallback } from "react";

const SPORTS = [
  { key: "nfl", label: "NFL", color: "bg-green-600", emoji: "🏈" },
  { key: "nba", label: "NBA", color: "bg-orange-600", emoji: "🏀" },
  { key: "mlb", label: "MLB", color: "bg-red-600", emoji: "⚾" },
];

const MODEL_META: Record<string,{label:string;color:string;chartColor:string}> = {
  ats: { label: "ATS", color: "text-green-400", chartColor: "#22c55e" },
  ou:  { label: "O/U", color: "text-yellow-400", chartColor: "#facc15" },
  ml:  { label: "ML", color: "text-earl-400", chartColor: "#a78bfa" },
};

interface BettingRow {
  correct: number; incorrect: number; pushes?: number; total: number; pct: number;
  profit?: number; roi?: number;
}

interface YearStats {
  year: number; total_games: number;
  confidence_breakdown: { overall: any[]; ats: any[]; ou: any[]; ml: any[]; };
  ats: BettingRow; ou: BettingRow; ml: BettingRow;
}

interface OverallStats {
  ats: { correct: number; incorrect: number; pct: number; profit?: number; roi?: number };
  ou:  { correct: number; incorrect: number; pct: number; profit?: number; roi?: number };
  ml:  { correct: number; incorrect: number; pct: number; profit?: number; roi?: number };
}

interface PredictionData { sport: string; yearly: YearStats[]; overall: OverallStats; }

interface CalibrationBin {
  bin_lo: number; bin_hi: number; label: string;
  total: number; wins: number; losses: number; pushes: number;
  win_rate: number; profit: number;
  avg_fwd_ev: number;    // forward-looking EV (model confidence × odds)
  avg_cal_ev: number;    // calibrated EV (actual win rate × odds)
  avg_profit_odds: number; // avg profit per $100 bet from odds
}
interface CalibrationData { sport: string; ats: CalibrationBin[]; ou: CalibrationBin[]; ml: CalibrationBin[]; }

interface EvBin {
  bin_lo: number; bin_hi: number; label: string;
  total: number; wins: number; losses: number; pushes: number;
  win_rate: number; profit: number;
}
type EvData = Record<string, EvBin[]>;

// ── SVG Chart Component ──

const W = 700, H = 580, PL = 55, PR = 30, PT = 40, PB = 50, MG = 14;
const CH = (H - PT - PB - MG) / 2;  // height per panel
const CW = W - PL - PR;            // chart width

function CalibrationChart({ model, bins }: { model: string; bins: CalibrationBin[] }) {
  const meta = MODEL_META[model];
  const populated = bins.filter(b => b.total > 0);
  const maxCount = Math.max(...populated.map(b => b.total), 1);
  const maxWR = Math.max(...populated.map(b => b.win_rate), 50);
  const minWR = Math.min(...populated.map(b => b.win_rate), 50);
  const wrRange = Math.max(maxWR - minWR, 10);

  function xPos(binIdx: number): number {
    return PL + (binIdx / 19) * CW;
  }

  function yCal(winRate: number): number {
    return PT + CH - ((winRate - 50) / 50) * CH;
  }

  const volTop = PT + CH + MG;

  function yVol(count: number): number {
    return volTop + CH - (count / maxCount) * CH;
  }

  const idealPoints = [0, 19].map(i => `${xPos(i)},${yCal(50 + 50 * i / 19)}`).join(" ");

  // Build calibration line
  const calLine: string[] = [];
  const calCircles: React.ReactNode[] = [];
  const volBars: React.ReactNode[] = [];

  bins.forEach((b, i) => {
    if (b.total === 0) return;
    const x = xPos(i);
    const y = yCal(b.win_rate);
    calLine.push(`${x},${y}`);

    const r = Math.max(3, Math.min(10, Math.sqrt(b.total) * 1.8));

    calCircles.push(
      <circle key={`cal-${i}`} cx={x} cy={y} r={r}
        fill={meta.chartColor} fillOpacity={0.5}
        stroke={meta.chartColor} strokeWidth={1.5}
        className="cursor-pointer hover:fill-opacity-80" />
    );

    const bHt = (b.total / maxCount) * CH;
    const evColor = b.avg_cal_ev >= 0 ? '#22c55e' : '#ef4444';
    volBars.push(
      <g key={`vol-${i}`}>
        <rect x={x - CW / 42} y={volTop + CH - bHt}
          width={CW / 21} height={bHt}
          fill={evColor} fillOpacity={0.65}
          rx={2} />
        {Math.abs(b.avg_cal_ev) >= 0.5 && <text x={x} y={volTop + CH - bHt - 4}
          textAnchor="middle" fill={evColor} fontSize={9}
          fontWeight={600}>{b.avg_cal_ev > 0 ? '+' : ''}{b.avg_cal_ev.toFixed(0)}</text>}
      </g>
    );
  });

  // Y-axis grid lines for calibration
  const calGridY = [50, 60, 70, 80, 90, 100].map(pct => (
    <g key={`cgy-${pct}`}>
      <line x1={PL} y1={yCal(pct)} x2={PL + CW} y2={yCal(pct)}
        stroke="white" strokeOpacity={0.08} strokeDasharray="3 3" />
      <text x={PL - 6} y={yCal(pct) + 4} textAnchor="end"
        fill="white" fillOpacity={0.5} fontSize={11}>{pct}%</text>
    </g>
  ));

  // Y-axis grid for volume
  const volSteps = [0.25, 0.5, 0.75, 1.0];
  const volGridY = volSteps.map(pct => {
    const vy = volTop + CH * (1 - pct);
    return (
      <g key={`vgy-${pct}`}>
        <line x1={PL} y1={vy} x2={PL + CW} y2={vy}
          stroke="white" strokeOpacity={0.08} strokeDasharray="3 3" />
        <text x={PL - 6} y={vy + 4} textAnchor="end"
          fill="white" fillOpacity={0.5} fontSize={11}>{Math.round(maxCount * pct)}</text>
      </g>
    );
  });

  // X-axis labels (every 5th bin)
  const xLabels = [0, 5, 10, 15, 19].map(idx => {
    const b = bins[idx];
    return (
      <text key={`xl-${idx}`} x={xPos(idx)} y={volTop + CH + 18}
        textAnchor="middle" fill="white" fillOpacity={0.5} fontSize={10}>
        {b.label}
      </text>
    );
  });

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" style={{ width: "100%", height: "auto" }}>
      {/* ── Calibration Curve Panel ── */}
      <rect x={PL} y={PT} width={CW} height={CH} fill="white" fillOpacity={0.02} rx={4} />
      {calGridY}
      {/* Ideal diagonal */}
      <polyline points={idealPoints} fill="none" stroke="white" strokeOpacity={0.2}
        strokeWidth={1.5} strokeDasharray="6 3" />
      <text x={PL + CW - 6} y={yCal(100) - 4} textAnchor="end" fill="white"
        fillOpacity={0.25} fontSize={10} fontStyle="italic">Ideal</text>
      {/* Actual performance line */}
      {calLine.length > 1 && <polyline points={calLine.join(" ")}
        fill="none" stroke={meta.chartColor} strokeWidth={2.5} />}
      {calCircles}
      {/* Panel label */}
      <text x={PL} y={PT - 6} fill={meta.chartColor} fontSize={13} fontWeight={600}>
        Win Rate by Confidence — {meta.label}
      </text>
      {populated.length === 0 && <text x={W/2} y={PT + CH/2} textAnchor="middle"
        fill="white" fillOpacity={0.3} fontSize={14}>No data</text>}

      {/* ── Volume Histogram Panel ── */}
      <rect x={PL} y={volTop} width={CW} height={CH} fill="white" fillOpacity={0.02} rx={4} />
      {volGridY}
      {volBars}
      <text x={PL} y={volTop - 6} fill="white" fillOpacity={0.5}
        fontSize={11}>Games per bucket — bar color = EV</text>
      {/* Legend */}
      <rect x={PL + CW - 130} y={volTop + 6} width={120} height={20}
        fill="white" fillOpacity={0.06} rx={4} />
      <rect x={PL + CW - 124} y={volTop + 10} width={10} height={10}
        fill="#22c55e" fillOpacity={0.65} rx={2} />
      <text x={PL + CW - 110} y={volTop + 18} fill="white" fillOpacity={0.6}
        fontSize={10}>EV &gt; 0</text>
      <rect x={PL + CW - 66} y={volTop + 10} width={10} height={10}
        fill="#ef4444" fillOpacity={0.65} rx={2} />
      <text x={PL + CW - 52} y={volTop + 18} fill="white" fillOpacity={0.6}
        fontSize={10}>EV &lt; 0</text>

      {/* X-axis */}
      <line x1={PL} y1={volTop + CH} x2={PL + CW} y2={volTop + CH}
        stroke="white" strokeOpacity={0.15} />
      {xLabels}
      <text x={W/2} y={volTop + CH + 34} textAnchor="middle"
        fill="white" fillOpacity={0.4} fontSize={11}>Confidence Range</text>
    </svg>
  );
}

// ── Calibration Modal ──

function CalibrationModal({ model, bins, onClose }: {
  model: string; bins: CalibrationBin[]; onClose: () => void;
}) {
  const meta = MODEL_META[model];
  const populated = bins.filter(b => b.total > 0);
  const totalGames = populated.reduce((s, b) => s + b.total, 0);
  const totalWins = populated.reduce((s, b) => s + b.wins, 0);
  const totalDecided = populated.reduce((s, b) => s + b.wins + b.losses, 0);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}>
      <div className="bg-gray-900 border border-white/10 rounded-2xl p-6 max-w-3xl w-full mx-4 max-h-[90vh] overflow-y-auto shadow-2xl"
        onClick={e => e.stopPropagation()}>

        {/* Header */}
        <div className="flex items-center justify-between mb-4">
          <div>
            <h2 className={`text-xl font-bold ${meta.color}`}>{meta.label} Calibration</h2>
            <p className="text-sm text-gray-500 mt-1">
              {totalGames} games ({totalWins}-{totalDecided - totalWins}, avg {(100 * totalWins / Math.max(totalDecided, 1)).toFixed(1)}% WR)
              — PnL: <span className={populated.reduce((s,b) => s + b.profit, 0) >= 0 ? 'text-green-400' : 'text-red-400'}>
                ${populated.reduce((s,b) => s + b.profit, 0).toLocaleString()}</span>
              | Breakeven WR at avg odds: <span className="text-cyan-400">{populated.length > 0 ? (100 / (populated[0].avg_profit_odds + 100) * 100).toFixed(1) : '—'}%</span>
              | Bars = Calibrated EV
            </p>
          </div>
          <button onClick={onClose}
            className="text-gray-500 hover:text-white text-2xl leading-none p-1">&times;</button>
        </div>

        {/* Chart */}
        <CalibrationChart model={model} bins={bins} />

        {/* Detail table */}
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-white/10 text-gray-500">
                <th className="text-left py-2 px-2">Confidence</th>
                <th className="text-right py-2 px-2">Games</th>
                <th className="text-right py-2 px-2">W</th>
                <th className="text-right py-2 px-2">L</th>
                <th className="text-right py-2 px-2">P</th>
                <th className={`text-right py-2 px-2 ${meta.color}`}>Win%</th>
                <th className="text-right py-2 px-2">Avg Odds</th>
                <th className="text-right py-2 px-2">Profit</th>
                <th className="text-right py-2 px-2 text-cyan-400">Cal EV</th>
              </tr>
            </thead>
            <tbody>
              {populated.map((b, i) => (
                <tr key={i} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="py-1.5 px-2 text-white">{b.label}</td>
                  <td className="py-1.5 px-2 text-right text-gray-300">{b.total}</td>
                  <td className="py-1.5 px-2 text-right text-green-400">{b.wins}</td>
                  <td className="py-1.5 px-2 text-right text-red-400">{b.losses}</td>
                  <td className="py-1.5 px-2 text-right text-gray-500">{b.pushes || "—"}</td>
                  <td className={`py-1.5 px-2 text-right font-semibold ${b.win_rate >= 55 ? 'text-green-400' : 'text-red-400'}`}>
                    {b.win_rate}%
                  </td>
                  <td className="py-1.5 px-2 text-right text-gray-400 font-mono">
                    {b.avg_profit_odds > 100 ? '+' : ''}{b.avg_profit_odds.toFixed(1)}
                  </td>
                  <td className={`py-1.5 px-2 text-right font-mono ${b.profit >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {b.profit > 0 ? '+' : ''}{b.profit.toFixed(0)}
                  </td>
                  <td className={`py-1.5 px-2 text-right font-mono ${b.avg_cal_ev >= 0 ? 'text-cyan-400' : 'text-rose-400'}`}>
                    {b.avg_cal_ev > 0 ? '+' : ''}{b.avg_cal_ev.toFixed(2)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

// ── EV Distribution Chart Component ──

const EW = 700, EH = 340, EPL = 60, EPR = 30, EPT = 45, EPB = 60;

function EvChart({ model, bins }: { model: string; bins: EvBin[] }) {
  if (!bins.length) return <div className="text-gray-500 text-sm">No data</div>;

  const meta = MODEL_META[model];
  const maxProfit = Math.max(...bins.map(b => Math.abs(b.profit)), 1);
  const maxCount = Math.max(...bins.map(b => b.total), 1);
  const ECW = EW - EPL - EPR;
  const ECH = EH - EPT - EPB;

  function xPos(i: number): number {
    return EPL + ((i + 0.5) / bins.length) * ECW;
  }

  function yProfit(profit: number): number {
    return EPT + ECH * (0.5 - profit / (2 * maxProfit));
  }

  // Build bars
  const bars: React.ReactNode[] = [];
  const countLabels: React.ReactNode[] = [];
  const xLabels: React.ReactNode[] = [];

  bins.forEach((b, i) => {
    const x = xPos(i);
    const barW = Math.max(ECW / bins.length * 0.7, 6);
    const y0 = EPT + ECH / 2;  // zero line
    const profitNeg = -b.profit < 0 ? b.profit : -b.profit;
    const barH = (Math.abs(b.profit) / maxProfit) * (ECH / 2);
    const yTop = b.profit >= 0 ? y0 - barH : y0;
    const color = b.profit >= 0 ? '#22c55e' : '#ef4444';

    bars.push(
      <rect key={`evb-${i}`} x={x - barW / 2} y={yTop}
        width={barW} height={Math.max(barH, 1)}
        fill={color} fillOpacity={0.7} rx={2} />
    );

    // Game count label: above bar for positive, below bar for negative
    const labelY = b.profit >= 0 ? yTop - 6 : y0 + barH + 14;
    countLabels.push(
      <text key={`evc-${i}`} x={x} y={labelY}
        textAnchor="middle" fill="white" fillOpacity={0.6}
        fontSize={10}>{b.total}</text>
    );

    // X-axis: every other label, horizontal
    if (i % 2 === 0 || i === bins.length - 1) {
      xLabels.push(
        <text key={`evx-${i}`} x={x} y={EH - 10}
          textAnchor="middle" fill="white" fillOpacity={0.5}
          fontSize={10}>
          {b.label}
        </text>
      );
    }
  });

  // Zero line
  const zeroY = EPT + ECH / 2;

  // Y-axis labels
  const yLabels = [];
  for (let pct of [-1, -0.5, 0, 0.5, 1]) {
    const y = EPT + ECH * (0.5 - pct / 2);
    const val = Math.round(pct * maxProfit);
    yLabels.push(
      <g key={`evy-${pct}`}>
        <line x1={EPL} y1={y} x2={EW - EPR} y2={y}
          stroke="white" strokeOpacity={0.06} />
        <text x={EPL - 6} y={y + 4} textAnchor="end"
          fill="white" fillOpacity={0.45} fontSize={10}>
          {val < 0 ? '-' : val > 0 ? '+' : ''}${Math.abs(val)}
        </text>
      </g>
    );
  }

  return (
    <svg viewBox={`0 0 ${EW} ${EH}`} className="w-full h-auto">
      <rect x={EPL} y={EPT} width={ECW} height={ECH} fill="white" fillOpacity={0.02} rx={4} />
      {yLabels}
      <line x1={EPL} y1={zeroY} x2={EW - EPR} y2={zeroY}
        stroke="white" strokeOpacity={0.2} strokeWidth={1} strokeDasharray="4 2" />
      {bars}
      {countLabels}
      {xLabels}
      <text x={EPL} y={EPT - 4} fill={meta.chartColor} fontSize={12} fontWeight={600}>
        Profit by EV Score — {meta.label}
      </text>
      <text x={EW - EPR} y={EPT - 4} textAnchor="end" fill="white" fillOpacity={0.35}
        fontSize={10}>(label = game count)</text>
    </svg>
  );
}

// ── Utilities ──

function StatCell({ label, wins, losses, pushes, pct }: {
  label: string; wins: number; losses: number; pushes?: number; pct: number;
}) {
  const isGood = pct >= 53;
  return (
    <div className="text-center">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-1">{label}</div>
      <div className={`text-lg font-bold ${isGood ? "text-green-400" : "text-red-400"}`}>{pct.toFixed(1)}%</div>
      <div className="text-xs text-gray-500">{wins}-{losses}{pushes ? `-${pushes}` : ""}</div>
    </div>
  );
}

const token = () => localStorage.getItem("earl_token");

// ── Main Page ──

export default function PredictionsPage() {
  const [sport, setSport] = useState("mlb");
  const [data, setData] = useState<PredictionData | null>(null);
  const [calData, setCalData] = useState<CalibrationData | null>(null);
  const [evData, setEvData] = useState<EvData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [calModal, setCalModal] = useState<string | null>(null); // 'ats' | 'ou' | 'ml'

  const fetchAll = useCallback(async (s: string) => {
    setLoading(true);
    setError(null);
    setCalModal(null);
    try {
      const tok = token();
      const [statsRes, calRes, evRes] = await Promise.all([
        fetch(`/api/admin/prediction-stats/${s}`, { headers: { Authorization: `Bearer ${tok}` } }),
        fetch(`/api/admin/prediction-stats/${s}/calibration`, { headers: { Authorization: `Bearer ${tok}` } }),
        fetch(`/api/admin/prediction-stats/${s}/ev-distribution`, { headers: { Authorization: `Bearer ${tok}` } }),
      ]);
      if (!statsRes.ok) throw new Error(`Stats HTTP ${statsRes.status}: ${await statsRes.text()}`);
      if (!calRes.ok) throw new Error(`Calibration HTTP ${calRes.status}: ${await calRes.text()}`);
      if (!evRes.ok) throw new Error(`EV HTTP ${evRes.status}: ${await evRes.text()}`);
      setData(await statsRes.json());
      setCalData(await calRes.json());
      setEvData(await evRes.json());
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchAll(sport); }, [sport, fetchAll]);

  const currentSport = SPORTS.find(s => s.key === sport);

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center gap-4 flex-wrap">
        <h1 className="text-2xl font-bold text-white">Prediction Stats</h1>
        <div className="flex gap-1 bg-white/5 rounded-lg p-1">
          {SPORTS.map((s) => (
            <button key={s.key} onClick={() => setSport(s.key)}
              className={`flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-all ${
                sport === s.key
                  ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
                  : "text-gray-400 hover:text-white hover:bg-white/5"
              }`}
            >
              <span>{s.emoji}</span> {s.label}
            </button>
          ))}
        </div>
      </div>

      {loading && <div className="text-gray-400">Loading prediction stats...</div>}

      {error && (
        <div className="bg-red-900/20 border border-red-800/30 rounded-xl p-6 text-red-300">
          <p className="font-semibold">Failed to load</p>
          <p className="text-sm mt-1 text-red-400">{error}</p>
        </div>
      )}

      {data && !loading && (
        <>
          {/* Overall performance */}
          <div>
            <h3 className="text-lg font-semibold text-white mb-4">
              {currentSport?.emoji} {currentSport?.label} — Overall ({data.yearly.reduce((s, y) => s + y.total_games, 0)} games)
            </h3>
            <div className="grid grid-cols-3 gap-4">
              <div className="bg-white/[0.03] border border-white/10 rounded-2xl p-6 text-center cursor-pointer hover:border-green-500/30 transition-colors"
                onClick={() => setCalModal('ats')}>
                <StatCell label="ATS" wins={data.overall.ats.correct} losses={data.overall.ats.incorrect} pct={data.overall.ats.pct} />
                <div className="text-[10px] text-gray-600 mt-2">Click for calibration chart</div>
              </div>
              <div className="bg-white/[0.03] border border-white/10 rounded-2xl p-6 text-center cursor-pointer hover:border-yellow-500/30 transition-colors"
                onClick={() => setCalModal('ou')}>
                <StatCell label="O/U" wins={data.overall.ou.correct} losses={data.overall.ou.incorrect} pct={data.overall.ou.pct} />
                <div className="text-[10px] text-gray-600 mt-2">Click for calibration chart</div>
              </div>
              <div className="bg-white/[0.03] border border-white/10 rounded-2xl p-6 text-center cursor-pointer hover:border-purple-500/30 transition-colors"
                onClick={() => setCalModal('ml')}>
                <StatCell label="ML" wins={data.overall.ml.correct} losses={data.overall.ml.incorrect} pct={data.overall.ml.pct} />
                <div className="text-[10px] text-gray-600 mt-2">Click for calibration chart</div>
              </div>
            </div>
          </div>

          {/* Year-by-year table */}
          <div>
            <h3 className="text-lg font-semibold text-white mb-4">Year-by-Year</h3>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/10">
                    <th className="text-left py-3 px-4 text-gray-400 font-medium">Year</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium">Games</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium text-green-400">ATS%</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium text-yellow-400">OU%</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium text-earl-400">ML%</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium">ATS $</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium">OU $</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium">ML $</th>
                  </tr>
                </thead>
                <tbody>
                  {data.yearly.map((y) => (
                    <tr key={y.year} className="border-b border-white/5 hover:bg-white/[0.02]">
                      <td className="py-3 px-4 text-white font-medium">{y.year}</td>
                      <td className="py-3 px-4 text-right text-gray-300">{y.total_games}</td>
                      <td className="py-3 px-4 text-right font-semibold">{y.ats.pct.toFixed(1)}%</td>
                      <td className="py-3 px-4 text-right font-semibold">{y.ou.pct.toFixed(1)}%</td>
                      <td className="py-3 px-4 text-right font-semibold">{y.ml.pct.toFixed(1)}%</td>
                      <td className={`py-3 px-4 text-right font-mono ${(y.ats.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(y.ats.profit || 0).toLocaleString()}</td>
                      <td className={`py-3 px-4 text-right font-mono ${(y.ou.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(y.ou.profit || 0).toLocaleString()}</td>
                      <td className={`py-3 px-4 text-right font-mono ${(y.ml.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(y.ml.profit || 0).toLocaleString()}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-white/20 font-bold">
                    <td className="py-3 px-4 text-white">Total</td>
                    <td className="py-3 px-4 text-right text-white">{data.yearly.reduce((s, y) => s + y.total_games, 0)}</td>
                    <td className="py-3 px-4 text-right text-green-400">{data.overall.ats.pct.toFixed(1)}%</td>
                    <td className="py-3 px-4 text-right text-yellow-400">{data.overall.ou.pct.toFixed(1)}%</td>
                    <td className="py-3 px-4 text-right text-earl-400">{data.overall.ml.pct.toFixed(1)}%</td>
                    <td className={`py-3 px-4 text-right font-mono ${(data.overall.ats.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(data.overall.ats.profit || 0).toLocaleString()}</td>
                    <td className={`py-3 px-4 text-right font-mono ${(data.overall.ou.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(data.overall.ou.profit || 0).toLocaleString()}</td>
                    <td className={`py-3 px-4 text-right font-mono ${(data.overall.ml.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>${(data.overall.ml.profit || 0).toLocaleString()}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          </div>

          {/* Calibration Quality by Model — table + click-to-graph */}
          <div>
            <h3 className="text-lg font-semibold text-white mb-4">Calibration Quality by Model</h3>
            <p className="text-xs text-gray-500 mb-4">
              Predictions split into Low (&lt;60%), Medium (60-75%), and High (&gt;75%) confidence brackets.
              Click on any model header to see the full calibration curve and volume histogram.
            </p>

            {['ats','ou','ml'].map(model => {
              const meta = MODEL_META[model];
              const brackets: string[] = [];
              const totals: Record<string,{n:number;ok:number;pushes:number;profit:number}> = {};
              data.yearly.forEach((y: any) => {
                const breakdown = y.confidence_breakdown?.[model] || [];
                breakdown.forEach((b: any) => {
                  if (!brackets.includes(b.bracket)) brackets.push(b.bracket);
                  if (!totals[b.bracket]) totals[b.bracket] = {n:0,ok:0,pushes:0,profit:0};
                  totals[b.bracket].n += b.total;
                  totals[b.bracket].ok += b.correct;
                  totals[b.bracket].pushes += (b.pushes || 0);
                  totals[b.bracket].profit += (b.profit || 0);
                });
              });
              const BRACKET_ORDER = ['Low','Medium','High'];
              brackets.sort((a,b) => BRACKET_ORDER.indexOf(a) - BRACKET_ORDER.indexOf(b));
              if (brackets.length === 0) return null;
              const allBk = BRACKET_ORDER.filter(b => brackets.includes(b));

              return <div key={model} className="mb-8">
                <h4 className={`text-md font-semibold text-white mb-2 cursor-pointer hover:${meta.color} transition-colors`}
                  onClick={() => setCalModal(model)}>
                  {meta.label} — binned by {model}-calibrated confidence
                  <span className="text-[10px] text-gray-600 ml-2">Click for chart</span>
                </h4>
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left py-2 px-3 text-gray-400 font-medium">Year</th>
                        {allBk.map(b => (
                          <th key={b} className="text-center py-2 px-3 text-gray-400 font-medium" colSpan={3}>{b}</th>
                        ))}
                      </tr>
                      <tr className="border-b border-white/5 text-xs text-gray-500">
                        <th></th>
                        {allBk.flatMap(b => [
                          <th key={b+'n'} className="text-right py-1 px-2">#</th>,
                          <th key={b+'p'} className={`text-right py-1 px-2 ${meta.color}`}>{model.toUpperCase()}</th>,
                          <th key={b+'$'} className="text-right py-1 px-2">$</th>,
                        ])}
                      </tr>
                    </thead>
                    <tbody>
                      {data.yearly.map((y: any) => {
                        const byBk: Record<string,any> = {};
                        (y.confidence_breakdown?.[model] || []).forEach((b:any) => byBk[b.bracket] = b);
                        return (
                          <tr key={y.year} className="border-b border-white/5 hover:bg-white/[0.02]">
                            <td className="py-2 px-3 text-white font-medium">{y.year}</td>
                            {allBk.flatMap(b => {
                              const c = byBk[b];
                              return [
                                <td key={b+'n'} className="py-2 px-2 text-right text-gray-300">{c?.total ?? 0}</td>,
                                <td key={b+'p'} className={`py-2 px-2 text-right font-semibold ${c?.pct >= 55 ? 'text-green-400' : 'text-red-400'}`}>{c ? c.pct.toFixed(1)+'%' : '—'}</td>,
                                <td key={b+'$'} className={`py-2 px-2 text-right font-mono text-xs ${(c?.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>{c ? (c.profit > 0 ? '+':'')+c.profit.toFixed(0) : '—'}</td>,
                              ];
                            })}
                          </tr>
                        );
                      })}
                      <tr className="border-t border-white/20 font-bold bg-white/[0.02]">
                        <td className="py-3 px-3 text-white">All Years</td>
                        {allBk.flatMap(b => {
                          const t = totals[b];
                          if (!t) return [<td key={b+'n'}></td>,<td key={b+'p'}></td>,<td key={b+'$'}></td>];
                          const pct = t.n > 0 ? 100 * t.ok / Math.max(t.n - (t.pushes || 0), 1) : 0;
                          return [
                            <td key={b+'n'} className="py-3 px-2 text-right text-gray-300">{t.n}</td>,
                            <td key={b+'p'} className={`py-3 px-2 text-right font-semibold ${pct >= 55 ? 'text-green-400' : 'text-red-400'}`}>{pct.toFixed(1)}%</td>,
                            <td key={b+'$'} className={`py-3 px-2 text-right font-mono text-xs ${(t.profit || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>{(t.profit > 0 ? '+':'')+t.profit.toFixed(0)}</td>,
                          ];
                        })}
                      </tr>
                    </tbody>
                  </table>
                </div>
              </div>;
            })}
          </div>

          {/* EV Distribution by Pick Type */}
          <div>
            <h3 className="text-lg font-semibold text-white mb-4">Profit by Expected Value</h3>
            <p className="text-xs text-gray-500 mb-4">
              Each pick assigned an EV score = (confidence × profit_odds) - ((1 - confidence) × $100).
              Bars show actual profit per EV bucket; labels = game count. Green bars = profitable picks.
            </p>

            <div className="grid grid-cols-1 gap-8">
              {['ats','ou','ml'].map(model => {
                const meta = MODEL_META[model];
                const bins = evData?.[model] || [];
                if (!bins.length) return null;
                const totalProfit = bins.reduce((s, b) => s + b.profit, 0);
                const totalGames = bins.reduce((s, b) => s + b.total, 0);
                return (
                  <div key={model} className="bg-white/[0.02] border border-white/5 rounded-xl p-4">
                    <div className="flex items-center justify-between mb-2">
                      <h4 className={`font-semibold ${meta.color}`}>{meta.label}</h4>
                      <span className="text-xs text-gray-500">
                        {totalGames} games | PnL: <span className={totalProfit >= 0 ? 'text-green-400' : 'text-red-400'}>
                          {totalProfit >= 0 ? '+' : ''}${totalProfit.toLocaleString()}
                        </span>
                      </span>
                    </div>
                    <EvChart model={model} bins={bins} />
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}

      {/* Calibration Modal */}
      {calModal && calData && (
        <CalibrationModal
          model={calModal}
          bins={calData[calModal as keyof CalibrationData] as CalibrationBin[]}
          onClose={() => setCalModal(null)}
        />
      )}
    </div>
  );
}
