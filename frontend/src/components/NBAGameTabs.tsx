"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PremiumGate from "./PremiumGate";
import ShapBreakdown from "./ShapBreakdown";
import PropBetsTab from "./PropBetsTab";
import EarlsPicksPanel from "./EarlsPicksPanel";
import { useAuth } from "../lib/auth-context";

interface NBABoxScoreData {
  game_id: number;
  nba_game_id: string;
  date: string | null;
  status: string;
  game_type: string;
  venue: string | null;
  attendance: number | null;
  home: NBATeamStats;
  away: NBATeamStats;
  players?: NBAPlayerStat[];
  betting_lines?: NBABettingLines | null;
}

interface NBAPlayerStat {
  player_id: number;
  name: string | null;
  team_id: number;
  team: number | null;
  team_abbr: string | null;
  minutes: string | null;
  field_goals_made: number | null;
  field_goals_attempted: number | null;
  field_goal_pct: number | null;
  three_pointers_made: number | null;
  three_pointers_attempted: number | null;
  three_pointer_pct: number | null;
  free_throws_made: number | null;
  free_throws_attempted: number | null;
  free_throw_pct: number | null;
  rebounds_offensive: number | null;
  rebounds_defensive: number | null;
  rebounds_total: number | null;
  assists: number | null;
  steals: number | null;
  blocks: number | null;
  turnovers: number | null;
  fouls_personal: number | null;
  points: number | null;
  plus_minus: number | null;
}

interface NBABettingLines {
  opening_spread: number | null;
  opening_ou: number | null;
  closing_spread: number | null;
  closing_ou: number | null;
  closing_home_ml: number | null;
  closing_away_ml: number | null;
  closing_spread_home_odds: number | null;
  closing_spread_away_odds: number | null;
  closing_over_odds: number | null;
  closing_under_odds: number | null;
  closing_home_implied_probability: number | null;
  closing_away_implied_probability: number | null;
}

interface NBATeamStats {
  team: string | null;
  team_id: number | null;
  score: number | null;
  field_goals_made: number | null;
  field_goals_attempted: number | null;
  field_goal_pct: number | null;
  three_points_made: number | null;
  three_points_attempted: number | null;
  three_point_pct: number | null;
  free_throws_made: number | null;
  free_throws_attempted: number | null;
  free_throw_pct: number | null;
  rebounds: number | null;
  assists: number | null;
}

interface NBAGameTabsProps {
  gameId: number;
  prediction?: any | null;
}

function PCT(v: number | null): string {
  if (v == null) return "-";
  return (v * 100).toFixed(1) + "%";
}

function formatOdds(odds: number | null): string {
  if (odds == null) return "-";
  return odds > 0 ? `+${odds}` : `${odds}`;
}

function StatRow({ label, home, away, highlight }: { label: string; home: number | null | string; away: number | null | string; highlight?: boolean }) {
  return (
    <tr className="border-t border-white/5">
      <td className={`px-3 py-1.5 text-right ${highlight ? "font-bold" : ""}`}>{away ?? "-"}</td>
      <td className="px-3 py-1.5 text-center text-gray-500 text-[11px] uppercase tracking-wider">{label}</td>
      <td className={`px-3 py-1.5 text-left ${highlight ? "font-bold" : ""}`}>{home ?? "-"}</td>
    </tr>
  );
}

