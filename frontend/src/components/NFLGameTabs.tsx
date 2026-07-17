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
      <div className="text-center py-12">
        <div className="text-gray-500 text-sm">No picks available for this game yet</div>
        <div className="text-gray-600 text-xs mt-2">Picks are generated closer to game time</div>
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
                <span className="text-gray-400">Pick: {pickText}</span>
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
    <div className="space-y-4">

      {/* Predicted score */}
      {predicted.home_score != null && (
        <div className="text-center">
          <div className="text-sm text-gray-500 mb-1">Predicted</div>
          <div className="text-2xl font-bold tracking-tight">
            <span className="text-gray-300">{awayTeam}</span>
            <span className="text-white mx-2">{predicted.away_score}</span>
            <span className="text-gray-600">@</span>
            <span className="text-white mx-2">{predicted.home_score}</span>
            <span className="text-gray-300">{homeTeam}</span>
          </div>
          <div className="text-sm text-gray-500 mt-1">
            Total: {predicted.total && predicted.total !== 0 ? predicted.total : "?"} | Margin: {(predicted.home_score ?? 0) - (predicted.away_score ?? 0) >= 0 ? "+" : ""}{(predicted.home_score ?? 0) - (predicted.away_score ?? 0)}
          </div>
        </div>
      )}

      {/* MLB-style 3-card grid */}
      <div className="grid grid-cols-3 gap-3">
        {renderPickCard({
          type: "Spread",
          label: "Spread",
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
  spread_home_odds,
  spread_away_odds,
  over_under,
  over_odds,
  under_odds,
  homeML,
  awayML,
}: {
  homeTeam: string;
  awayTeam: string;
  spread: number | null | undefined;
  spread_home_odds?: number | null;
  spread_away_odds?: number | null;
  over_under: number | null | undefined;
  over_odds?: number | null;
  under_odds?: number | null;
  homeML?: number | null;
  awayML?: number | null;
}) {
  if (spread == null && over_under == null && homeML == null && awayML == null) return null;

  return (
    <div className="border border-white/10 rounded-xl p-4 bg-white/5">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        {/* Spread */}
        <div className="text-center p-3 rounded-lg bg-white/[0.03]">
          <div className="text-[10px] text-gray-500 uppercase">Spread</div>
          <div className="text-sm mt-1">
            <span className="text-earl-400">{awayTeam}</span> {spread != null ? (spread < 0 ? `+${Math.abs(spread)}` : `-${spread}`) : "-"}
            <span className="text-gray-500 text-xs ml-1">({formatOdds(spread_away_odds ?? -110)})</span>
            <span className="text-gray-600 mx-2">|</span>
            <span className="text-gray-400">{homeTeam}</span> {spread != null ? (spread < 0 ? `${spread}` : `+${spread}`) : "-"}
            <span className="text-gray-500 text-xs ml-1">({formatOdds(spread_home_odds ?? -110)})</span>
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
          <div className="text-[10px] text-gray-500 uppercase">Over / Under</div>
          <div className="text-sm mt-1">
            {over_under != null ? (
              <>
                O/U {over_under}
                <span className="text-gray-500 text-xs ml-2 font-normal">Over {formatOdds(over_odds ?? -110)}</span>
                <span className="text-gray-500 text-xs ml-1 font-normal">| Under {formatOdds(under_odds ?? -110)}</span>
              </>
            ) : "-"}
          </div>
        </div>
      </div>
    </div>
  );
}

function formatOdds(odds: number): string {
  if (odds > 0) return `+${odds}`;
  return `${odds}`;
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
      {/* Tabs Container */}
      <div className="border border-white/10 rounded-xl bg-gradient-to-br from-blue-900/20 to-transparent mt-4">
      {/* Tab Buttons */}
      <div className="flex border-b border-white/10">
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
      <div className="p-4 min-h-[200px]">
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
    </div>
  );
}

// ── Detailed Stats Tab ─────────────────────────────────────────

function DetailedStatsTab({ gameId, boxscore }: { gameId: string; boxscore: NFLBoxScoreData }) {
  const [statsData, setStatsData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const game = boxscore.game;
  const homeTeam = game.home_team;
  const awayTeam = game.away_team;

  useEffect(() => {
    if (!gameId) return;
    setLoading(true);
    setError(false);
    fetch(`/api/handicapping/nfl/prediction-stats/${gameId}`)
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

  // ── Parse data sources ──────────────────────────────────────────────────
  const features = statsData.features || {};
  const homeStats = statsData.home_stats || {};
  const awayStats = statsData.away_stats || {};
  const situational = statsData.situational || {};
  const splits = statsData.splits || {};
  const predicted = statsData.predicted || {};
  const actual = statsData.actual || {};

  // ── Rich value extractor ─────────────────────────────────────────────────
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

  // ── StatRow: label + value + CSS-only tooltip ───────────────────────────
  const StatRow = ({
    label,
    value,
    description,
    valueClass,
  }: {
    label: string;
    value: string;
    description?: string;
    valueClass?: string;
  }) => (
    <div className="group relative flex items-center justify-between px-2 py-1 rounded hover:bg-white/[0.03] transition-colors">
      <span
        className={`text-gray-400 truncate text-[11px] ${
          description ? "cursor-help border-b border-dotted border-gray-600/40 hover:border-gray-400" : ""
        }`}
      >
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
            <div className="w-0 h-0 border-l-[5px] border-r-[5px] border-t-[5px] border-transparent border-t-gray-700"></div>
          </div>
        </div>
      )}
    </div>
  );

  // ── SectionHeader ───────────────────────────────────────────────────────
  const SectionHeader = ({ title }: { title: string }) => (
    <div className="flex items-center gap-2 mb-3">
      <span className="text-gray-500 text-[10px] uppercase tracking-[0.12em] font-semibold">{title}</span>
      <div className="flex-1 h-px bg-gradient-to-r from-gray-700/60 to-transparent" />
    </div>
  );

  // ── Render stat section (handles plain + rich dict values) ──────────────
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

  // ── Features section ────────────────────────────────────────────────────
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

  // ── Splits section ──────────────────────────────────────────────────────
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

  // ── PredCard for predictions summary ────────────────────────────────────
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

  // ── Build section entry counts ──────────────────────────────────────────
  const homeEntries = Object.entries(homeStats);
  const awayEntries = Object.entries(awayStats);
  const sitEntries = Object.entries(situational);

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="space-y-6 text-xs">
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
