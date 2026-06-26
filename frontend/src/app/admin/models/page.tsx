"use client";

import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";

const SPORTS = [
  { key: "mlb", label: "MLB", color: "bg-red-600", emoji: "⚾" },
  { key: "nfl", label: "NFL", color: "bg-green-600", emoji: "🏈" },
  { key: "nba", label: "NBA", color: "bg-orange-600", emoji: "🏀" },
];

interface Feature {
  name: string;
  description: string;
  importance: number;
  category: string;
}

interface FeatureCategory {
  name: string;
  feature_count: number;
  total_importance: number;
  features: string[];
}

interface BettingResult {
  correct: number;
  incorrect: number;
  total: number;
  pct: number;
  pushes?: number;
}

interface BacktestYear {
  test_year: number;
  train_years: number[];
  total_games: number;
  mae: number;
  err_mean?: number;
  err_std?: number;
  within_3?: number;
  within_5?: number;
  ats?: BettingResult;
  ou?: BettingResult;
  ml?: BettingResult;
  auc?: number;
  brier?: number;
}

interface ModelVariant {
  name: string;
  description: string;
  algorithm: string;
  total_features: number;
  features: Feature[];
  feature_categories: FeatureCategory[];
  backtest_results: BacktestYear[];
  overall_mae: number;
  overall_ats: BettingResult | null;
  overall_ou: BettingResult | null;
  overall_ml: BettingResult | null;
  feature_importance_plot: { name: string; importance: number }[];
}

interface HighConfidence {
  threshold: number;
  total: number;
  correct: number;
  pct: number;
  ou_total: number;
  ou_correct: number;
  ou_pct: number;
  ml_total: number;
  ml_correct: number;
  ml_pct: number;
}

interface SportModelDetail {
  sport: string;
  model_type: string;
  description: string;
  algorithm: string;
  training_years: number[];
  test_years: number[];
  total_features: number;
  features: Feature[];
  feature_categories: FeatureCategory[];
  backtest_results: BacktestYear[];
  overall_mae: number;
  overall_ats: BettingResult;
  overall_ou: BettingResult | null;
  overall_ml: BettingResult | null;
  monthly: any[];
  high_confidence: HighConfidence[];
  feature_importance_plot: { name: string; importance: number }[];
  last_updated: string | null;
  model_variants: ModelVariant[];
}

const VARIANT_COLORS: Record<string, { bg: string; border: string; text: string; accent: string }> = {
  "ATS": { bg: "bg-blue-900/20", border: "border-blue-700/40", text: "text-blue-300", accent: "bg-blue-500" },
  "O/U": { bg: "bg-yellow-900/20", border: "border-yellow-700/40", text: "text-yellow-300", accent: "bg-yellow-500" },
  "ML":  { bg: "bg-red-900/20", border: "border-red-700/40", text: "text-red-300", accent: "bg-red-500" },
};

const token = () => localStorage.getItem("earl_token");

function StatCard({ label, value, subtitle, color, size = "default" }: {
  label: string; value: string | number; subtitle?: string; color?: string; size?: "default" | "lg";
}) {
  return (
    <div className="bg-white/[0.03] border border-white/10 rounded-xl p-6 hover:bg-white/[0.05] transition">
      <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold mb-1">{label}</div>
      <div className={`${size === "lg" ? "text-4xl" : "text-3xl"} font-bold ${color || "text-white"}`}>
        {typeof value === "number" && value % 1 !== 0 ? value.toFixed(2) : value}
      </div>
      {subtitle && <div className="text-xs text-gray-500 mt-1">{subtitle}</div>}
    </div>
  );
}

function Bar({ pct, color = "bg-earl-500" }: { pct: number; color?: string }) {
  return (
    <div className="w-full h-2 bg-white/5 rounded-full overflow-hidden">
      <div className={`h-full rounded-full ${color} transition-all duration-500`}
           style={{ width: `${Math.min(pct, 100)}%` }} />
    </div>
  );
}

function FeatureImportanceBar({ name, importance, maxImp, barColor }: { name: string; importance: number; maxImp: number; barColor?: string }) {
  const pct = (importance / maxImp) * 100;
  return (
    <div className="flex items-center gap-3 py-1.5">
      <span className="text-sm text-gray-300 w-28 shrink-0 font-mono text-right">{name}</span>
      <div className="flex-1 h-5 bg-white/5 rounded overflow-hidden">
        <div className={`h-full rounded ${barColor || "bg-gradient-to-r from-earl-600 to-earl-400"}`}
             style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs text-gray-500 w-16 text-right">{(importance * 100).toFixed(2)}%</span>
    </div>
  );
}

function ValuePct({ v, good }: { v: number; good: number }) {
  const isGood = v >= good;
  return (
    <span className={`font-semibold ${isGood ? "text-green-400" : "text-red-400"}`}>
      {v.toFixed(1)}%
    </span>
  );
}

// ── Model Variant Section ──
interface TrainingRunInfo {
  id: string;
  model_type: string;
  algorithm: string;
  trained_at: string;
  created_at?: string;
  training_id: string | null;
  is_current: boolean;
  is_live?: boolean;
  results_json?: any;
}

function getRunOverallOuPct(run: TrainingRunInfo): number | null {
  if (!run.results_json || !Array.isArray(run.results_json)) return null;
  let correct = 0, incorrect = 0;
  for (const yr of run.results_json) {
    if (yr?.ou?.correct !== undefined && yr?.ou?.incorrect !== undefined) {
      correct += yr.ou.correct;
      incorrect += yr.ou.incorrect;
    }
  }
  if (correct + incorrect === 0) return null;
  return Math.round(10000 * correct / (correct + incorrect)) / 100;
}