function PlayerTable({ players }: { players: NBAPlayerStat[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs whitespace-nowrap border-collapse">
        <thead>
          <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
            <th className="px-2 py-1.5 text-left sticky left-0 bg-[#0a0a1a]">Player</th>
            <th className="px-2 py-1.5 text-center">MIN</th>
            <th className="px-2 py-1.5 text-center">FG</th>
            <th className="px-2 py-1.5 text-center">3PT</th>
            <th className="px-2 py-1.5 text-center">FT</th>
            <th className="px-2 py-1.5 text-center">OREB</th>
            <th className="px-2 py-1.5 text-center">DREB</th>
            <th className="px-2 py-1.5 text-center">REB</th>
            <th className="px-2 py-1.5 text-center">AST</th>
            <th className="px-2 py-1.5 text-center">STL</th>
            <th className="px-2 py-1.5 text-center">BLK</th>
            <th className="px-2 py-1.5 text-center">TO</th>
            <th className="px-2 py-1.5 text-center">PF</th>
            <th className="px-2 py-1.5 text-center">PTS</th>
            <th className="px-2 py-1.5 text-center">+/-</th>
          </tr>
        </thead>
        <tbody>
          {players.map((p) => (
            <tr key={p.player_id} className="border-t border-white/5">
              <td className="px-2 py-1 sticky left-0 bg-[#0a0a1a] text-white font-medium whitespace-nowrap">{p.name || "—"}</td>
              <td className="px-2 py-1 text-center text-gray-400">{p.minutes || "—"}</td>
              <td className="px-2 py-1 text-center">{p.field_goals_made ?? "—"}/{p.field_goals_attempted ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.three_pointers_made ?? "—"}/{p.three_pointers_attempted ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.free_throws_made ?? "—"}/{p.free_throws_attempted ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.rebounds_offensive ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.rebounds_defensive ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.rebounds_total ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.assists ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.steals ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.blocks ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.turnovers ?? "—"}</td>
              <td className="px-2 py-1 text-center">{p.fouls_personal ?? "—"}</td>
              <td className="px-2 py-1 text-center font-bold text-white">{p.points ?? "—"}</td>
              <td className={`px-2 py-1 text-center ${(p.plus_minus ?? 0) >= 0 ? "text-green-400" : "text-red-400"}`}>{p.plus_minus != null ? (p.plus_minus >= 0 ? "+" : "") + p.plus_minus : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function NBAGameTabs({ gameId, prediction }: NBAGameTabsProps) {
  const [data, setData] = useState<NBABoxScoreData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string>("boxscore");
  const [writeup, setWriteup] = useState<any>(null);
  const [loadingWriteup, setLoadingWriteup] = useState(false);
  const [propBets, setPropBets] = useState<any[] | null>(null);
  const [loadingProps, setLoadingProps] = useState(false);
  const writeupAttempted = useRef(false);

  const isFinal = data?.status === "FINAL" || data?.status === "final";
  const hasBoxscore = !!(data && isFinal);
  const hasProps = !!propBets && propBets.length > 0;

  useEffect(() => {
    if (!gameId) return;
    fetch(`/api/nba/games/${gameId}/boxscore`)
      .then((r) => {
        if (!r.ok) throw new Error(`Status ${r.status}`);
        return r.json();
      })
      .then((d) => {
        if (d.error) {
          setError(d.error);
        } else {
          setData(d);
        }
        setLoading(false);
      })
      .catch((e) => {
        setError(e.message || "Failed to load game");
        setLoading(false);
      });
  }, [gameId]);

  // Default to Game Preview if no boxscore (non-final games)
  useEffect(() => {
    if (!hasBoxscore && !loading) {
      setActiveTab("summary");
    }
  }, [hasBoxscore, loading]);

  // Fetch writeup when Game Preview or Detailed Analysis tab is active
  useEffect(() => {
    if (activeTab === "summary" || activeTab === "analysis") {
      if (!writeup && !loadingWriteup && !writeupAttempted.current) {
        writeupAttempted.current = true;
        setLoadingWriteup(true);
        fetch(`/api/writeups/nba/game/${gameId}`)
          .then((r) => {
            if (!r.ok) throw new Error(`Status ${r.status}`);
            return r.json();
          })
          .then((d) => {
            if (d && !d.error) {
              setWriteup(d);
            }
            setLoadingWriteup(false);
          })
          .catch(() => setLoadingWriteup(false));
      }
    }
  }, [activeTab, gameId]);

  // Load prop bets availability for this game (controls whether the tab shows)
  useEffect(() => {
    if (propBets !== null) return;
    setLoadingProps(true);
    fetch(`/api/nba/games/${gameId}/prop-bets`)
      .then((r) => r.json())
      .then((d) => {
        setPropBets(Array.isArray(d) ? d : []);
        setLoadingProps(false);
      })
      .catch(() => {
        setPropBets([]);
        setLoadingProps(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gameId]);

  const tabs = [
    { key: "boxscore", label: "Box Score", enabled: hasBoxscore },
    { key: "summary", label: "Game Preview", enabled: true },
    { key: "analysis", label: "Detailed Analysis", enabled: true },
    { key: "stats", label: "Detailed Stats", enabled: true },
    { key: "propBets", label: "Prop Bets", enabled: hasProps },
  ];

  const activeTabs = tabs.filter((t) => t.enabled);

  if (loading) {
    return (
      <div className="max-w-3xl mx-auto p-4 space-y-4">
        <div className="border border-white/10 rounded-xl p-6 animate-pulse">
          <div className="h-24 bg-white/5 rounded" />
        </div>
        <div className="text-center py-12 text-gray-500">Loading game...</div>
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="max-w-3xl mx-auto">
        <div className="text-center py-12">
          <div className="text-gray-500 mb-4">{error || "Game not found."}</div>
        </div>
      </div>
    );
  }

  const h = data.home;
  const a = data.away;

  const homePlayers = data.players?.filter(p => p.team_id === data.home.team_id) ?? [];
  const awayPlayers = data.players?.filter(p => p.team_id === data.away.team_id) ?? [];

  const renderBoxScore = () => (
    <div className="space-y-6">
      {/* Team Stats */}
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-earl-400">Team Stats</div>
        <table className="w-full text-sm">
          <thead>
            <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
              <th className="px-3 py-1.5 text-right w-[40%]">{a.team || "Away"}</th>
              <th className="px-3 py-1.5 text-center w-[20%]"></th>
              <th className="px-3 py-1.5 text-left w-[40%]">{h.team || "Home"}</th>
            </tr>
          </thead>
          <tbody>
            <StatRow label="Score" home={h.score} away={a.score} highlight />
            <StatRow label="FG" home={`${h.field_goals_made ?? "-"}/${h.field_goals_attempted ?? "-"}`} away={`${a.field_goals_made ?? "-"}/${a.field_goals_attempted ?? "-"}`} />
            <StatRow label="FG%" home={PCT(h.field_goal_pct)} away={PCT(a.field_goal_pct)} />
            <StatRow label="3PT" home={`${h.three_points_made ?? "-"}/${h.three_points_attempted ?? "-"}`} away={`${a.three_points_made ?? "-"}/${a.three_points_attempted ?? "-"}`} />
            <StatRow label="3PT%" home={PCT(h.three_point_pct)} away={PCT(a.three_point_pct)} />
            <StatRow label="FT" home={`${h.free_throws_made ?? "-"}/${h.free_throws_attempted ?? "-"}`} away={`${a.free_throws_made ?? "-"}/${a.free_throws_attempted ?? "-"}`} />
            <StatRow label="FT%" home={PCT(h.free_throw_pct)} away={PCT(a.free_throw_pct)} />
            <StatRow label="Rebounds" home={h.rebounds} away={a.rebounds} />
            <StatRow label="Assists" home={h.assists} away={a.assists} />
          </tbody>
        </table>
      </div>

      {/* Player Stats */}
      {data.players && data.players.length > 0 && (
        <>
          <div className="border border-white/10 rounded-xl overflow-hidden">
            <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-gray-300">{data.away.team || "Away"}</div>
            <PlayerTable players={awayPlayers} />
          </div>
          <div className="border border-white/10 rounded-xl overflow-hidden">
            <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-white">{data.home.team || "Home"}</div>
            <PlayerTable players={homePlayers} />
          </div>
        </>
      )}
    </div>
  );

  const renderGamePreview = () => {
    if (loadingWriteup) {
      return <div className="text-sm text-gray-400 text-center py-8">Loading game preview...</div>;
    }

    const content = writeup?.public_content;
    if (!content) {
      return (
        <div className="text-sm text-gray-500 text-center py-8">
          No game preview available yet. Check back closer to game time.
        </div>
      );
    }

    return (
      <div className="writeup-content">
        {writeup.title && (
          <div className="text-sm font-semibold text-white mb-3">{writeup.title}</div>
        )}
        <div className="text-gray-300 leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      </div>
    );
  };


  const renderAnalysis = () => {
    if (loadingWriteup) {
      return <div className="text-sm text-gray-400 text-center py-8">Loading detailed analysis...</div>;
    }

    const content = writeup?.premium_content;
    if (!content) {
      return (
        <div className="text-sm text-gray-500 text-center py-8">
          No detailed analysis available yet.
        </div>
      );
    }

    return (
      <div className="writeup-content">
        {writeup.title && (
          <div className="text-sm font-semibold text-white mb-3">{writeup.title}</div>
        )}
        <div className="text-gray-300 leading-relaxed">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
        </div>
      </div>
    );
  };

  // ── Detailed Stats Tab ──────────────────────────────────────────────────
  function DetailedStatsTab({ gameId }: { gameId: number }) {
    const { user } = useAuth();
    const [statsData, setStatsData] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(false);

    useEffect(() => {
      if (!gameId) return;
      setLoading(true);
      setError(false);
      fetch(`/api/handicapping/nba/prediction-stats/${gameId}`)
        .then(res => {
          if (!res.ok) throw new Error("Failed to fetch");
          return res.json();
        })
        .then((data) => {
          setStatsData(data);
          setLoading(false);
        })
        .catch((err) => {
          console.error(err);
          setError(true);
          setLoading(false);
        });
    }, [gameId]);

    if (loading) {
      return (
        <div className="flex items-center justify-center py-12">
          <div className="text-gray-500 text-sm">Loading prediction data...</div>
        </div>
      );
    }

    if (error || !statsData) {
      return (
        <div className="flex items-center justify-center py-12">
          <div className="text-gray-500 text-sm">No prediction data available for this game.</div>
        </div>
      );
    }

    // ── Data sources ──────────────────────────────────────────────────────
    const features = statsData.features || {};
    const homeStats = statsData.home_stats || {};
    const awayStats = statsData.away_stats || {};
    const situational = statsData.situational || {};
    const splits = statsData.splits || {};
    const predicted = statsData.predicted || {};
    const actual = statsData.actual || {};

    // ── Rich value extractor ──────────────────────────────────────────────
    interface FeatureInfo { displayValue: string; displayName: string; description?: string; }
    function getInfo(val: any, fallbackKey?: string): FeatureInfo {
      if (val !== null && typeof val === "object" && "value" in val) {
        const raw = val.value;
        const dv = raw !== null && raw !== undefined
          ? (typeof raw === "number"
              ? (Number.isInteger(raw) ? raw.toLocaleString() : raw.toFixed(4))
              : String(raw))
          : "—";
        const dn = val.display_name || fallbackKey || "";
        return { displayValue: dv, displayName: dn, description: val.description };
      }
      const raw = val;
      const dv = raw !== null && raw !== undefined
        ? (typeof raw === "number"
            ? (Number.isInteger(raw) ? raw.toLocaleString() : raw.toFixed(2))
            : String(raw))
        : "—";
      return { displayValue: dv, displayName: fallbackKey || "", description: undefined };
    }

    function keyToLabel(k: string): string {
      return k
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
    }

    // ── StatRow: label + value + CSS-only tooltip ─────────────────────────
    const StatRow = ({
      label, value, description, valueClass,
    }: { label: string; value: string; description?: string; valueClass?: string }) => (
      <div className="group relative flex items-center justify-between px-2 py-1 rounded hover:bg-white/[0.03] transition-colors">
        <span className={`text-gray-400 truncate text-[11px] ${description ? "cursor-help border-b border-dotted border-gray-600/40 hover:border-gray-400" : ""}`}>
          {label}
        </span>
        <span className={`text-white text-[11px] font-medium tabular-nums ${valueClass || ""}`}>
          {value}
        </span>
        {description && (
          <div className="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block">
            <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-xl px-3 py-2 w-64">
              <div className="text-gray-100 text-[11px] font-semibold mb-1">{label}</div>
              <p className="text-gray-400 text-[10px] leading-relaxed">{description}</p>
            </div>
            <div className="flex justify-center -mt-px">
              <div className="w-0 h-0 border-l-[5px] border-r-[5px] border-t-[5px] border-transparent border-t-gray-700" />
            </div>
          </div>
        )}
      </div>
    );

    // ── SectionHeader ─────────────────────────────────────────────────────
    const SectionHeader = ({ title }: { title: string }) => (
      <div className="flex items-center gap-2 mb-3">
        <span className="text-gray-500 text-[10px] uppercase tracking-[0.12em] font-semibold">{title}</span>
        <div className="flex-1 h-px bg-gradient-to-r from-gray-700/60 to-transparent" />
      </div>
    );

    // ── Stat section renderer ─────────────────────────────────────────────
    function renderStatSection(data: Record<string, any>, gridCols: string) {
      const entries = Object.entries(data);
      if (entries.length === 0) return null;
      return (
        <div className={`grid ${gridCols} gap-x-3 gap-y-0.5`}>
          {entries.map(([key, val]) => {
            const info = getInfo(val, keyToLabel(key));
            return (
              <StatRow
                key={key}
                label={info.displayName || keyToLabel(key)}
                value={info.displayValue}
                description={info.description}
              />
            );
          })}
        </div>
      );
    }

    // ── Features section ──────────────────────────────────────────────────
    function renderFeatures() {
      const entries = Object.entries(features);
      if (entries.length === 0) return null;
      return (
        <div>
          <SectionHeader title="All Model Features" />
          <div className="max-h-96 overflow-y-auto rounded-lg border border-gray-700/30 bg-black/20 p-2">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-3 gap-y-0.5">
              {entries.map(([key, val]) => {
                const info = getInfo(val, keyToLabel(key));
                return (
                  <StatRow
                    key={key}
                    label={info.displayName || keyToLabel(key)}
                    value={info.displayValue}
                    description={info.description}
                  />
                );
              })}
            </div>
          </div>
        </div>
      );
    }

    // ── Splits section ────────────────────────────────────────────────────
    function renderSplits() {
      const entries = Object.entries(splits);
      if (entries.length === 0) return null;
      return (
        <div>
          <SectionHeader title="Splits / Betting Lines" />
          <div className="grid grid-cols-2 md:grid-cols-3 gap-x-3 gap-y-0.5">
            {entries.map(([key, val]) => {
              const info = getInfo(val, keyToLabel(key));
              return (
                <StatRow
                  key={key}
                  label={info.displayName || keyToLabel(key)}
                  value={info.displayValue}
                  description={info.description}
                />
              );
            })}
          </div>
        </div>
      );
    }

    // ── PredCard ──────────────────────────────────────────────────────────
    function PredCard({ label, value, iconClass, tooltip }: {
      label: string; value: string; iconClass: string; tooltip: string;
    }) {
      return (
        <div className={`group/pred relative rounded-lg bg-gradient-to-br ${iconClass} p-3 text-center`}>
          <div className="text-gray-500 text-[10px] uppercase tracking-wide mb-1">{label}</div>
          <div className="text-lg font-bold text-white">{value}</div>
          <div className="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover/pred:block">
            <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-xl px-3 py-2 w-52 text-center">
              <div className="text-gray-100 text-[11px] font-semibold mb-1">{label}</div>
              <p className="text-gray-400 text-[10px] leading-relaxed">{tooltip}</p>
            </div>
            <div className="flex justify-center -mt-px">
              <div className="w-0 h-0 border-l-[5px] border-r-[5px] border-t-[5px] border-transparent border-t-gray-700" />
            </div>
          </div>
        </div>
      );
    }

    const homeEntries = Object.entries(homeStats);
    const awayEntries = Object.entries(awayStats);
    const sitEntries = Object.entries(situational);

    return (
      <div className="space-y-6 text-xs">
        {/* SHAP Feature Attribution (admin only) */}
        {user?.is_admin && statsData?.shap && (
          <ShapBreakdown data={statsData.shap} />
        )}

        {/* Predictions Summary */}
        <div>
          <SectionHeader title="Predictions Summary" />
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <PredCard
              label="Home Score Estimate"
              value={predicted.home_score?.toFixed(1) ?? "—"}
              iconClass="from-earl-500/10 to-transparent border border-earl-500/20"
              tooltip={actual.home_score != null
                ? `Model estimate for the home team's final score — Actual: ${actual.home_score}`
                : "Model estimate for the home team's final score"}
            />
            <PredCard
              label="Away Score Estimate"
              value={predicted.away_score?.toFixed(1) ?? "—"}
              iconClass="from-cyan-500/10 to-transparent border border-cyan-500/20"
              tooltip={actual.away_score != null
                ? `Model estimate for the away team's final score — Actual: ${actual.away_score}`
                : "Model estimate for the away team's final score"}
            />
            <PredCard
              label="Total Estimate"
              value={predicted.total?.toFixed(1) ?? "—"}
              iconClass="from-purple-500/10 to-transparent border border-purple-500/20"
              tooltip={actual.total != null
                ? `Model estimate for total combined score — Actual: ${actual.total}`
                : "Model estimate for total combined score"}
            />
            <PredCard
              label="Margin Estimate"
              value={predicted.margin?.toFixed(1) ?? "—"}
              iconClass="from-amber-500/10 to-transparent border border-amber-500/20"
              tooltip={actual.margin != null
                ? `Model estimate for point differential (home - away) — Actual: ${actual.margin > 0 ? "+" : ""}${actual.margin}`
                : "Model estimate for point differential (home - away)"}
            />
          </div>
        </div>

        {/* Home Team Stats */}
        {homeEntries.length > 0 && (
          <div>
            <SectionHeader title="Home Team Stats" />
            {renderStatSection(homeStats, "grid-cols-2 md:grid-cols-3")}
          </div>
        )}

        {/* Away Team Stats */}
        {awayEntries.length > 0 && (
          <div>
            <SectionHeader title="Away Team Stats" />
            {renderStatSection(awayStats, "grid-cols-2 md:grid-cols-3")}
          </div>
        )}

        {/* Game Context / Situational */}
        {sitEntries.length > 0 && (
          <div>
            <SectionHeader title="Game Context / Situational" />
            {renderStatSection(situational, "grid-cols-2 md:grid-cols-3")}
          </div>
        )}

        {/* Splits / Betting Lines */}
        {renderSplits()}

        {/* All Model Features */}
        {renderFeatures()}
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-4">

      {/* Score Card */}
      <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 mb-2">
        <div className="flex justify-between items-center">
          {/* Away team */}
          <div className="flex-1 text-center">
            <div className="text-2xl font-bold text-gray-300">{a.team || "???"}</div>
            <div className="text-5xl font-black mt-2">{a.score ?? "-"}</div>
          </div>

          {/* Status / @ */}
          <div className="flex-shrink-0 mx-6 text-center">
            {isFinal ? (
              <div className="text-green-400 font-bold text-lg">FINAL</div>
            ) : (
              <div className="text-yellow-400 text-sm">{data.status}</div>
            )}
            <div className="text-gray-500 text-xs mt-1">{data.game_type === "PRE" ? "Preseason" : data.game_type === "POST" ? "Postseason" : "Regular Season"}</div>
            {data.date && <div className="text-gray-500 text-xs mt-0.5">{new Date(data.date).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" })}</div>}
          </div>

          {/* Home team */}
          <div className="flex-1 text-center">
            <div className="text-2xl font-bold text-white">{h.team || "???"}</div>
            <div className="text-5xl font-black mt-2">{h.score ?? "-"}</div>
          </div>
        </div>

        {(data.venue || data.attendance) && (
          <div className="text-center text-gray-500 text-xs mt-4">
            {data.venue && <span>{data.venue}</span>}
            {data.attendance && <span> &middot; {data.attendance.toLocaleString()}</span>}
          </div>
        )}
      </div>

      {/* Betting Lines Card */}
      {data.betting_lines && data.betting_lines.closing_spread != null && (
        <div className="rounded-xl p-4 border border-white/10">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
          <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-white/10">
            <div className="text-center py-3 md:px-3">
              <div className="text-[10px] text-gray-500 uppercase">Spread</div>
              <div className="text-sm mt-1">
                {data.betting_lines.closing_spread != null ? (
                  <>
                    {data.away.team} {(data.betting_lines.closing_spread * -1) >= 0 ? "+" : ""}{(data.betting_lines.closing_spread * -1).toFixed(1)}
                    <span className="text-gray-500 text-xs ml-1">({formatOdds(data.betting_lines.closing_spread_away_odds ?? -110)})</span>
                    <span className="text-gray-600 mx-2">|</span>
                    {data.home.team} {data.betting_lines.closing_spread >= 0 ? "+" : ""}{data.betting_lines.closing_spread.toFixed(1)}
                    <span className="text-gray-500 text-xs ml-1">({formatOdds(data.betting_lines.closing_spread_home_odds ?? -110)})</span>
                  </>
                ) : "-"}
              </div>
            </div>
            <div className="text-center py-3 md:px-3">
              <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
              <div className="text-sm mt-1">
                <span className="text-gray-400">{data.away.team}</span> {formatOdds(data.betting_lines.closing_away_ml)}
                <span className="text-gray-600 mx-2">|</span>
                <span className="text-earl-400">{data.home.team}</span> {formatOdds(data.betting_lines.closing_home_ml)}
              </div>
            </div>
            <div className="text-center py-3 md:px-3">
              <div className="text-[10px] text-gray-500 uppercase">Over / Under</div>
              <div className="text-sm mt-1">
                {data.betting_lines.closing_ou != null ? (
                  <>
                    O/U {data.betting_lines.closing_ou}
                    <span className="text-gray-500 text-xs ml-2 font-normal">Over {formatOdds(data.betting_lines.closing_over_odds ?? -110)}</span>
                    <span className="text-gray-600 mx-2">|</span>
                    <span className="text-gray-500 text-xs ml-2 font-normal">Under {formatOdds(data.betting_lines.closing_under_odds ?? -110)}</span>
                  </>
                ) : "-"}
              </div>
            </div>
          </div>

          {prediction && !prediction.detail && (
            <EarlsPicksPanel
              title="Earl's Picks"
              isFinal={!!(prediction.actual?.home_score != null || prediction.actual?.away_score != null)}
              predicted={
                prediction.predicted?.away_score != null && prediction.predicted?.home_score != null
                  ? {
                      awayLabel: data.away.team || "Away",
                      homeLabel: data.home.team || "Home",
                      awayScore: Math.round(prediction.predicted.away_score),
                      homeScore: Math.round(prediction.predicted.home_score),
                      total:
                        prediction.predicted.total ||
                        Math.round(prediction.predicted.away_score + prediction.predicted.home_score),
                      margin:
                        prediction.predicted.margin ||
                        Math.round((prediction.predicted.away_score - prediction.predicted.home_score) * 10) / 10,
                    }
                  : null
              }
              actual={
                prediction.actual?.home_score != null && prediction.actual?.away_score != null
                  ? {
                      awayLabel: data.away.team || "Away",
                      homeLabel: data.home.team || "Home",
                      awayScore: prediction.actual.away_score,
                      homeScore: prediction.actual.home_score,
                      total:
                        prediction.actual.total != null
                          ? prediction.actual.total
                          : prediction.actual.away_score + prediction.actual.home_score,
                      margin:
                        prediction.actual.margin != null
                          ? prediction.actual.margin
                          : prediction.actual.away_score - prediction.actual.home_score,
                    }
                  : null
              }
              items={[
                {
                  label: "Spread",
                  pick: prediction.predicted?.ats || "—",
                  ev: prediction.expected_value?.ats ?? null,
                  line: prediction.line?.spread != null ? `Spread ${prediction.line.spread >= 0 ? "+" : ""}${prediction.line.spread}` : null,
                  result: prediction.results?.ats || null,
                  pickColor: "text-earl-400",
                },
                {
                  label: "Over/Under",
                  pick: prediction.predicted?.ou ? `${prediction.predicted.ou} ${prediction.line?.over_under ?? ""}`.trim() : "—",
                  ev: prediction.expected_value?.ou ?? null,
                  line: prediction.line?.over_under != null ? `O/U ${prediction.line.over_under}` : null,
                  result: prediction.results?.ou || null,
                  pickColor: "text-yellow-400",
                },
                {
                  label: "Moneyline",
                  pick: prediction.predicted?.ml || "—",
                  ev: prediction.expected_value?.ml ?? null,
                  line: null,
                  result: prediction.results?.ml || null,
                  pickColor: "text-cyan-400",
                },
              ]}
            />
          )}
        </div>
      )}

      {/* Tabs */}
      {data && (
        <div className="bg-white/5 border border-white/10 rounded-xl overflow-hidden">
          {/* Tab bar */}
          <div className="flex border-b border-white/10 overflow-x-auto">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`px-4 py-2.5 text-sm font-medium whitespace-nowrap transition-colors border-b-2 ${
                  activeTab === tab.key
                    ? "border-earl-400 text-earl-400"
                    : "border-transparent text-gray-500 hover:text-gray-300 hover:border-gray-500"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab Content */}
          <div className="p-4">
            {activeTab === "boxscore" && renderBoxScore()}
            {activeTab === "summary" && renderGamePreview()}
            {activeTab === "analysis" && <PremiumGate>{renderAnalysis()}</PremiumGate>}
            {activeTab === "stats" && <PremiumGate><DetailedStatsTab gameId={gameId} /></PremiumGate>}
            {activeTab === "propBets" && <PropBetsTab sport="nba" gameId={gameId} />}
          </div>
        </div>
      )}
    </div>
  );
}
