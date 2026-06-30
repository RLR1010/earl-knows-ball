"use client";

import { useState, useCallback } from "react";

/* ── Types ────────────────────────────────────────────────────────── */

interface FeatureItem {
  name: string;
  display_name: string;
  group: string;
  description: string;
  value: unknown;
  type: "raw" | "computed";
}

interface GameInfo {
  game_id?: number;
  season_id?: number;
  week?: number;
  home_team?: string;
  away_team?: string;
  ha?: string;
  aa?: string;
  home_score?: number;
  away_score?: number;
  game_date?: string;
  status?: string;
}

interface DataLoaderResponse {
  sport: string;
  game_info: GameInfo;
  total_features: number;
  raw_features: number;
  computed_features: number;
  features: FeatureItem[];
}

/* ── Helpers ──────────────────────────────────────────────────────── */

function formatValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (Number.isNaN(v)) return "—";
    if (Number.isInteger(v)) return v.toLocaleString();
    return v.toFixed(4);
  }
  if (typeof v === "boolean") return v ? "Yes" : "No";
  return String(v);
}

function valueClass(v: unknown): string {
  if (v === null || v === undefined || (typeof v === "number" && Number.isNaN(v))) {
    return "text-gray-500 italic";
  }
  if (typeof v === "number") return "text-earl-300 font-mono";
  return "text-white";
}

const Spinner = () => (
  <svg className="animate-spin h-5 w-5 text-earl-400" fill="none" viewBox="0 0 24 24">
    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
  </svg>
);

/* ── Feature row ──────────────────────────────────────────────────── */

function FeatureRow({ feat }: { feat: FeatureItem }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <tr className="border-b border-white/5 hover:bg-white/[0.02] transition-colors">
      <td className="py-2 pr-4">
        <div className="flex items-center gap-2">
          <span
            className={`w-2 h-2 rounded-full shrink-0 ${
              feat.type === "raw" ? "bg-blue-500" : "bg-amber-500"
            }`}
            title={feat.type === "raw" ? "Raw DB column" : "Computed feature"}
          />
          <span className="text-sm text-gray-300">{feat.display_name}</span>
        </div>
      </td>
      <td className="py-2 pr-4">
        <code className={`text-sm ${valueClass(feat.value)}`}>
          {formatValue(feat.value)}
        </code>
      </td>
      <td className="py-2 pr-4 hidden md:table-cell">
        <span className="text-xs text-gray-600">{feat.group}</span>
      </td>
      <td className="py-2 pr-4 hidden lg:table-cell max-w-[260px]">
        <div className="flex items-center gap-2">
          {feat.description && (
            <button
              onClick={() => setExpanded(!expanded)}
              className="text-gray-500 hover:text-gray-300 text-xs shrink-0"
            >
              {expanded ? "▲" : "▼"}
            </button>
          )}
          <span className="text-xs text-gray-500 truncate">
            {expanded
              ? feat.description
              : feat.description
                ? feat.description.slice(0, 60) + (feat.description.length > 60 ? "…" : "")
                : ""}
          </span>
        </div>
      </td>
      <td className="py-2 text-right">
        <span
          className={`text-[10px] uppercase tracking-wider font-semibold ${
            feat.type === "raw" ? "text-blue-500/60" : "text-amber-500/60"
          }`}
        >
          {feat.type}
        </span>
      </td>
    </tr>
  );
}

/* ── Main Page ────────────────────────────────────────────────────── */