function getRunOverallMlPct(run: TrainingRunInfo): number | null {
  if (!run.results_json || !Array.isArray(run.results_json)) return null;
  let correct = 0, incorrect = 0;
  for (const yr of run.results_json) {
    if (yr?.ml?.correct !== undefined && yr?.ml?.incorrect !== undefined) {
      correct += yr.ml.correct;
      incorrect += yr.ml.incorrect;
    }
  }
  if (correct + incorrect === 0) return null;
  return Math.round(10000 * correct / (correct + incorrect)) / 100;
}

function getRunOverallAtsPct(run: TrainingRunInfo): number | null {
  if (!run.results_json || !Array.isArray(run.results_json)) return null;
  try {
    let correct = 0, incorrect = 0;
    for (const yr of run.results_json) {
      if (yr && typeof yr === 'object' && 'ats' in yr && yr.ats && typeof yr.ats.correct === 'number' && typeof yr.ats.incorrect === 'number') {
        correct += yr.ats.correct;
        incorrect += yr.ats.incorrect;
      }
    }
    if (correct + incorrect === 0) return null;
    return Math.round(10000 * correct / (correct + incorrect)) / 100;
  } catch { return null; }
}

function ModelVariantSection({ variant: _variant, loadedRunInfo, trainingRuns, onSelectRun, onSetCurrent, onSetLive, onTrainNew, sport }: { variant: ModelVariant; loadedRunInfo?: ModelVariant | null; trainingRuns?: TrainingRunInfo[]; onSelectRun?: (runId: number) => void; onSetCurrent?: (runId: number) => void; onSetLive?: (runId: number) => void; onTrainNew?: (modelType: string) => void; sport?: string }) {
  const variantSource = loadedRunInfo && typeof loadedRunInfo === "object" && !("error" in loadedRunInfo) ? loadedRunInfo : _variant;
  const variant = variantSource;
  const colors = VARIANT_COLORS[variant?.name || "A"] || VARIANT_COLORS["ATS"];
  const [expandedFeat, setExpandedFeat] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string>("");

  return (
    <div className={`rounded-2xl border ${colors.border} ${colors.bg} p-6`}>
      {/* Training Run Selector */}
      <div className="flex items-center gap-3 mb-4 pb-4 border-b border-white/10">
        {trainingRuns && trainingRuns.length > 0 && onSelectRun && (
          <>
            <label className="text-xs text-gray-400 font-medium whitespace-nowrap">Training Run:</label>
            <select
              className="bg-black/40 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-gray-200 focus:outline-none focus:border-earl-500"
            value={selectedRunId}
            onChange={(e) => {
              setSelectedRunId(e.target.value);
              const val = e.target.value;
              if (val === "") onSelectRun(0);
              else onSelectRun(Number(val));
            }}
          >
            <option value="">Current Model</option>
            {trainingRuns.map((run) => {
              const mlPct = getRunOverallMlPct(run);
              const atsPct = getRunOverallAtsPct(run);
              const ouPct = getRunOverallOuPct(run);
              const stats = [
                atsPct !== null ? `ATS ${atsPct}%` : null,
                ouPct !== null ? `OU ${ouPct}%` : null,
                mlPct !== null ? `ML ${mlPct}%` : null,
              ].filter(Boolean).join(" | ");
              return (
                <option key={run.id} value={run.id}>
                  Run #{run.id} — {new Date(run.trained_at || run.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })} — {stats}
                </option>
              );
            })}
          </select>
          {selectedRunId && onSetCurrent && (
            <button
              onClick={() => onSetCurrent(Number(selectedRunId))}
              className="bg-earl-600 hover:bg-earl-500 text-white text-xs px-3 py-1.5 rounded-lg transition-colors"
            >
              Set as Current
            </button>
          )}
          {selectedRunId && onSetLive && (
            <button
              onClick={() => onSetLive(Number(selectedRunId))}
              className="bg-green-600 hover:bg-green-500 text-white text-xs px-3 py-1.5 rounded-lg transition-colors"
            >
              Set as Live
            </button>
          )}
          <div className="ml-auto flex items-center gap-4 text-xs">
            {trainingRuns?.find(r => r.is_current) && (
              <span className="text-gray-400">
                Current: <span className="text-earl-400 font-mono">#{trainingRuns.find(r => r.is_current)!.id}</span>
              </span>
            )}
            {trainingRuns?.find(r => r.is_live) && (
              <span className="text-gray-400">
                Live: <span className="text-green-400 font-mono">#{trainingRuns.find(r => r.is_live)!.id}</span>
              </span>
            )}
          </div>
            </>
          )}
          {onTrainNew && (
            <button
              onClick={() => onTrainNew(variant.name.toLowerCase())}
              className="bg-purple-600 hover:bg-purple-500 text-white text-xs px-3 py-1.5 rounded-lg transition-colors"
            >
              Train New
            </button>
          )}
        </div>

      {/* Header */}
      <div className="flex items-center gap-3 mb-6">
        <span className={`text-2xl font-bold ${colors.text}`}>{variant.name} Model</span>
        <span className={`px-3 py-1 rounded-lg text-xs bg-white/10 ${colors.text}`}>
          {variant.total_features} features
        </span>
        <span className="text-xs text-gray-500 ml-auto">{variant.algorithm}</span>
      </div>

      {variant.description && <p className="text-sm text-gray-400 mb-6 leading-relaxed">{variant.description}</p>}

      {/* Performance cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-6">
        <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
          <div className="text-xs text-gray-500 uppercase font-semibold">MAE</div>
          <div className={`text-2xl font-bold ${colors.text}`}>{variant.overall_mae != null ? variant.overall_mae.toFixed(2) : "-"}</div>
        </div>
        {variant.name !== "O/U" && variant.overall_ats && (
          <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
            <div className="text-xs text-gray-500 uppercase font-semibold">ATS</div>
            <div className={`text-2xl font-bold ${variant.overall_ats.pct >= 53 ? "text-green-400" : "text-red-400"}`}>
              {variant.overall_ats.pct}%
            </div>
            <div className="text-xs text-gray-600">{variant.overall_ats.correct}-{variant.overall_ats.incorrect}{variant.overall_ats.pushes ? `-${variant.overall_ats.pushes}` : ""}</div>
          </div>
        )}
        {variant.name === "O/U" && variant.overall_ou && (
          <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
            <div className="text-xs text-gray-500 uppercase font-semibold">O/U</div>
            <div className={`text-2xl font-bold ${variant.overall_ou.pct >= 53 ? "text-green-400" : "text-red-400"}`}>
              {variant.overall_ou.pct}%
            </div>
            <div className="text-xs text-gray-600">{variant.overall_ou.correct}-{variant.overall_ou.incorrect}{variant.overall_ou.pushes ? `-${variant.overall_ou.pushes}` : ""}</div>
          </div>
        )}
        {/* AUC + Brier cards for ML variant */}
        {variant.name === "ML" && (() => {
          const avgAuc = variant.backtest_results.reduce((s, r) => s + (r.auc || 0), 0) / Math.max(variant.backtest_results.length, 1);
          const avgBrier = variant.backtest_results.reduce((s, r) => s + (r.brier || 0), 0) / Math.max(variant.backtest_results.length, 1);
          return (
            <>
              <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
                <div className="text-xs text-gray-500 uppercase font-semibold">AUC</div>
                <div className={`text-2xl font-bold ${avgAuc >= 0.7 ? "text-purple-400" : "text-yellow-400"}`}>{avgAuc.toFixed(3)}</div>
                <div className="text-xs text-gray-600">ROC area under curve</div>
              </div>
              <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
                <div className="text-xs text-gray-500 uppercase font-semibold">Brier</div>
                <div className={`text-2xl font-bold ${avgBrier <= 0.3 ? "text-cyan-400" : "text-yellow-400"}`}>{avgBrier.toFixed(4)}</div>
                <div className="text-xs text-gray-600">lower is better</div>
              </div>
            </>
          );
        })()}
        {variant.name !== "O/U" && variant.overall_ml && (
          <div className="bg-white/[0.03] border border-white/10 rounded-xl p-4 text-center">
            <div className="text-xs text-gray-500 uppercase font-semibold">ML</div>
            <div className={`text-2xl font-bold ${variant.overall_ml.pct >= 53 ? "text-green-400" : "text-red-400"}`}>
              {variant.overall_ml.pct}%
            </div>
            <div className="text-xs text-gray-600">{variant.overall_ml.correct}-{variant.overall_ml.incorrect}</div>
          </div>
        )}
      </div>

      {/* Year-by-year table */}
      {variant.backtest_results.length > 0 && (
        <div className="mb-6">
          <h4 className="text-sm font-semibold text-gray-300 mb-3">Year-by-Year Backtest</h4>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-white/10">
                  <th className="text-left py-2 px-3 text-gray-500 font-medium">Year</th>
                  <th className="text-right py-2 px-3 text-gray-500 font-medium">Games</th>
                  <th className="text-right py-2 px-3 text-gray-500 font-medium">MAE</th>
                  {variant.name !== "O/U" && variant.overall_ats && <th className="text-right py-2 px-3 text-blue-400 font-medium">ATS%</th>}
                  {variant.name === "O/U" && variant.overall_ou && <th className="text-right py-2 px-3 text-yellow-400 font-medium">OU%</th>}
                  {variant.name !== "O/U" && variant.overall_ml && <th className="text-right py-2 px-3 text-red-400 font-medium">ML%</th>}
                  {variant.name === "ML" && <th className="text-right py-2 px-3 text-purple-400 font-medium">AUC</th>}
                  {variant.name === "ML" && <th className="text-right py-2 px-3 text-cyan-400 font-medium">Brier</th>}
                </tr>
              </thead>
              <tbody>
                {variant.backtest_results.map((r) => (
                  <tr key={r.test_year} className="border-b border-white/5 hover:bg-white/[0.02]">
                    <td className="py-2 px-3 text-white">{r.test_year}</td>
                    <td className="py-2 px-3 text-right text-gray-400">{r.total_games}</td>
                    <td className="py-2 px-3 text-right text-gray-400">{r.mae != null ? r.mae.toFixed(2) : "—"}</td>
                    {variant.name !== "O/U" && variant.overall_ats && <td className="py-2 px-3 text-right">{r.ats ? <ValuePct v={r.ats.pct} good={53} /> : "—"}</td>}
                    {variant.name === "O/U" && variant.overall_ou && <td className="py-2 px-3 text-right">{r.ou ? <ValuePct v={r.ou.pct} good={53} /> : "—"}</td>}
                    {variant.name !== "O/U" && variant.overall_ml && <td className="py-2 px-3 text-right">{r.ml ? <ValuePct v={r.ml.pct} good={53} /> : "—"}</td>}
                    {variant.name === "ML" && <td className="py-2 px-3 text-right font-mono text-purple-400">{(r.auc != null ? r.auc : 0).toFixed(3)}</td>}
                    {variant.name === "ML" && <td className="py-2 px-3 text-right font-mono text-cyan-400">{(r.brier != null ? r.brier : 0).toFixed(4)}</td>}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Feature importance */}
      <div className="mb-6">
        <h4 className="text-sm font-semibold text-gray-300 mb-3">Feature Importance</h4>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <div className="lg:col-span-2 bg-white/[0.02] border border-white/10 rounded-xl p-4">
            {variant.feature_importance_plot.length > 0 ? (
              variant.feature_importance_plot.map((fi) => (
                <FeatureImportanceBar
                  key={fi.name}
                  name={fi.name}
                  importance={fi.importance}
                  maxImp={variant.feature_importance_plot[0]?.importance || 1}
                  barColor={`bg-gradient-to-r ${colors.text.replace("text-", "from-")} ${colors.text.replace("text-", "to-")}`}
                />
              ))
            ) : (
              <p className="text-xs text-gray-500 italic">No feature importance data available</p>
            )}
          </div>
          <div className="bg-white/[0.02] border border-white/10 rounded-xl p-4">
            <h5 className="text-xs font-semibold text-gray-400 mb-3">Feature Categories</h5>
            {variant.feature_categories.map((cat) => (
              <div key={cat.name} className="mb-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-xs text-gray-300">{cat.name}</span>
                  <span className="text-xs text-gray-500">{cat.feature_count}</span>
                </div>
                <Bar pct={cat.total_importance * 100} color={colors.accent} />
                <span className="text-xs text-gray-600">{(cat.total_importance * 100).toFixed(2)}%</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* All features */}
      <div>
        <h4 className="text-sm font-semibold text-gray-300 mb-3">All Features ({variant.total_features})</h4>
        <div className="bg-white/[0.02] border border-white/10 rounded-xl overflow-hidden">
          <table className="w-full text-xs">
            <thead>
              <tr className="border-b border-white/10">
                <th className="text-left py-2 px-3 text-gray-500 font-medium">Feature</th>
                <th className="text-left py-2 px-3 text-gray-500 font-medium">Category</th>
                <th className="text-right py-2 px-3 text-gray-500 font-medium">Importance</th>
              </tr>
            </thead>
            <tbody>
              {variant.features.map((f) => (
                <tr
                  key={f.name}
                  className="border-b border-white/5 hover:bg-white/[0.02] cursor-pointer"
                  onClick={() => setExpandedFeat(expandedFeat === f.name ? null : f.name)}
                >
                  <td className="py-2 px-3">
                    <div className="flex items-center gap-2">
                      <span className={`font-mono ${colors.text}`}>{f.name}</span>
                      {expandedFeat === f.name && (
                        <span className="text-xs text-gray-400 ml-2">{f.description}</span>
                      )}
                    </div>
                  </td>
                  <td className="py-2 px-3">
                    <span className="px-2 py-0.5 rounded text-xs bg-white/5 text-gray-400">{f.category}</span>
                  </td>
                  <td className="py-2 px-3 text-right text-gray-300">{(f.importance * 100).toFixed(2)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

export default function AdminModels() {
  const [sport, setSport] = useState("nfl");
  const [variant, setVariant] = useState<string>("ATS");
  const [data, setData] = useState<SportModelDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedFeat, setExpandedFeat] = useState<string | null>(null);
  const [trainingHistory, setTrainingHistory] = useState<any[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [selectedRunId, setSelectedRunId] = useState<number | null>(null);
  const [loadingRunDetail, setLoadingRunDetail] = useState(false);
  const [loadedRunInfo, setLoadedRunInfo] = useState<any | null>(null);

  // Train New modal state
  const [trainModalOpen, setTrainModalOpen] = useState(false);
  const [trainModalType, setTrainModalType] = useState<string>("ou");

  const openTrainModal = (modelType: string) => {
    // Normalise model type: strip slashes, lowercase, map to known types
    const clean = modelType.toLowerCase().replace(/[^a-z0-9]/g, "");
    const mapped: Record<string, string> = { "ou": "ou", "overunder": "ou", "ats": "ats", "ml": "ml", "moneyline": "ml" };
    setTrainModalType(mapped[clean] || clean);
    setTrainModalOpen(true);
  };

  const fetchModel = useCallback(async (s: string) => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/admin/models/${s}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 100)}`);
      }
      const d = await res.json();
      setData(d);
      // Default to first variant for any sport
      if (d.model_variants?.length > 0) {
        setVariant(d.model_variants[0].name);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
    // Fetch training history
    setHistoryLoading(true);
    try {
      const hRes = await fetch(`/api/admin/training-runs/${s}?limit=100`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (hRes.ok) {
        const hData = await hRes.json();
        setTrainingHistory(hData);
      }
    } catch { /* ignore */ }
    setHistoryLoading(false);
  }, []);

  const loadTrainingRun = useCallback(async (runId: number) => {
    console.log("loadTrainingRun", runId);
    setLoadingRunDetail(true);
    setLoadedRunInfo(null);
    try {
      const res = await fetch(`/api/admin/models/${sport}/from-run/${runId}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      const json = await res.json();
      if (res.ok) {
        setLoadedRunInfo(json.variant || json);
        setSelectedRunId(String(runId));
      } else {
        setLoadedRunInfo({ error: json.detail || "Failed to load" });
      }
    } catch (err) {
      setLoadedRunInfo({ error: "Network error" });
    }
    setLoadingRunDetail(false);
  }, [sport]);

  const setCurrentRun = useCallback(async (runId: number, modelType: string) => {
    try {
      const res = await fetch(`/api/admin/training-runs/${sport}/${modelType}/${runId}/set-current`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) {
        setSelectedRunId(null);
        setLoadedRunInfo(null);
        fetchModel(sport);
      }
    } catch { /* ignore */ }
  }, [sport, fetchModel]);

  useEffect(() => {
    fetchModel(sport);
  }, [sport, fetchModel]);

  const currentSport = SPORTS.find(s => s.key === sport);
  const activeVariant = data?.model_variants?.find(v => v.name === variant);

  if (error) {
    return (
      <div>
        <div className="mb-8 flex items-center gap-4">
          <h1 className="text-2xl font-bold text-white">Prediction Models</h1>
          <SportTabs sports={SPORTS} active={sport} onSelect={setSport} />
        </div>
        <div className="bg-red-900/20 border border-red-800/30 rounded-xl p-6 text-red-300">
          <p className="font-semibold">Failed to load model data</p>
          <p className="text-sm mt-1 text-red-400">{error}</p>
          <p className="text-sm mt-3 text-gray-400">Make sure the backend is running and you are logged in as admin.</p>
        </div>
      </div>
    );
  }

  if (loading || !data) {
    return (
      <div>
        <div className="mb-8 flex items-center gap-4">
          <h1 className="text-2xl font-bold text-white">Prediction Models</h1>
          <SportTabs sports={SPORTS} active={sport} onSelect={setSport} />
        </div>
        <div className="text-gray-400">Loading model data...</div>
      </div>
    );
  }

  return (
    <>
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-center gap-4 flex-wrap">
        <h1 className="text-2xl font-bold text-white">Prediction Models</h1>
        <SportTabs sports={SPORTS} active={sport} onSelect={setSport} />
        {data.last_updated && (
          <span className="text-xs text-gray-500 ml-auto">Updated {data.last_updated}</span>
        )}
      </div>

      {/* Model overview */}
      <div className="bg-white/[0.02] border border-white/10 rounded-2xl p-8">
        <div className="flex items-center gap-3 mb-4">
          <span className="text-2xl">{currentSport?.emoji}</span>
          <h2 className="text-xl font-bold text-white">{data.model_type}</h2>
        </div>
        <p className="text-gray-400 text-sm leading-relaxed whitespace-pre-line">{data.description}</p>
        <div className="mt-4 flex flex-wrap gap-2">
          <span className="px-3 py-1.5 bg-purple-900/30 border border-purple-800/30 rounded-lg text-xs text-purple-300">
            Train: {data.training_years[0]}-{data.training_years[data.training_years.length - 1]}
          </span>
          <span className="px-3 py-1.5 bg-green-900/30 border border-green-800/30 rounded-lg text-xs text-green-300">
            Test: {data.test_years.join(", ")}
          </span>
          <span className="px-3 py-1.5 bg-gray-800/50 border border-gray-700/50 rounded-lg text-xs text-gray-300">
            {data.total_features} total features
          </span>
          <span className="px-3 py-1.5 bg-blue-900/30 border border-blue-800/30 rounded-lg text-xs text-blue-300">
            {data.model_variants.length} model variants
          </span>
        </div>
      </div>

      {/* Overall performance cards */}
      <div>
        <h3 className="text-lg font-semibold text-white mb-4">Overall Performance</h3>
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-4">
          <StatCard label="MAE" value={data.overall_mae} subtitle="Avg prediction error" color="text-blue-400" />
          {variant !== "O/U" && <StatCard label="ATS" value={`${data.overall_ats.pct}%`} subtitle={`${data.overall_ats.correct}-${data.overall_ats.incorrect}`} color="text-green-400" />}
          {variant === "O/U" && data.overall_ou && <StatCard label="O/U" value={`${data.overall_ou.pct}%`} subtitle={`${data.overall_ou.correct}-${data.overall_ou.incorrect}${data.overall_ou.pushes ? `-${data.overall_ou.pushes}` : ""}`} color="text-yellow-400" />}
          {variant !== "O/U" && data.overall_ml && <StatCard label="Moneyline" value={`${data.overall_ml.pct}%`} subtitle={`${data.overall_ml.correct}-${data.overall_ml.incorrect}`} color="text-earl-400" />}
          <StatCard label="Total Games" value={data.backtest_results.reduce((s, r) => s + r.total_games, 0)} subtitle="Across all test years" color="text-purple-400" />
          <StatCard label="Test Years" value={data.test_years.length} subtitle={data.test_years.join(", ")} color="text-gray-300" />
        </div>
      </div>

      {/* Model variant tabs (NFL only) */}
      {data.model_variants.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold text-white mb-4">Specialized Models</h3>
          <div className="flex gap-2 mb-6">
            {data.model_variants.map((v) => {
              const c = VARIANT_COLORS[v.name] || VARIANT_COLORS["ATS"];
              return (
                <button
                  key={v.name}
                  onClick={() => { setVariant(v.name); setLoadedRunInfo(null); }}
                  className={`flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium transition-all ${
                    variant === v.name
                      ? `${c.bg} ${c.border} border ${c.text}`
                      : "text-gray-400 hover:text-white bg-white/5 border border-white/5 hover:border-white/10"
                  }`}
                >
                  <span className={`w-2 h-2 rounded-full ${c.accent}`} />
                  {v.name}
                  <span className="text-xs text-gray-500 ml-1">{v.total_features} feats</span>
                </button>
              );
            })}
          </div>

          {activeVariant && (() => {
            // Map variant names to model_type filter
            const variantModelType: Record<string, string> = {
              "ATS": "ats",
              "O/U": "ou",
              "ML": "ml",
              "Moneyline": "ml",
              "Spread": "ats",
              "Total": "ou",
            };
            const mtFilter = variantModelType[activeVariant.name] || activeVariant.name.toLowerCase();
            const sortedRuns = (trainingHistory || []).sort((a: any, b: any) => {
              const da = new Date(a.trained_at || a.created_at || 0).getTime();
              const db = new Date(b.trained_at || b.created_at || 0).getTime();
              return db - da;
            });
            const filteredRuns = sortedRuns.filter((r: any) => (r.model_type || "").toLowerCase() === mtFilter);
            const handleRunSelect = async (runId: number) => {
              if (runId === 0) {
                setSelectedRunId(null);
                setLoadedRunInfo(null);
              } else {
                await loadTrainingRun(runId);
              }
            };
            const handleSetCurrent = async (runId: number) => {
              try {
                const mtFilter = variantModelType[activeVariant.name] || activeVariant.name.toLowerCase();
                const res = await fetch(`/api/admin/training-runs/${sport}/${mtFilter}/${runId}/set-current`, {
                  method: "POST",
                  headers: { Authorization: `Bearer ${token()}` },
                });
                if (res.ok) {
                  // Refresh the model data to reflect the new current run
                  await fetchModel(sport);
                }
              } catch { /* ignore */ }
            };
            const handleSetLive = async (runId: number) => {
              try {
                const mtFilter = variantModelType[activeVariant.name] || activeVariant.name.toLowerCase();
                const res = await fetch(`/api/admin/training-runs/${sport}/${mtFilter}/${runId}/set-live`, {
                  method: "POST",
                  headers: { Authorization: `Bearer ${token()}` },
                });
                if (res.ok) {
                  await fetchModel(sport);
                }
              } catch { /* ignore */ }
            };
            return <ModelVariantSection key={activeVariant.name} variant={activeVariant} loadedRunInfo={loadedRunInfo} trainingRuns={filteredRuns} onSelectRun={handleRunSelect} onSetCurrent={handleSetCurrent} onSetLive={handleSetLive} onTrainNew={openTrainModal} sport={sport} />;
          })()}
        </div>
      )}

      {/* Year-by-year backtest table (legacy, for MLB/NBA) */}
      {data.model_variants.length === 0 && data.backtest_results.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold text-white mb-4">Year-by-Year Backtest</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10">
                  <th className="text-left py-3 px-4 text-gray-400 font-medium">Year</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Games</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">MAE</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Err μ</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">Err σ</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">±3r</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium">±5r</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium text-green-400">ATS%</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium text-yellow-400">OU%</th>
                  <th className="text-right py-3 px-4 text-gray-400 font-medium text-earl-400">ML%</th>
                </tr>
              </thead>
              <tbody>
                {data.backtest_results.map((r) => (
                  <tr key={r.test_year} className="border-b border-white/5 hover:bg-white/[0.02]">
                    <td className="py-3 px-4 text-white font-medium">{r.test_year}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.total_games}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.mae.toFixed(2)}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.err_mean?.toFixed(2) ?? "—"}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.err_std?.toFixed(2) ?? "—"}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.within_3 != null ? `${(r.within_3 * 100).toFixed(1)}%` : "—"}</td>
                    <td className="py-3 px-4 text-right text-gray-300">{r.within_5 != null ? `${(r.within_5 * 100).toFixed(1)}%` : "—"}</td>
                    <td className="py-3 px-4 text-right">{r.ats ? <ValuePct v={r.ats.pct} good={53} /> : "—"}</td>
                    <td className="py-3 px-4 text-right">{r.ou ? <ValuePct v={r.ou.pct} good={53} /> : "—"}</td>
                    <td className="py-3 px-4 text-right">{r.ml ? <ValuePct v={r.ml.pct} good={53} /> : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* High confidence */}
      {data.high_confidence.length > 0 && (
        <div>
          <h3 className="text-lg font-semibold text-white mb-4">High Confidence Games</h3>
          <p className="text-sm text-gray-500 mb-4">
            Games where the model predicts the largest margins — the higher the confidence threshold,
            the smaller the dataset but the higher the expected accuracy.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10">
                  <th className="text-left py-3 px-4 text-gray-400 font-medium">Confidence</th>
                  <th className="text-center py-3 px-4 text-gray-400 font-medium">Games</th>
                  <th className="text-center py-3 px-4 text-green-400 font-medium">ATS</th>
                  <th className="text-center py-3 px-4 text-yellow-400 font-medium">O/U</th>
                  <th className="text-center py-3 px-4 text-earl-400 font-medium">ML</th>
                </tr>
              </thead>
              <tbody>
                {data.high_confidence.map((hc) => (
                  <tr key={hc.threshold} className="border-b border-white/5">
                    <td className="py-3 px-4 text-white font-medium">Top {hc.threshold.toFixed(0)}%</td>
                    <td className="py-3 px-4 text-center text-gray-400 text-xs">{hc.total} / {hc.ou_total} / {hc.ml_total}</td>
                    <td className="py-3 px-4 text-center"><ValuePct v={hc.pct} good={53} /></td>
                    <td className="py-3 px-4 text-center"><ValuePct v={hc.ou_pct} good={53} /></td>
                    <td className="py-3 px-4 text-center"><ValuePct v={hc.ml_pct} good={53} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="text-xs text-gray-600 mt-2 px-4">Games column: ATS games / O/U games / ML games (may differ due to data availability)</p>
          </div>
        </div>
      )}

      {/* Legacy feature importance + features (for non-NFL sports or fallback) */}
      {data.model_variants.length === 0 && data.features.length > 0 && (
        <>
          <div>
            <h3 className="text-lg font-semibold text-white mb-4">Feature Importance</h3>
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
              <div className="lg:col-span-2 bg-white/[0.02] border border-white/10 rounded-2xl p-6">
                <h4 className="text-sm font-semibold text-gray-300 mb-4">Top Features</h4>
                {data.feature_importance_plot.map((fi) => (
                  <FeatureImportanceBar
                    key={fi.name}
                    name={fi.name}
                    importance={fi.importance}
                    maxImp={data.feature_importance_plot[0]?.importance || 1}
                  />
                ))}
              </div>
              <div className="bg-white/[0.02] border border-white/10 rounded-2xl p-6">
                <h4 className="text-sm font-semibold text-gray-300 mb-4">Feature Categories</h4>
                {data.feature_categories.map((cat) => (
                  <div key={cat.name} className="mb-4">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm text-gray-300">{cat.name}</span>
                      <span className="text-xs text-gray-500">{cat.feature_count} feats</span>
                    </div>
                    <Bar pct={cat.total_importance * 100} color="bg-purple-500" />
                    <span className="text-xs text-gray-600">{(cat.total_importance * 100).toFixed(2)}%</span>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div>
            <h3 className="text-lg font-semibold text-white mb-4">
              All Features ({data.total_features})
            </h3>
            <div className="bg-white/[0.02] border border-white/10 rounded-2xl overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-white/10">
                    <th className="text-left py-3 px-4 text-gray-400 font-medium">Feature</th>
                    <th className="text-left py-3 px-4 text-gray-400 font-medium">Category</th>
                    <th className="text-right py-3 px-4 text-gray-400 font-medium">Importance</th>
                  </tr>
                </thead>
                <tbody>
                  {data.features.map((f) => (
                    <tr
                      key={f.name}
                      className="border-b border-white/5 hover:bg-white/[0.02] cursor-pointer"
                      onClick={() => setExpandedFeat(expandedFeat === f.name ? null : f.name)}
                    >
                      <td className="py-3 px-4">
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-earl-400">{f.name}</span>
                          {expandedFeat === f.name && (
                            <span className="text-xs text-gray-400 ml-2">{f.description}</span>
                          )}
                        </div>
                      </td>
                      <td className="py-3 px-4">
                        <span className="px-2 py-0.5 rounded text-xs bg-white/5 text-gray-400">{f.category}</span>
                      </td>
                      <td className="py-3 px-4 text-right text-gray-300">{(f.importance * 100).toFixed(2)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {/* Training History */}
          <div className="bg-white/5 rounded-xl border border-white/10 p-6">
            <h3 className="text-lg font-semibold mb-4">Training History</h3>
            {historyLoading ? (
              <div className="text-gray-400">Loading…</div>
            ) : trainingHistory.length === 0 ? (
              <div className="text-gray-500 text-sm">No training runs recorded yet.</div>
            ) : (
              <>
                <div className="overflow-x-auto mb-4">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left py-2 px-3 text-gray-400 font-medium">ID</th>
                        <th className="text-left py-2 px-3 text-gray-400 font-medium">Model</th>
                        <th className="text-left py-2 px-3 text-gray-400 font-medium">Trained At</th>
                        <th className="text-center py-2 px-3 text-gray-400 font-medium">Current</th>
                        <th className="text-left py-2 px-3 text-gray-400 font-medium">PKL</th>
                        <th className="text-right py-2 px-3 text-gray-400 font-medium">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {trainingHistory.map((run: any) => (
                        <tr key={run.id} className={`border-b border-white/5 hover:bg-white/[0.02] ${selectedRunId === run.id ? 'bg-blue-500/10' : ''}`}>
                          <td className="py-2 px-3 text-gray-400 text-xs font-mono">{run.id}</td>
                          <td className="py-2 px-3 text-gray-300 capitalize">{run.model_type}</td>
                          <td className="py-2 px-3 text-gray-400 text-xs">
                            {run.trained_at ? new Date(run.trained_at).toLocaleString() : "—"}
                          </td>
                          <td className="py-2 px-3 text-center">
                            {run.is_current ? (
                              <span className="text-green-400 text-xs">Active</span>
                            ) : (
                              <span className="text-gray-600">—</span>
                            )}
                          </td>
                          <td className="py-2 px-3 text-gray-500 text-xs font-mono truncate max-w-[120px]">
                            {run.pkl_filename || "—"}
                          </td>
                          <td className="py-2 px-3 text-right">
                            <div className="flex gap-1 justify-end">
                              <button
                                onClick={() => loadTrainingRun(run.id)}
                                disabled={loadingRunDetail}
                                className="px-2 py-1 text-xs rounded bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white"
                              >
                                {loadingRunDetail && selectedRunId === run.id ? "…" : "Load"}
                              </button>
                              {!run.is_current && (
                                <button
                                  onClick={() => setCurrentRun(run.id, run.model_type)}
                                  className="px-2 py-1 text-xs rounded bg-emerald-700 hover:bg-emerald-600 text-white"
                                >
                                  Set Current
                                </button>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                {/* Loaded run detail */}
                {loadedRunInfo && (
                  <div className="border-t border-white/10 pt-4 mt-2">
                    {loadedRunInfo.error ? (
                      <div className="text-red-400 text-sm">Error: {loadedRunInfo.error}</div>
                    ) : (
                      <div className="text-sm text-gray-300">
                        <div className="flex items-center gap-3 mb-3">
                          <span className="font-medium">Loaded Run (#{selectedRunId || "?"})</span>
                          <span className="text-gray-500">—</span>
                          <span className="text-cyan-400">{loadedRunInfo.name?.toUpperCase() || ""}</span>
                        </div>
                        {loadedRunInfo.backtest_results?.map((yr: any, i: number) => (
                          <div key={i} className="flex gap-4 text-xs bg-black/20 rounded px-3 py-1.5 mb-1">
                            <span className="text-gray-400 min-w-[60px]">{yr.test_year}</span>
                            <span className={yr.name === "O/U" ? "text-cyan-300" : "text-gray-400"}>O/U: {yr.ou_correct}/{yr.ou_total} ({yr.ou_pct})</span>
                            <span className="text-gray-500">MAE: {yr.mae}</span>
                          </div>
                        )) || (
                          <div className="text-gray-500 text-xs">No backtest results in this run.</div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        </>
      )}
    </div>

      {/* ── Train New Modal ── */}
      {trainModalOpen && (
        <TrainFeatureModal
          sport={sport}
          modelType={trainModalType}
          onClose={() => setTrainModalOpen(false)}
          onRefresh={() => { if (sport) fetchModel(sport); }}
        />
      )}
    </>
  );
}


function TrainFeatureModal({ sport, modelType, onClose, onRefresh }: {
  sport: string;
  modelType: string;
  onClose: () => void;
  onRefresh: () => void;
}) {
  const [features, setFeatures] = useState<Array<{name: string; description: string; display_name: string | null; is_trainable: boolean; current_ou: boolean; current_ats: boolean}>>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [status, setStatus] = useState<string | null>(null);

  const loadFeatures = async () => {
    setLoading(true);
    try {
      const res = await api.admin.features.get(sport);
      const feats = res.features;
      setFeatures(feats);
      const col = modelType === "ou" ? "current_ou" : "current_ats" as "current_ou" | "current_ats";
      setSelected(new Set(feats.filter(f => f.is_trainable && f[col]).map(f => f.name)));
    } catch (e: any) {
      setStatus("Failed to load features: " + (e.message || "Unknown error"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { loadFeatures(); }, [sport, modelType]);

  const toggle = (name: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  };

  const submit = async () => {
    const arr = Array.from(selected);
    if (arr.length === 0) { setStatus("Please select at least one feature."); return; }
    setSubmitting(true);
    setStatus("⏳ Starting training...");
    const startedAt = new Date().toISOString();
    try {
      const res = await api.admin.training.trigger(sport, modelType, arr);
      setStatus(`⏳ Training in progress (PID ${res.training_pid})...`);

      // Poll for completion every 3 seconds
      let attempts = 0;
      const maxAttempts = 120; // 6 minutes max
      const poll = async (): Promise<void> => {
        attempts++;
        const runs: any[] = await api.admin.training.getRuns(sport, modelType);
        const completed = runs.find(
          (r: any) => r.is_current && r.results_json && r.trained_at > startedAt
        );
        if (completed) {
          setStatus(`✅ Training complete — ${completed.pkl_filename}`);
          setTimeout(() => { onClose(); onRefresh(); }, 500);
          return;
        }
        if (attempts >= maxAttempts) {
          setStatus("⚠️ Training timed out — check server logs");
          setSubmitting(false);
          return;
        }
        setTimeout(poll, 3000);
      };
      poll();
    } catch (e: any) {
      setStatus("Failed to start training: " + (e.message || "Unknown error"));
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={() => { if (!submitting) onClose(); }}>
      <div className="bg-gray-900 border border-white/15 rounded-xl shadow-2xl w-full max-w-3xl max-h-[85vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-white/10">
          <h2 className="text-lg font-semibold text-white">Train {sport.toUpperCase()} {modelType.toUpperCase()} Model</h2>
          <button
            onClick={() => { if (!submitting) onClose(); }}
            className="text-gray-400 hover:text-white text-xl leading-none transition-colors"
          >
            &times;
          </button>
        </div>

        {/* Feature list */}
        <div className="flex-1 overflow-y-auto px-6 py-4">
          {loading ? (
            <div className="text-gray-400 text-sm py-8 text-center">Loading features...</div>
          ) : features.length === 0 ? (
            <div className="text-gray-500 text-sm py-8 text-center">No features found.</div>
          ) : (
            <>
              <div className="flex items-center gap-3 mb-3 text-xs text-gray-500">
                <button onClick={() => setSelected(new Set(features.map(f => f.name)))} className="text-earl-400 hover:text-earl-300 underline">Select All</button>
                <span>/</span>
                <button onClick={() => setSelected(new Set())} className="text-earl-400 hover:text-earl-300 underline">Clear All</button>
                <span className="ml-auto">{selected.size} / {features.length} selected</span>
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {features.map((feat) => {
                  const isSelected = selected.has(feat.name);
                  return (
                    <label key={feat.name} className={`flex items-start gap-2 px-3 py-2 rounded-lg cursor-pointer text-sm transition-colors ${
                      isSelected ? "bg-earl-600/20 border border-earl-600/30" : "bg-white/5 border border-white/5 hover:bg-white/10"
                    }`}>
                      <input
                        type="checkbox"
                        checked={isSelected}
                        onChange={() => toggle(feat.name)}
                        className="mt-0.5 accent-earl-500"
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-gray-200 font-mono text-xs truncate">{feat.name}</div>
                        {feat.display_name && <div className="text-gray-500 text-[10px] truncate">{feat.display_name}</div>}
                      </div>
                    </label>
                  );
                })}
              </div>
            </>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center gap-3 px-6 py-4 border-t border-white/10">
          <div className="flex-1 text-xs">
            {status && (
              <span className={status.startsWith("✅") ? "text-green-400" : "text-red-400"}>{status}</span>
            )}
          </div>
          <button
            onClick={() => { if (!submitting) onClose(); }}
            className="px-4 py-2 text-sm text-gray-300 hover:text-white transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={submitting || selected.size === 0}
            className="px-5 py-2 text-sm font-medium bg-earl-600 hover:bg-earl-500 disabled:bg-gray-700 disabled:text-gray-500 text-white rounded-lg transition-colors"
          >
            {submitting ? "Starting Training..." : "Start Training"}
          </button>
        </div>
      </div>
    </div>
  );
}

function SportTabs({ sports, active, onSelect }: {
  sports: typeof SPORTS; active: string; onSelect: (s: string) => void;
}) {
  return (
    <div className="flex gap-1 bg-white/5 rounded-lg p-1">
      {sports.map((s) => (
        <button
          key={s.key}
          onClick={() => onSelect(s.key)}
          className={`flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-all ${
            active === s.key
              ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
              : "text-gray-400 hover:text-white hover:bg-white/5"
          }`}
        >
          <span>{s.emoji}</span>
          {s.label}
        </button>
      ))}
    </div>
  );
}
