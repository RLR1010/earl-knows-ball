"use client";
import { useEffect, useState } from "react";
import Link from "next/link";

// ── Types ─────────────────────────────────────────────────────

interface NFLPlayerStat {
  player_id: number;
  player_name: string;
  position: string | null;
  pass_attempts: number | null;
  pass_completions: number | null;
  pass_yards: number | null;
  pass_tds: number | null;
  pass_int: number | null;
  rush_attempts: number | null;
  rush_yards: number | null;
  rush_tds: number | null;
  targets: number | null;
  receptions: number | null;
  receiving_yards: number | null;
  receiving_tds: number | null;
}

interface NFLTeamStats {
  total_yards: number | null;
  pass_yards: number | null;
  rush_yards: number | null;
  turnovers: number | null;
  first_downs: number | null;
  third_down_pct: number | null;
  fourth_down_pct?: number | null;
  time_of_possession: string | null;
  penalties: number | null;
  penalty_yards: number | null;
  top_players: NFLPlayerStat[];
}

interface NFLBoxScoreData {
  game: {
    id: number;
    week: number;
    status: string;
    home_team: string;
    away_team: string;
    home_score: number | null;
    away_score: number | null;
    spread?: number | null;
    over_under?: number | null;
  };
  home_stats: NFLTeamStats | null;
  away_stats: NFLTeamStats | null;
}

interface NFLWriteupData {
  has_writeup: boolean;
  public_content: string | null;
  premium_content: string | null;
  title: string | null;
  status: string | null;
}

interface GamePrediction {
  game_id: number;
  home_team: string;
  away_team: string;
  predicted: {
    home_score?: number;
    away_score?: number;
    total?: number;
    ats?: string;
    ou?: string;
    ml?: string;
  };
  actual: {
    home_score?: number;
    away_score?: number;
    total?: number;
  };
  results: {
    ats?: string;
    ou?: string;
    ml?: string;
  };
  confidence?: {
    overall?: number | null;
    ats?: number | null;
    ou?: number | null;
    ml?: number | null;
  };
  line?: {
    spread: number | null;
    over_under: number | null;
  };
}

// ── Helpers ────────────────────────────────────────────────────

function formatSpreadLine(spread: number | null | undefined, homeTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${homeTeam} +${spread}`;
  if (spread < 0) return `${homeTeam} ${spread}`;
  return `${homeTeam} PK`;
}

function formatLineAway(spread: number | null | undefined, awayTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${awayTeam} -${spread}`;
  if (spread < 0) return `${awayTeam} +${Math.abs(spread)}`;
  return `${awayTeam} PK`;
}

function ConfidenceBar({ score }: { score?: number | null }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color = score >= 0.7 ? "bg-green-500" : score >= 0.55 ? "bg-yellow-500" : "bg-gray-500";
  return (
    <div className="w-full h-1.5 bg-white/10 rounded-full mt-1 overflow-hidden">
      <div className={`h-full rounded-full ${color} transition-all duration-300`} style={{ width: `${pct}%` }} />
    </div>
  );
}

// ── Team Stats Table ──────────────────────────────────────────