export default function DataLoaderPage() {
  const [sport, setSport] = useState<"nfl" | "mlb">("nfl");
  const [gameId, setGameId] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DataLoaderResponse | null>(null);
  const [searchFilter, setSearchFilter] = useState("");

  const token = (): string | null =>
    typeof window !== "undefined" ? localStorage.getItem("earl_token") : null;

  const handleLoad = useCallback(async () => {
    const gid = parseInt(gameId, 10);
    if (!gid || gid <= 0) {
      setError("Enter a valid game ID (positive integer)");
      return;
    }
    setLoading(true);
    setError(null);
    setResult(null);

    try {
      // Call backend directly to avoid Next.js dev proxy 30s timeout
      const res = await fetch(`http://localhost:8001/api/admin/data-loader/${sport}/load?game_id=${gid}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(body?.detail || `HTTP ${res.status}: ${res.statusText}`);
      }
      const data: DataLoaderResponse = await res.json();
      setResult(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [sport, gameId]);

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") handleLoad();
  };

  /* ── Filter features ────────────────────────────────────────────── */
  const filteredFeatures = result
    ? result.features.filter((f) => {
        if (!searchFilter) return true;
        const q = searchFilter.toLowerCase();
        return (
          f.name.toLowerCase().includes(q) ||
          f.display_name.toLowerCase().includes(q) ||
          f.group.toLowerCase().includes(q)
        );
      })
    : [];

  const rawFeatures = filteredFeatures.filter((f) => f.type === "raw");
  const computedFeatures = filteredFeatures.filter((f) => f.type === "computed");

  /* ── Render ──────────────────────────────────────────────────────── */
  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">🧪 Data Loader</h1>
        <p className="text-gray-400 text-sm mt-1">
          Inspect every feature the data loader produces for a single game
        </p>
      </div>

      {/* Sport & Game ID */}
      <div className="bg-white/[0.03] border border-white/10 rounded-xl p-6 mb-6">
        <div className="flex flex-wrap items-end gap-4">
          {/* Sport toggle */}
          <div>
            <label className="block text-xs text-gray-500 uppercase tracking-wider font-semibold mb-2">
              Sport
            </label>
            <div className="flex rounded-lg overflow-hidden border border-white/10">
              <button
                onClick={() => {
                  setSport("nfl");
                  setResult(null);
                  setError(null);
                }}
                className={`px-5 py-2 text-sm font-medium transition ${
                  sport === "nfl"
                    ? "bg-earl-600/30 text-earl-400 border-r border-white/10"
                    : "bg-white/5 text-gray-400 hover:text-white hover:bg-white/10"
                }`}
              >
                NFL
              </button>
              <button
                onClick={() => {
                  setSport("mlb");
                  setResult(null);
                  setError(null);
                }}
                className={`px-5 py-2 text-sm font-medium transition ${
                  sport === "mlb"
                    ? "bg-earl-600/30 text-earl-400"
                    : "bg-white/5 text-gray-400 hover:text-white hover:bg-white/10"
                }`}
              >
                MLB
              </button>
              <button
                disabled
                className="px-5 py-2 text-sm font-medium bg-white/5 text-gray-600 cursor-not-allowed border-l border-white/10"
                title="NBA data loader not built yet"
              >
                NBA
              </button>
            </div>
          </div>

          {/* Game ID input */}
          <div className="flex-1 min-w-[200px]">
            <label className="block text-xs text-gray-500 uppercase tracking-wider font-semibold mb-2">
              Game ID
            </label>
            <input
              type="number"
              value={gameId}
              onChange={(e) => setGameId(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="e.g. 401671789"
              className="w-full px-4 py-2 bg-black/40 border border-white/10 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-earl-600/50 transition"
            />
          </div>

          <button
            onClick={handleLoad}
            disabled={loading || !gameId}
            className="px-6 py-2 bg-earl-600 hover:bg-earl-500 disabled:bg-earl-800/50 disabled:text-gray-500 text-white rounded-lg text-sm font-medium transition flex items-center gap-2"
          >
            {loading ? (
              <>
                <Spinner />
                Loading...
              </>
            ) : (
              "Load Game"
            )}
          </button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="bg-red-900/20 border border-red-800/30 rounded-xl p-4 mb-6">
          <p className="text-red-300 text-sm">{error}</p>
        </div>
      )}

      {/* Results */}
      {result && (
        <>
          {/* Game summary */}
          <div className="bg-white/[0.03] border border-white/10 rounded-xl p-5 mb-6">
            <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
              <h2 className="text-lg font-semibold text-white">
                Game #{result.game_info.game_id}
              </h2>
              <span className="text-xs text-gray-500">
                {result.total_features} feature{result.total_features !== 1 ? "s" : ""} total
                {" · "}
                <span className="text-blue-400">{result.raw_features} raw</span>
                {" · "}
                <span className="text-amber-400">{result.computed_features} computed</span>
              </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3 text-sm">
              {result.game_info.ha && (
                <div>
                  <span className="text-gray-500 text-xs block">Home</span>
                  <span className="text-white font-semibold">{result.game_info.ha}</span>
                  {result.game_info.home_team && (
                    <span className="text-gray-500 text-xs block">{result.game_info.home_team}</span>
                  )}
                </div>
              )}
              {result.game_info.aa && (
                <div>
                  <span className="text-gray-500 text-xs block">Away</span>
                  <span className="text-white font-semibold">{result.game_info.aa}</span>
                  {result.game_info.away_team && (
                    <span className="text-gray-500 text-xs block">{result.game_info.away_team}</span>
                  )}
                </div>
              )}
              {!result.game_info.ha && result.game_info.home_team && (
                <div>
                  <span className="text-gray-500 text-xs block">Home</span>
                  <span className="text-white">{result.game_info.home_team}</span>
                </div>
              )}
              {!result.game_info.aa && result.game_info.away_team && (
                <div>
                  <span className="text-gray-500 text-xs block">Away</span>
                  <span className="text-white">{result.game_info.away_team}</span>
                </div>
              )}
              {result.game_info.season_id != null && (
                <div>
                  <span className="text-gray-500 text-xs block">Season</span>
                  <span className="text-white">{result.game_info.season_id}</span>
                </div>
              )}
              {result.game_info.week != null && (
                <div>
                  <span className="text-gray-500 text-xs block">Week</span>
                  <span className="text-white">{result.game_info.week}</span>
                </div>
              )}
              {result.game_info.home_score != null && (
                <div>
                  <span className="text-gray-500 text-xs block">Score</span>
                  <span className="text-white">
                    {result.game_info.away_score ?? "?"} – {result.game_info.home_score}
                  </span>
                </div>
              )}
              {result.game_info.game_date && (
                <div>
                  <span className="text-gray-500 text-xs block">Date</span>
                  <span className="text-gray-300">{result.game_info.game_date}</span>
                </div>
              )}
              {result.game_info.status && (
                <div>
                  <span className="text-gray-500 text-xs block">Status</span>
                  <span className="text-gray-300">{result.game_info.status}</span>
                </div>
              )}
            </div>
          </div>

          {/* Search filter */}
          <div className="mb-4 flex items-center gap-3">
            <input
              type="text"
              value={searchFilter}
              onChange={(e) => setSearchFilter(e.target.value)}
              placeholder="Filter features by name, display name, or group..."
              className="flex-1 max-w-md px-4 py-2 bg-black/40 border border-white/10 rounded-lg text-sm text-white placeholder-gray-600 focus:outline-none focus:border-earl-600/50 transition"
            />
            {searchFilter && (
              <span className="text-xs text-gray-500">
                {filteredFeatures.length} of {result.total_features}
              </span>
            )}
          </div>

          {/* Features table */}
          <div className="bg-white/[0.03] border border-white/10 rounded-xl overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-white/10 bg-white/[0.02]">
                    <th className="text-left py-3 px-4 text-xs text-gray-500 uppercase tracking-wider font-semibold">
                      Feature
                    </th>
                    <th className="text-left py-3 px-4 text-xs text-gray-500 uppercase tracking-wider font-semibold">
                      Value
                    </th>
                    <th className="text-left py-3 px-4 text-xs text-gray-500 uppercase tracking-wider font-semibold hidden md:table-cell">
                      Group
                    </th>
                    <th className="text-left py-3 px-4 text-xs text-gray-500 uppercase tracking-wider font-semibold hidden lg:table-cell">
                      Description
                    </th>
                    <th className="text-right py-3 px-4 text-xs text-gray-500 uppercase tracking-wider font-semibold">
                      Type
                    </th>
                  </tr>
                </thead>

                {rawFeatures.length > 0 && (
                  <>
                    <tbody>
                      <tr className="bg-blue-900/10">
                        <td colSpan={5} className="py-2 px-4 text-xs text-blue-400 font-semibold uppercase tracking-wider">
                          📦 Raw Columns ({rawFeatures.length})
                        </td>
                      </tr>
                    </tbody>
                    <tbody>
                      {rawFeatures.map((f) => (
                        <FeatureRow key={f.name} feat={f} />
                      ))}
                    </tbody>
                  </>
                )}

                {computedFeatures.length > 0 && (
                  <>
                    <tbody>
                      <tr className="bg-amber-900/10">
                        <td colSpan={5} className="py-2 px-4 text-xs text-amber-400 font-semibold uppercase tracking-wider">
                          🧮 Computed Features ({computedFeatures.length})
                        </td>
                      </tr>
                    </tbody>
                    <tbody>
                      {computedFeatures.map((f) => (
                        <FeatureRow key={f.name} feat={f} />
                      ))}
                    </tbody>
                  </>
                )}

                {filteredFeatures.length === 0 && (
                  <tbody>
                    <tr>
                      <td colSpan={5} className="py-8 text-center text-gray-500 text-sm">
                        {searchFilter
                          ? `No features match "${searchFilter}"`
                          : "No features loaded"}
                      </td>
                    </tr>
                  </tbody>
                )}
              </table>
            </div>
          </div>

          {/* Legend */}
          <div className="mt-4 flex items-center gap-6 text-xs text-gray-500">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-blue-500 shrink-0" />
              Raw — loaded directly from the games table
            </div>
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-amber-500 shrink-0" />
              Computed — derived via build_features()
            </div>
          </div>
        </>
      )}

      {/* Empty state */}
      {!result && !loading && !error && (
        <div className="bg-white/[0.02] border border-dashed border-white/10 rounded-xl p-12 text-center">
          <p className="text-gray-500 text-lg mb-2">🧪 Data Loader Inspector</p>
          <p className="text-gray-600 text-sm">
            Select a sport, enter a game ID, and click{" "}
            <strong className="text-gray-400">Load Game</strong> to see every
            feature the data loader produces.
          </p>
        </div>
      )}
    </div>
  );
}
