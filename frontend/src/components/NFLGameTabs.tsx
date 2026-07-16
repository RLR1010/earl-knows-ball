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
  expected_value?: {
    ats?: number | null;
    ou?: number | null;
    ml?: number | null;
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


function NFLPickCard({
  pred,
  homeTeam,
  awayTeam,
  isFinal,
}: {
  pred: GamePrediction | null;
  homeTeam: string;
  awayTeam: string;
  isFinal?: boolean;
}) {
  const predicted = pred?.predicted || {} as { home_score?: number; away_score?: number; total?: number; margin?: number; ats?: string; ou?: string; ml?: string };
  const actual = (pred?.actual || {}) as { home_score?: number | null; away_score?: number | null; total?: number | null; margin?: number | null };
  const ev = pred?.expected_value;
  const lines = pred?.line;
  const results = pred?.results || {} as { ats?: string; ou?: string; ml?: string };
  const hasPrediction = predicted?.home_score != null && predicted?.away_score != null;
  const gameIsFinal = isFinal ?? (actual.home_score != null && actual.away_score != null);

  if (!pred || !hasPrediction) {
    return (
      <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-br from-earl-900/20 to-transparent">
        <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Earl's Picks</div>
        <div className="text-center py-8">
          <div className="text-gray-500 text-sm">No picks available for this game yet</div>
          <div className="text-gray-600 text-xs mt-2">Picks are generated closer to game time</div>
        </div>
      </div>
    );
  }

  const renderPickCard = ({
    type,
    label,
    pickText,
    evValue,
    result,
    lineText,
    borderColor,
    bgColor,
  }: {
    type: string;
    label: string;
    pickText: string | undefined | null;
    evValue: number | undefined | null;
    result: string | undefined | null;
    lineText: string | undefined | null;
    borderColor: string;
    bgColor: string;
  }) => {
    const showPick = pickText && pickText !== "N/A";
    const isWin = result === "W" || result === "Win";
    const isLoss = result === "L" || result === "Loss";
    const isPush = result === "P" || result === "Push";

    return (
      <div className={`rounded-lg p-3 border ${borderColor} ${bgColor}`}>
        <div className="text-[10px] text-gray-500 uppercase">{label}</div>
        {gameIsFinal && result && result !== "N/A" ? (
          isPush ? (
            <div className="text-sm font-bold mt-1 text-gray-400">Push</div>
          ) : (
            <>
              <div className={`text-lg font-bold mt-1 ${isWin ? "text-green-400" : "text-red-400"}`}>
                {isWin ? "Win" : "Loss"}
              </div>
              <div className="flex items-center gap-2 mt-1">
                {evValue != null && (
                  <span className={`text-[10px] font-semibold ${evValue >= 0 ? "text-green-400" : "text-red-400"}`}>
                    EV: {evValue >= 0 ? "+" : ""}{evValue.toFixed(1)}¢
                  </span>
                )}
                <span className="text-[10px] text-gray-400">Pick: {pickText}</span>
              </div>
            </>
          )
        ) : showPick ? (
          <>
            <div className="text-lg font-bold mt-1 text-white">{pickText}</div>
            {evValue != null && (
              <span className={`text-[10px] font-semibold mt-1 inline-block ${evValue >= 0 ? "text-green-400" : "text-red-400"}`}>
                EV: {evValue >= 0 ? "+" : ""}{evValue.toFixed(1)}¢
              </span>
            )}
            <div className="text-xs text-gray-500 mt-1">{lineText || "-"}</div>
          </>
        ) : (
          <div className="text-xs text-gray-400 mt-1">No {type} data</div>
        )}
      </div>
    );
  };

  const mapResult = (r?: string | null) => {
    if (!r || r === "N/A") return null;
    if (r === "W") return "Win";
    if (r === "L") return "Loss";
    if (r === "P") return "Push";
    return r;
  };

  return (
    <div className="border border-white/10 rounded-xl p-4 bg-gradient-to-br from-earl-900/20 to-transparent space-y-4">
      <div className="text-xs text-gray-500 uppercase tracking-wider">Earl's Picks</div>

      {/* Predicted score */}
      {predicted.home_score != null && (
        <div className="text-center">
          <div className="inline-block border border-white/10 rounded-lg px-6 py-2 bg-white/5">
            <span className="text-xs text-gray-500">Predicted</span>
            <div className="text-lg font-bold tracking-tight">
              <span className="text-gray-300">{awayTeam}</span>
              <span className="text-white mx-2">{predicted.away_score}</span>
              <span className="text-gray-600">@</span>
              <span className="text-white mx-2">{predicted.home_score}</span>
              <span className="text-gray-300">{homeTeam}</span>
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Total: {predicted.total && predicted.total !== 0 ? predicted.total : "?"} | Margin: {(predicted.home_score ?? 0) - (predicted.away_score ?? 0) >= 0 ? "+" : ""}{(predicted.home_score ?? 0) - (predicted.away_score ?? 0)}
            </div>
          </div>
        </div>
      )}

      {/* Actual score for completed games */}
      {gameIsFinal && (
        <div className="text-center">
          <div className="inline-block border border-green-500/20 rounded-lg px-6 py-2 bg-green-500/5">
            <span className="text-xs text-gray-500">Actual</span>
            <div className="text-lg font-bold tracking-tight">
              <span className="text-gray-300">{awayTeam}</span>
              <span className="text-white mx-2">{actual.away_score}</span>
              <span className="text-gray-600">@</span>
              <span className="text-white mx-2">{actual.home_score}</span>
              <span className="text-gray-300">{homeTeam}</span>
            </div>
            <div className="text-xs text-gray-500 mt-1">
              Total: {actual.total != null ? actual.total : "?"} | Margin: {actual.margin != null ? (actual.margin >= 0 ? "+" : "") + actual.margin : "?"}
            </div>
          </div>
        </div>
      )}

      {/* MLB-style 3-card grid */}
      <div className="grid grid-cols-3 gap-3">
        {renderPickCard({
          type: "ATS",
          label: "ATS",
          pickText: predicted?.ats,
          evValue: ev?.ats,
          result: mapResult(results?.ats),
          lineText: lines?.spread != null ? `Spread ${lines.spread >= 0 ? "+" : ""}${lines.spread}` : null,
          borderColor: "border-blue-500/40",
          bgColor: "bg-blue-500/10",
        })}
        {renderPickCard({
          type: "O/U",
          label: "Over/Under",
          pickText: predicted?.ou ? `${predicted.ou} ${lines?.over_under ?? ""}` : null,
          evValue: ev?.ou,
          result: mapResult(results?.ou),
          lineText: lines?.over_under != null ? `O/U ${lines.over_under}` : null,
          borderColor: "border-yellow-500/40",
          bgColor: "bg-yellow-500/10",
        })}
        {renderPickCard({
          type: "ML",
          label: "Moneyline",
          pickText: predicted?.ml,
          evValue: ev?.ml,
          result: mapResult(results?.ml),
          lineText: null,
          borderColor: "border-purple-500/40",
          bgColor: "bg-purple-500/10",
        })}
      </div>
    </div>
  );
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
          <tr className="bg-white/[0.03] text-gray-500 uppercase tracking-wider">
            <th className="text-right py-2 px-3 w-1/3 text-xs">{awayTeam}</th>
            <th className="text-center py-2 px-3 w-1/3 text-[10px]"></th>
            <th className="text-left py-2 px-3 w-1/3 text-xs">{homeTeam}</th>
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
  isFinal?: boolean;
}

export default function NFLGameTabs({ gameId, boxscore, prediction, isFinal }: NFLGameTabsProps) {
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
          <NFLPickCard pred={prediction} homeTeam={homeTeam} awayTeam={awayTeam} isFinal={isFinal} />
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
  const [featuresOpen, setFeaturesOpen] = useState(false);

  const game = boxscore.game;
  const homeTeam = game.home_team;
  const awayTeam = game.away_team;

  useEffect(() => {
    if (!gameId) return;
    setLoading(true);
    setError(false);
    fetch(`/api/handicapping/nfl/prediction-stats/${gameId}`)
      .then(res => {
        if (!res.ok) throw new Error('Failed to fetch');
        return res.json();
      })
      .then(data => {
        setStatsData(data);
        setLoading(false);
      })
      .catch(err => {
        console.error(err);
        setError(true);
        setLoading(false);
      });
  }, [gameId]);

  const featureDisplayNames: Record<string, string> = {
    spread: 'Spread',
    opening_spread: 'Opening Spread',
    opening_ou: 'Opening O/U',
    home_ats_streak: 'Home ATS Streak',
    away_ats_streak: 'Away ATS Streak',
    home_ml_streak: 'Home ML Streak',
    away_ml_streak: 'Away ML Streak',
    home_ou_streak: 'Home O/U Streak',
    away_ou_streak: 'Away O/U Streak',
    home_ats_pct_r10: 'Home ATS% (L10)',
    away_ats_pct_r10: 'Away ATS% (L10)',
    home_ml_pct_r10: 'Home Win% (L10)',
    away_ml_pct_r10: 'Away Win% (L10)',
    rest_days_home: 'Home Rest Days',
    rest_days_away: 'Away Rest Days',
    home_points_scored_pg: 'Home PPG',
    home_points_allowed_pg: 'Home OPPG',
    away_points_scored_pg: 'Away PPG',
    away_points_allowed_pg: 'Away OPPG',
    home_off_rush_ypg: 'Home Rush YPG',
    home_off_pass_ypg: 'Home Pass YPG',
    home_off_total_ypg: 'Home Total YPG',
    home_def_rush_ypg: 'Home Def Rush YPG',
    home_def_pass_ypg: 'Home Def Pass YPG',
    home_def_total_ypg: 'Home Def Total YPG',
    away_off_rush_ypg: 'Away Rush YPG',
    away_off_pass_ypg: 'Away Pass YPG',
    away_off_total_ypg: 'Away Total YPG',
    away_def_rush_ypg: 'Away Def Rush YPG',
    away_def_pass_ypg: 'Away Def Pass YPG',
    away_def_total_ypg: 'Away Def Total YPG',
    home_dvoa_total: 'Home Total DVOA',
    home_dvoa_offense: 'Home Off DVOA',
    home_dvoa_defense: 'Home Def DVOA',
    away_dvoa_total: 'Away Total DVOA',
    away_dvoa_offense: 'Away Off DVOA',
    away_dvoa_defense: 'Away Def DVOA',
    home_ats_pct: 'Home ATS%',
    away_ats_pct: 'Away ATS%',
    home_ml_pct: 'Home Win%',
    away_ml_pct: 'Away Win%',
    home_ou_over_pct: 'Home O/U Over%',
    away_ou_over_pct: 'Away O/U Over%',
    implied_home_ml: 'Implied Home ML',
    implied_away_ml: 'Implied Away ML',
    implied_total: 'Implied Total',
    implied_spread: 'Implied Spread',
    home_implied_win_pct: 'Home Implied Win%',
    away_implied_win_pct: 'Away Implied Win%',
    div_game: 'Division Game',
    dome: 'Dome Game',
    grass: 'Grass',
    home_rookie_qb: 'Home Rookie QB',
    away_rookie_qb: 'Away Rookie QB',
    short_week_home: 'Home Short Week',
    short_week_away: 'Away Short Week',
  };

  function getFeatureCategory(key: string): string {
    if (key.startsWith('home_') || key.startsWith('away_')) return 'team';
    if (key.includes('spread') || key.includes('ou') || key.includes('_ats_') || key.includes('_ml_') || key.startsWith('implied')) return 'betting';
    if (key.startsWith('opening')) return 'opening';
    return 'other';
  }

  function getDotColor(key: string): string {
    const cat = getFeatureCategory(key);
    switch (cat) {
      case 'team': return '#22c55e';
      case 'betting': return '#3b82f6';
      case 'opening': return '#f59e0b';
      default: return '#6b7280';
    }
  }

  function formatFeatureValue(value: any): string {
    if (value == null) return '-';
    if (typeof value === 'boolean') return value ? 'Yes' : 'No';
    if (typeof value === 'number') {
      if (Number.isInteger(value)) return value.toString();
      return value.toFixed(2);
    }
    return String(value);
  }

  if (loading) {
    return (
      <div className="text-center py-8">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-emerald-500 mx-auto"></div>
        <div className="text-gray-500 text-sm mt-2">Loading prediction data...</div>
      </div>
    );
  }

  if (error || !statsData) {
    return (
      <div className="text-center py-8">
        <div className="text-gray-500">Prediction stats not available yet.</div>
        <div className="text-gray-600 text-xs mt-1">Data loads closer to game time.</div>
        {boxscore?.home_stats && boxscore?.away_stats && (
          <div className="mt-4">
            <TeamStatsTable
              homeStats={boxscore.home_stats}
              awayStats={boxscore.away_stats}
              homeTeam={homeTeam}
              awayTeam={awayTeam}
            />
          </div>
        )}
      </div>
    );
  }

  const ps = statsData;
  const isFinal = game.home_score != null && game.away_score != null;
  const features = ps.features || ps.all_features || ps.model_features || {};
  const splits = ps.splits || ps.betting_trends || {};
  const situational = ps.situational || ps.situational_data || {};

  return (
    <div className="space-y-6">
      {/* Prediction Summary */}
      {ps.predicted && (ps.predicted.home_score != null || ps.predicted.away_score != null) && (
        <div>
          <h3 className="text-xs text-gray-500 uppercase tracking-wider mb-2">Prediction Summary</h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-xl p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">{awayTeam}</div>
              <div className="text-lg font-bold text-emerald-400">{ps.predicted.away_score != null ? Number(ps.predicted.away_score).toFixed(1) : '-'}</div>
            </div>
            <div className="bg-emerald-500/10 border border-emerald-500/30 rounded-xl p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">{homeTeam}</div>
              <div className="text-lg font-bold text-emerald-400">{ps.predicted.home_score != null ? Number(ps.predicted.home_score).toFixed(1) : '-'}</div>
            </div>
            <div className="bg-blue-500/10 border border-blue-500/30 rounded-xl p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">Total</div>
              <div className="text-lg font-bold text-blue-400">{ps.predicted.total != null ? Number(ps.predicted.total).toFixed(1) : '-'}</div>
            </div>
            <div className="bg-purple-500/10 border border-purple-500/30 rounded-xl p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">Margin</div>
              <div className="text-lg font-bold text-purple-400">{ps.predicted.margin != null ? (ps.predicted.margin > 0 ? '+' : '') + Number(ps.predicted.margin).toFixed(1) : '-'}</div>
            </div>
          </div>
        </div>
      )}

      {/* Home/Away Team Stats */}
      {boxscore?.home_stats && boxscore?.away_stats && (
        <div>
          <h3 className="text-xs text-gray-500 uppercase tracking-wider mb-2">Team Stats</h3>
          <TeamStatsTable
            homeStats={boxscore.home_stats}
            awayStats={boxscore.away_stats}
            homeTeam={homeTeam}
            awayTeam={awayTeam}
          />
        </div>
      )}

      {/* Splits / Betting Trends */}
      {Object.keys(splits).length > 0 && (
        <div>
          <h3 className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Splits & Trends</h3>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="bg-white/[0.03] text-gray-500 uppercase tracking-wider">
                  <th className="text-left py-2 px-3 w-2/5"></th>
                  <th className="text-center py-2 px-3 w-3/10">{awayTeam}</th>
                  <th className="text-center py-2 px-3 w-3/10">{homeTeam}</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(splits).map(([key, val], idx) => {
                  const displayName = featureDisplayNames[key] || key.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
                  if (typeof val !== 'object' || val === null) {
                    return (
                      <tr key={idx} className="border-t border-white/5">
                        <td className="py-2 px-3 text-gray-400">{displayName}</td>
                        <td className="py-2 px-3 text-center text-white" colSpan={2}>{formatFeatureValue(val)}</td>
                      </tr>
                    );
                  }
                  const obj = val as Record<string, any>;
                  return (
                    <tr key={idx} className="border-t border-white/5">
                      <td className="py-2 px-3 text-gray-400">{displayName}</td>
                      <td className="py-2 px-3 text-center text-white">{obj.away != null || obj.away_value != null ? formatFeatureValue(obj.away ?? obj.away_value) : '-'}</td>
                      <td className="py-2 px-3 text-center text-white">{obj.home != null || obj.home_value != null ? formatFeatureValue(obj.home ?? obj.home_value) : '-'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* All Model Features */}
      {Object.keys(features).length > 0 && (
        <div>
          <button
            onClick={() => setFeaturesOpen(!featuresOpen)}
            className="flex items-center gap-2 text-xs text-gray-400 hover:text-white transition-colors"
          >
            <svg className={`w-3 h-3 transition-transform ${featuresOpen ? 'rotate-90' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
            All Model Features ({Object.keys(features).length})
          </button>
          {featuresOpen && (
            <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2 max-h-96 overflow-y-auto">
              {Object.entries(features).map(([key, val], idx) => {
                const displayName = featureDisplayNames[key] || key.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
                return (
                  <div key={idx} className="flex items-center gap-2 bg-white/[0.03] rounded-lg px-3 py-2">
                    <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: getDotColor(key) }} />
                    <span className="text-gray-300 text-xs flex-1">{displayName}</span>
                    <span className="text-white text-xs font-medium">{formatFeatureValue(val)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Situational Data */}
      {Object.keys(situational).length > 0 && (
        <div>
          <h3 className="text-xs text-gray-500 uppercase tracking-wider mb-3">Situational</h3>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {Object.entries(situational).map(([key, val], idx) => {
              const displayName = key.split('_').map((w: string) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
              return (
                <div key={idx} className="bg-white/[0.03] rounded-lg px-3 py-2">
                  <div className="text-[10px] text-gray-500">{displayName}</div>
                  <div className="text-white text-sm font-medium mt-0.5">{formatFeatureValue(val)}</div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Actual Final Score */}
      {isFinal && (
        <div>
          <h3 className="text-xs text-gray-500 uppercase tracking-wider mb-2">Final Score</h3>
          <div className="grid grid-cols-4 gap-3">
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">{awayTeam}</div>
              <div className="text-lg font-bold text-white">{game.away_score}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">{homeTeam}</div>
              <div className="text-lg font-bold text-white">{game.home_score}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">Total</div>
              <div className="text-lg font-bold text-white">{Number(game.home_score!) + Number(game.away_score!)}</div>
            </div>
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 text-xs mb-1">Margin</div>
              <div className="text-lg font-bold text-white">{Number(game.home_score!) - Number(game.away_score!)}</div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}