function TeamStatsTable({ homeStats, awayStats, homeTeam, awayTeam }: {
  homeStats: NFLTeamStats;
  awayStats: NFLTeamStats;
  homeTeam: string;
  awayTeam: string;
}) {
  const stats: { label: string; home: number | string | null | undefined; away: number | string | null | undefined; better?: "high" | "low" }[] = [
    { label: "Total Yards", home: homeStats.total_yards, away: awayStats.total_yards, better: "high" },
    { label: "Pass Yards", home: homeStats.pass_yards, away: awayStats.pass_yards, better: "high" },
    { label: "Rush Yards", home: homeStats.rush_yards, away: awayStats.rush_yards, better: "high" },
    { label: "Turnovers", home: homeStats.turnovers, away: awayStats.turnovers, better: "low" },
    { label: "First Downs", home: homeStats.first_downs, away: awayStats.first_downs, better: "high" },
    { label: "3rd Down %", home: homeStats.third_down_pct, away: awayStats.third_down_pct, better: "high" },
    { label: "4th Down %", home: homeStats.fourth_down_pct, away: awayStats.fourth_down_pct, better: "high" },
    { label: "Time of Poss", home: homeStats.time_of_possession, away: awayStats.time_of_possession },
    { label: "Penalties", home: homeStats.penalties, away: awayStats.penalties, better: "low" },
    { label: "Penalty Yards", home: homeStats.penalty_yards, away: awayStats.penalty_yards, better: "low" },
  ];

  const isBetter = (v: number | string | null | undefined, otherV: number | string | null | undefined, better?: "high" | "low") => {
    if (v == null || otherV == null || v === "-" || otherV === "-" || !better) return false;
    const n = typeof v === "string" ? parseFloat(v) : v;
    const on = typeof otherV === "string" ? parseFloat(otherV) : otherV;
    if (isNaN(n) || isNaN(on)) return false;
    if (better === "high") return n > on;
    return n < on;
  };

  const fmtVal = (v: number | string | null | undefined) => v != null ? v : "-";

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
            <th className="text-right py-2 px-3 w-1/3">{awayTeam}</th>
            <th className="text-center py-2 px-3 w-1/3"></th>
            <th className="py-2 px-3 w-1/3">{homeTeam}</th>
          </tr>
        </thead>
        <tbody>
          {stats.map(s => (
            <tr key={s.label} className="border-b border-white/5">
              <td className={`py-2 px-3 text-sm text-right ${isBetter(s.away, s.home, s.better) ? "text-green-400 font-bold" : ""}`}>{fmtVal(s.away)}</td>
              <td className="py-2 px-3 text-xs text-gray-500 uppercase tracking-wider text-center font-medium">{s.label}</td>
              <td className={`py-2 px-3 text-sm ${isBetter(s.home, s.away, s.better) ? "text-green-400 font-bold" : ""}`}>{fmtVal(s.home)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Player Stat Components ────────────────────────────────────

function PlayerStatRow({ p }: { p: NFLPlayerStat }) {
  const cols: { label: string; value: number | null; key: string }[] = [
    // Passing
    ...(p.pass_completions != null ? [{ label: "Cmp", value: p.pass_completions, key: "p-cmp" }] : []),
    ...(p.pass_attempts != null ? [{ label: "Att", value: p.pass_attempts, key: "p-att" }] : []),
    ...(p.pass_yards != null ? [{ label: "Yds", value: p.pass_yards, key: "p-yds" }] : []),
    ...(p.pass_tds != null ? [{ label: "TD", value: p.pass_tds, key: "p-td" }] : []),
    ...(p.pass_int != null ? [{ label: "Int", value: p.pass_int, key: "p-int" }] : []),
    // Rushing
    ...(p.rush_attempts != null ? [{ label: "Att", value: p.rush_attempts, key: "r-att" }] : []),
    ...(p.rush_yards != null ? [{ label: "Yds", value: p.rush_yards, key: "r-yds" }] : []),
    ...(p.rush_tds != null ? [{ label: "TD", value: p.rush_tds, key: "r-td" }] : []),
    // Receiving
    ...(p.receptions != null ? [{ label: "Rec", value: p.receptions, key: "rec-rec" }] : []),
    ...(p.receiving_yards != null ? [{ label: "Yds", value: p.receiving_yards, key: "rec-yds" }] : []),
    ...(p.receiving_tds != null ? [{ label: "TD", value: p.receiving_tds, key: "rec-td" }] : []),
  ];
  return (
    <tr className="border-b border-white/5 hover:bg-white/[0.02]">
      <td className="py-2 px-3 text-sm font-medium whitespace-nowrap">
        {p.player_name}
        {p.position ? <span className="text-gray-500 text-[10px] ml-1">({p.position})</span> : null}
      </td>
      {cols.map(c => <td key={c.key} className="py-2 px-3 text-sm text-right">{c.value}</td>)}
    </tr>
  );
}

function PlayerStatsSection({ title, players }: { title: string; players: NFLPlayerStat[] }) {
  const displayCols: { label: string; key: string }[] = players.length > 0
    ? [
        // Passing headers
        ...(players[0].pass_completions != null ? [{ label: "Cmp", key: "p-cmp" }] : []),
        ...(players[0].pass_attempts != null ? [{ label: "Att", key: "p-att" }] : []),
        ...(players[0].pass_yards != null ? [{ label: "Yds", key: "p-yds" }] : []),
        ...(players[0].pass_tds != null ? [{ label: "TD", key: "p-td" }] : []),
        ...(players[0].pass_int != null ? [{ label: "Int", key: "p-int" }] : []),
        // Rushing headers
        ...(players[0].rush_attempts != null ? [{ label: "Att", key: "r-att" }] : []),
        ...(players[0].rush_yards != null ? [{ label: "Yds", key: "r-yds" }] : []),
        ...(players[0].rush_tds != null ? [{ label: "TD", key: "r-td" }] : []),
        // Receiving headers
        ...(players[0].receptions != null ? [{ label: "Rec", key: "rec-rec" }] : []),
        ...(players[0].receiving_yards != null ? [{ label: "Yds", key: "rec-yds" }] : []),
        ...(players[0].receiving_tds != null ? [{ label: "TD", key: "rec-td" }] : []),
      ]
    : [];
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wider text-gray-400 mb-2">{title}</h4>
      <div className="overflow-x-auto">
        <table className="w-full text-left">
          <thead>
            <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
              <th className="py-2 px-3 text-left">Player</th>
              {displayCols.map(c => <th key={c.key} className="py-2 px-3 text-right">{c.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {players.map((p, i) => <PlayerStatRow key={`${p.player_id}-${i}`} p={p} />)}
          </tbody>
        </table>
      </div>
    </div>
  );
}
function NFLBoxScoreContent({ homeTeam, awayTeam, homeStats, awayStats }: {
  homeTeam: string;
  awayTeam: string;
  homeStats: NFLTeamStats;
  awayStats: NFLTeamStats;
}) {
  return (
    <div className="space-y-6">
      {/* Team Stats */}
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold">Team Stats</div>
        <TeamStatsTable homeStats={homeStats} awayStats={awayStats} homeTeam={homeTeam} awayTeam={awayTeam} />
      </div>

      {/* Key Players */}
      {awayStats.top_players.length > 0 && (
        <div className="border border-white/10 rounded-xl overflow-hidden">
          <PlayerStatsSection title={`${awayTeam} — Key Players`} players={awayStats.top_players} />
        </div>
      )}
      {homeStats.top_players.length > 0 && (
        <div className="border border-white/10 rounded-xl overflow-hidden">
          <PlayerStatsSection title={`${homeTeam} — Key Players`} players={homeStats.top_players} />
        </div>
      )}

      {/* Empty state */}
      {awayStats.top_players.length === 0 && homeStats.top_players.length === 0 &&
        !homeStats.total_yards && !awayStats.total_yards && (
        <div className="text-center py-12 border border-white/10 rounded-xl">
          <div className="text-gray-500 text-sm">Game has not started yet — box score will appear once it begins.</div>
        </div>
      )}
    </div>
  );
}

function NFLPickCard({
  pred,
  homeTeam,
  awayTeam,
}: {
  pred: GamePrediction | null;
  homeTeam: string;
  awayTeam: string;
}) {
  const predicted = pred?.predicted || {};
  const actual = pred?.actual || {};
  const results = pred?.results || {};
  const line = pred?.line;
  const conf = pred?.confidence;
  const noPrediction = !results.ats || results.ats === "N/A";

  return (
    <div className="border border-white/10 rounded-xl p-4 bg-gradient-to-br from-earl-900/20 to-transparent">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Earl's Prediction</div>

      {line?.spread != null && (
        <div className="text-center mb-4">
          <span className="inline-block px-5 py-2 rounded-lg bg-gradient-to-r from-earl-800/40 via-earl-600/50 to-earl-800/40 border border-earl-500/50 text-base font-bold tracking-wide">
            <span className="text-earl-200">{formatLineAway(line.spread, awayTeam)}</span>
            <span className="mx-3 text-gray-500">|</span>
            <span className="text-earl-300">{formatSpreadLine(line.spread, homeTeam)}</span>
            {line.over_under != null && (
              <>
                <span className="mx-3 text-gray-500">|</span>
                <span className="text-white">O/U {line.over_under}</span>
              </>
            )}
          </span>
        </div>
      )}

      {conf?.overall != null && (
        <div className="text-center mb-4">
          <span className={`inline-block px-4 py-1 rounded-lg text-sm font-bold tracking-wide ${
            conf.overall >= 0.7 ? "bg-green-900/30 text-green-400 border border-green-500/40" :
            conf.overall >= 0.55 ? "bg-yellow-900/30 text-yellow-400 border border-yellow-500/40" :
            "bg-gray-800/30 text-gray-400 border border-gray-500/40"
          }`}>
            Overall Confidence: {Math.round(conf.overall * 100)}%
          </span>
        </div>
      )}

      {noPrediction ? (
        <div className="text-sm text-gray-500 text-center">No prediction available for this game.</div>
      ) : (
        <div className="grid grid-cols-3 gap-4 mt-4">
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">ATS</div>
            <div className={`text-lg font-bold mt-1 ${results.ats === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ats || "-"}</div>
            <ConfidenceBar score={conf?.ats ?? conf?.overall} />
            {predicted.home_score != null && predicted.away_score != null && (
              <div className="text-xs text-gray-400 mt-1">Pred: {predicted.away_score}-{predicted.home_score}</div>
            )}
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">O/U</div>
            <div className={`text-lg font-bold mt-1 ${results.ou === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ou || "-"}</div>
            <ConfidenceBar score={conf?.ou ?? conf?.overall} />
            {predicted.total != null && <div className="text-xs text-gray-400 mt-1">Pred: {predicted.total}</div>}
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">ML</div>
            <div className={`text-lg font-bold mt-1 ${results.ml === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ml || "-"}</div>
            <ConfidenceBar score={conf?.ml ?? conf?.overall} />
            <div className="text-xs text-gray-400 mt-1">{awayTeam} @ {homeTeam}</div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Betting Lines Card ─────────────────────────────────────────

export function BettingLinesCard({
  homeTeam,
  awayTeam,
  spread,
  over_under,
  homeML,
  awayML,
}: {
  homeTeam: string;
  awayTeam: string;
  spread: number | null | undefined;
  over_under: number | null | undefined;
  homeML?: number | null;
  awayML?: number | null;
}) {
  if (spread == null && over_under == null && homeML == null && awayML == null) return null;

  const spreadStr = spread != null
    ? (spread >= 0
        ? `${homeTeam} +${spread} | ${awayTeam} -${spread}`
        : `${homeTeam} ${spread} | ${awayTeam} +${Math.abs(spread)}`)
    : "-";

  return (
    <div className="border border-white/10 rounded-xl p-4 bg-white/5">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Spread */}
        <div className="text-center p-3 rounded-lg bg-white/[0.03]">
          <div className="text-[10px] text-gray-500 uppercase">Spread</div>
          <div className="text-sm mt-1">
            <span className="text-earl-400">{awayTeam}</span> {spread != null ? (spread < 0 ? `+${Math.abs(spread)}` : `-${spread}`) : "-"}
            <span className="text-gray-600 mx-2">|</span>
            <span className="text-gray-400">{homeTeam}</span> {spread != null ? (spread < 0 ? `${spread}` : `+${spread}`) : "-"}
          </div>
        </div>

        {/* Moneyline */}
        <div className="text-center p-3 rounded-lg bg-white/[0.03]">
          <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
          <div className="text-sm mt-1">
            <span className="text-earl-400">{awayTeam}</span> {awayML != null ? (awayML > 0 ? `+${awayML}` : `${awayML}`) : "N/A"}<span className="text-gray-600 mx-2">|</span><span className="text-gray-400">{homeTeam}</span> {homeML != null ? (homeML > 0 ? `+${homeML}` : `${homeML}`) : "N/A"}
          </div>
        </div>

        {/* Over/Under */}
        <div className="text-center p-3 rounded-lg bg-white/[0.03]">
          <div className="text-[10px] text-gray-500 uppercase">Total</div>
          <div className="text-sm mt-1">
            {over_under != null ? `O/U ${over_under}` : "-"}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Main NFLGameTabs Component ─────────────────────────────────

interface NFLGameTabsProps {
  gameId: string;
  boxscore: NFLBoxScoreData;
  prediction: GamePrediction | null;
}

export default function NFLGameTabs({ gameId, boxscore, prediction }: NFLGameTabsProps) {
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [writeupData, setWriteupData] = useState<NFLWriteupData | null>(null);
  const [writeupLoading, setWriteupLoading] = useState(false);

  const home_stats = boxscore.home_stats || { total_yards: null, pass_yards: null, rush_yards: null, turnovers: null, first_downs: null, third_down_pct: null, fourth_down_pct: null, time_of_possession: null, penalties: null, penalty_yards: null, top_players: [] };
  const away_stats = boxscore.away_stats || { total_yards: null, pass_yards: null, rush_yards: null, turnovers: null, first_downs: null, third_down_pct: null, fourth_down_pct: null, time_of_possession: null, penalties: null, penalty_yards: null, top_players: [] };
  const homeTeam = boxscore.game?.home_team || "";
  const awayTeam = boxscore.game?.away_team || "";
  const gameStatus = boxscore.game?.status?.toLowerCase() || "";

  // Determine default tab
  const isGameInProgress = gameStatus === "final" || gameStatus === "in progress" || gameStatus === "in_progress" ||
    gameStatus === "2nd quarter" || gameStatus === "3rd quarter" || gameStatus === "4th quarter" || gameStatus === "halftime";
  const defaultTab = isGameInProgress ? "boxscore" : "preview";

  useEffect(() => {
    setActiveTab(defaultTab);
  }, [defaultTab]);

  // Fetch writeup data when Game Preview or Detailed Analysis tab is selected
  useEffect(() => {
    if (activeTab !== "preview" && activeTab !== "analysis") return;
    if (writeupData || writeupLoading) return;
    setWriteupLoading(true);
    fetch(`/api/writeups/nfl/game/${gameId}`)
      .then(r => r.ok ? r.json() : null)
      .then(d => setWriteupData(d as NFLWriteupData))
      .catch(() => {})
      .finally(() => setWriteupLoading(false));
  }, [activeTab, gameId, writeupData, writeupLoading]);

  const tabs = [
    ...(isGameInProgress ? [{ key: "boxscore", label: "Box Score" }] : []),
    { key: "preview", label: "Game Preview" },
    ...(prediction ? [{ key: "picks", label: "Earl's Picks" }] : []),
    { key: "analysis", label: "Detailed Analysis" },
    { key: "stats", label: "Detailed Stats" },
  ];

  if (tabs.length === 0) return null;

  return (
    <div className="mt-6">
      {/* Tab Buttons */}
      <div className="flex border-b border-white/10 mb-6">
        {tabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-xs font-semibold uppercase tracking-wider whitespace-nowrap transition-all cursor-pointer ${
              activeTab === tab.key
                ? "text-earl-300 border-b-2 border-earl-500"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div className="min-h-[200px]">
        {/* Box Score */}
        {activeTab === "boxscore" && (
          <NFLBoxScoreContent
            homeTeam={homeTeam}
            awayTeam={awayTeam}
            homeStats={home_stats}
            awayStats={away_stats}
          />
        )}

        {/* Game Preview */}
        {activeTab === "preview" && (
          <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-br from-earl-900/20 to-transparent">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Game Preview</div>
            {writeupLoading ? (
              <div className="text-sm text-gray-500 animate-pulse">Loading preview...</div>
            ) : writeupData?.has_writeup && writeupData.public_content ? (
              <div className="prose prose-sm prose-invert max-w-none whitespace-pre-wrap">
                {writeupData.public_content}
              </div>
            ) : (
              <div className="text-sm text-gray-500 text-center py-8">
                No game preview available yet.
              </div>
            )}
          </div>
        )}

        {/* Earl's Picks */}
        {activeTab === "picks" && (
          <NFLPickCard pred={prediction} homeTeam={homeTeam} awayTeam={awayTeam} />
        )}

        {/* Detailed Analysis */}
        {activeTab === "analysis" && (
          <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-br from-earl-900/20 to-transparent">
            <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Detailed Analysis</div>
            {writeupLoading ? (
              <div className="text-sm text-gray-500 animate-pulse">Loading analysis...</div>
            ) : writeupData?.has_writeup && writeupData.premium_content ? (
              <>
                {writeupData.title && (
                  <h3 className="text-lg font-bold text-white mb-3">{writeupData.title}</h3>
                )}
                <div className="prose prose-sm prose-invert max-w-none whitespace-pre-wrap">
                  {writeupData.premium_content}
                </div>
              </>
            ) : (
              <div className="text-sm text-gray-500 text-center py-8">
                No detailed analysis available yet.
              </div>
            )}
          </div>
        )}

        {/* Detailed Stats */}
        {activeTab === "stats" && (
          <DetailedStatsTab gameId={gameId} boxscore={boxscore} />
        )}
      </div>
    </div>
  );
}

// ── Detailed Stats Tab ─────────────────────────────────────────

function DetailedStatsTab({ gameId, boxscore }: { gameId: string; boxscore: NFLBoxScoreData }) {
  const [statsData, setStatsData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch(`/api/games/${gameId}/features`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d) setStatsData(d);
        else setError(true);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, [gameId]);

  const homeTeam = boxscore.game?.home_team || "";
  const awayTeam = boxscore.game?.away_team || "";
  const homeStats = boxscore.home_stats || { total_yards: null, pass_yards: null, rush_yards: null, turnovers: null, first_downs: null, third_down_pct: null, fourth_down_pct: null, time_of_possession: null, penalties: null, penalty_yards: null, top_players: [] };
  const awayStats = boxscore.away_stats || { total_yards: null, pass_yards: null, rush_yards: null, turnovers: null, first_downs: null, third_down_pct: null, fourth_down_pct: null, time_of_possession: null, penalties: null, penalty_yards: null, top_players: [] };

  if (loading) {
    return (
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold">Team Stats</div>
        <TeamStatsTable homeStats={homeStats} awayStats={awayStats} homeTeam={homeTeam} awayTeam={awayTeam} />
      </div>
    );
  }

  if (error || !statsData) {
    return (
      <div className="space-y-6">
        <div className="border border-white/10 rounded-xl overflow-hidden">
          <div className="bg-white/5 px-4 py-2 text-sm font-semibold">Team Stats</div>
          <TeamStatsTable homeStats={homeStats} awayStats={awayStats} homeTeam={homeTeam} awayTeam={awayTeam} />
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Team Stats */}
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold">Team Stats</div>
        <TeamStatsTable homeStats={homeStats} awayStats={awayStats} homeTeam={homeTeam} awayTeam={awayTeam} />
      </div>

      {/* Situational Data */}
      {statsData.situational && (
        <div className="border border-white/10 rounded-xl p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Situational</div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            {Object.entries(statsData.situational).map(([key, val]) => (
              <div key={key} className="bg-white/[0.03] rounded-lg p-3">
                <div className="text-[10px] text-gray-500 uppercase">{key.replace(/_/g, " ")}</div>
                <div className="text-white font-semibold mt-1">{String(val)}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Team Splits */}
      {statsData.splits && (
        <div className="border border-white/10 rounded-xl p-4">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Team Splits</div>
          {typeof statsData.splits === "object" && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {Object.entries(statsData.splits).map(([team, splits]) => {
                if (typeof splits !== "object") return null;
                return (
                  <div key={team} className="bg-white/[0.03] rounded-lg p-3">
                    <div className="text-xs font-semibold text-white mb-2">{team}</div>
                    {Object.entries(splits as Record<string, any>).map(([k, v]) => (
                      <div key={k} className="flex justify-between text-xs py-1 border-b border-white/5 last:border-0">
                        <span className="text-gray-500 capitalize">{k.replace(/_/g, " ")}</span>
                        <span className="text-white">{String(v)}</span>
                      </div>
                    ))}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Features JSON */}
      {statsData.features && (
        <div className="border border-white/10 rounded-xl overflow-hidden">
          <div className="bg-white/5 px-4 py-2 text-sm font-semibold">Features</div>
          <div className="p-4">
            {typeof statsData.features === "object" ? (
              <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                {Object.entries(statsData.features).map(([key, val]) => (
                  <div key={key} className="bg-white/[0.03] rounded p-2 text-xs">
                    <span className="text-gray-500">{key.replace(/_/g, " ")}</span>
                    <span className="text-white ml-2 font-mono">{String(val)}</span>
                  </div>
                ))}
              </div>
            ) : (
              <pre className="text-xs text-gray-400 whitespace-pre-wrap font-mono">
                {JSON.stringify(statsData.features, null, 2)}
              </pre>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
