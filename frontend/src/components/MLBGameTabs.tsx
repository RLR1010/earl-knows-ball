"use client";

import { useState, useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PremiumGate from "./PremiumGate";
import ShapBreakdown from "./ShapBreakdown";
import PropBetsTab from "./PropBetsTab";
import { useAuth } from "../lib/auth-context";

interface MLBGameTabsProps {
  gameId: number;
  game: any;
  formatOdds: (v: any) => string;
  boxscore?: any;
  linescore?: any;
}

export default function MLBGameTabs({ gameId, game, formatOdds, boxscore, linescore }: MLBGameTabsProps) {
  const { user } = useAuth();
  const [activeTab, setActiveTab] = useState<string>("boxscore");
  const [writeup, setWriteup] = useState<any>(null);
  const [predictionStats, setPredictionStats] = useState<any>(null);
  const [propBets, setPropBets] = useState<any[] | null>(null);
  const [loadingWriteup, setLoadingWriteup] = useState(false);
  const [loadingStats, setLoadingStats] = useState(false);
  const [loadingProps, setLoadingProps] = useState(false);

  // Sub-tab inside Detailed Analysis: "analysis" vs "stats"
  const [analysisSubTab, setAnalysisSubTab] = useState<"analysis" | "stats">("analysis");
  const writeupLoadedOnce = useRef(false);

  const hasBoxscore = !!(boxscore?.teams?.away?.teamStats || linescore?.teams?.away?.runs != null);
  const hasProps = !!propBets && propBets.length > 0;

  // Track whether we've already attempted each fetch (prevents infinite loops on error)
  const writeupAttempted = useRef(false);
  const statsAttempted = useRef(false);

  // Default: boxscore tab if available, otherwise Game Preview tab
  useEffect(() => {
    if (!hasBoxscore) {
      setActiveTab("summary");
    }
  }, [hasBoxscore]);

  // Fetch writeup data when needed — fire once per gameId change
  useEffect(() => {
    if (activeTab === "summary" || activeTab === "analysis") {
      if (!writeup && !loadingWriteup && !writeupAttempted.current) {
        writeupAttempted.current = true;
        setLoadingWriteup(true);
        fetch(`/api/writeups/mlb/by-game/${gameId}`)
          .then(r => r.json())
          .then(data => {
            setWriteup(data);
            setLoadingWriteup(false);
          })
          .catch(() => {
            setWriteup(null); // ensure it stays null so we don't show stale data
            setLoadingWriteup(false);
          });
      }
    }
  }, [activeTab, gameId]);

  // Default sub-tab for Detailed Analysis: Analysis unless there is no premium_content, then Stats.
  // Only applied after the writeup fetch has completed (avoids a flash to Stats before content loads).
  useEffect(() => {
    if (loadingWriteup) writeupLoadedOnce.current = true;
  }, [loadingWriteup]);
  useEffect(() => {
    if (writeupLoadedOnce.current && !loadingWriteup) {
      setAnalysisSubTab(writeup?.premium_content ? "analysis" : "stats");
    }
  }, [loadingWriteup, writeup?.premium_content]);

  // Fetch prediction stats when needed — fire once per gameId change
  useEffect(() => {
    if (activeTab === "analysis" && analysisSubTab === "stats") {
      if (!predictionStats && !loadingStats && !statsAttempted.current) {
        statsAttempted.current = true;
        setLoadingStats(true);
        fetch(`/api/mlb/games/${gameId}/prediction-stats`)
          .then(r => r.json())
          .then(data => {
            setPredictionStats(data);
            setLoadingStats(false);
          })
          .catch(() => {
            setPredictionStats(null);
            setLoadingStats(false);
          });
      }
    }
  }, [activeTab, analysisSubTab, gameId]);

  // Preload prop bets for this game (determines whether the Prop Bets tab shows)
  useEffect(() => {
    if (propBets !== null) return;
    setLoadingProps(true);
    fetch(`/api/mlb/games/${gameId}/prop-bets`)
      .then(r => r.json())
      .then(data => {
        setPropBets(Array.isArray(data) ? data : []);
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
    { key: "propBets", label: "Prop Bets", enabled: hasProps },
  ];

  // Only show enabled tabs; default to Game Preview if boxscore tab hidden
  const visibleTabs = tabs.filter(t => (t.key === "boxscore" ? hasBoxscore : t.enabled));

  return (
    <div className="border border-white/10 rounded-xl bg-gradient-to-br from-blue-900/20 to-transparent mt-4">
      {/* Tabs */}
      <div className="flex border-b border-white/10">
        {visibleTabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-xs uppercase tracking-wider font-medium transition-colors cursor-pointer ${
              activeTab === tab.key
                ? "text-earl-400 border-b-2 border-earl-400"
                : "text-gray-500 hover:text-gray-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab Content */}
      <div className="p-4">
        {activeTab === "boxscore" && renderBoxScore()}
        {activeTab === "summary" && renderGameSummary()}
        {activeTab === "analysis" && (
          <div>
            {/* Nested sub-tabs: Analysis | Stats */}
            <div className="flex border-b border-white/10 mb-4">
              {[
                { key: "analysis", label: "Analysis" },
                { key: "stats", label: "Stats" },
              ].map(sub => (
                <button
                  key={sub.key}
                  onClick={() => setAnalysisSubTab(sub.key as "analysis" | "stats")}
                  className={`px-4 py-2 text-xs uppercase tracking-wider font-medium transition-colors cursor-pointer ${
                    analysisSubTab === sub.key
                      ? "text-earl-400 border-b-2 border-earl-400"
                      : "text-gray-500 hover:text-gray-300"
                  }`}
                >
                  {sub.label}
                </button>
              ))}
            </div>
            {analysisSubTab === "analysis" && <PremiumGate>{renderDetailedAnalysis()}</PremiumGate>}
            {analysisSubTab === "stats" && <PremiumGate>{renderDetailedStats()}</PremiumGate>}
          </div>
        )}
        {activeTab === "propBets" && renderPropBets()}
      </div>
    </div>
  );

  function renderBoxScore() {
    if (!boxscore) return (
      <div className="text-sm text-gray-500 text-center py-8">
        Boxscore not yet available. Check back during or after the game.
      </div>
    );

    const awayTeamData = boxscore?.teams?.away;
    const homeTeamData = boxscore?.teams?.home;
    const awayPlayers = (awayTeamData?.players || {}) as Record<string, any>;
    const homePlayers = (homeTeamData?.players || {}) as Record<string, any>;
    const awayTeamName = awayTeamData?.team?.teamName || game.away_team || "Away";
    const homeTeamName = homeTeamData?.team?.teamName || game.home_team || "Home";
    const lsTeams = linescore?.teams || {};
    const awayRuns = lsTeams?.away?.runs ?? game.away_score ?? "-";
    const homeRuns = lsTeams?.home?.runs ?? game.home_score ?? "-";

    // Look up player by ID from players dict (keyed "ID{id}")
    const lookupPlayer = (players: Record<string, any>, id: number | string) =>
      players[`ID${id}`];

    // Get position abbreviation from player data
    const getPos = (p: any) => p?.position?.abbreviation || "-";

    // Build batting order from battingOrder array (contains player IDs)
    const buildBatters = (battingOrder: number[] | undefined, players: Record<string, any>) =>
      (battingOrder || []).map((pid: number) => lookupPlayer(players, pid)).filter(Boolean);

    // Build pitchers from pitchers array (contains player IDs)
    const buildPitchers = (pitcherIds: number[] | undefined, players: Record<string, any>) =>
      (pitcherIds || []).map((pid: number) => lookupPlayer(players, pid)).filter(Boolean);

    const awayBatters = buildBatters(awayTeamData?.battingOrder, awayPlayers);
    const homeBatters = buildBatters(homeTeamData?.battingOrder, homePlayers);
    const awayPitcherList = buildPitchers(awayTeamData?.pitchers, awayPlayers);
    const homePitcherList = buildPitchers(homeTeamData?.pitchers, homePlayers);

    return (
      <div className="space-y-6">
        {/* Inning-by-inning linescore */}
        {linescore?.innings && linescore.innings.length > 0 && (
          <div className="overflow-x-auto border border-white/10 rounded-xl p-2">
            <table className="w-full text-xs text-center">
              <thead>
                <tr className="text-gray-500 border-b border-white/10">
                  <th className="py-1 px-2 text-left"></th>
                  {linescore.innings.map((_: any, i: number) => (
                    <th key={i} className="py-1 px-2 w-7">{i + 1}</th>
                  ))}
                  <th className="py-1 px-2 w-7 font-bold">R</th>
                  <th className="py-1 px-2 w-7 font-bold">H</th>
                  <th className="py-1 px-2 w-7 font-bold">E</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-white/5">
                  <td className="py-1 px-2 text-left text-white font-medium">{awayTeamName}</td>
                  {linescore.innings.map((inn: any, i: number) => (
                    <td key={i} className="py-1 px-2 text-gray-300">{inn.away?.runs != null ? inn.away?.runs : "-"}</td>
                  ))}
                  <td className="py-1 px-2 text-white font-bold">{awayRuns}</td>
                  <td className="py-1 px-2 text-gray-300">{lsTeams?.away?.hits ?? "-"}</td>
                  <td className="py-1 px-2 text-gray-300">{lsTeams?.away?.errors ?? "-"}</td>
                </tr>
                <tr>
                  <td className="py-1 px-2 text-left text-white font-medium">{homeTeamName}</td>
                  {linescore.innings.map((inn: any, i: number) => (
                    <td key={i} className="py-1 px-2 text-gray-300">{inn.home?.runs != null ? inn.home?.runs : "-"}</td>
                  ))}
                  <td className="py-1 px-2 text-white font-bold">{homeRuns}</td>
                  <td className="py-1 px-2 text-gray-300">{lsTeams?.home?.hits ?? "-"}</td>
                  <td className="py-1 px-2 text-gray-300">{lsTeams?.home?.errors ?? "-"}</td>
                </tr>
                {linescore.currentInningOrdinal && game.game_status && !["Final", "Completed"].includes(game.game_status) && (
                  <tr>
                    <td colSpan={linescore.innings.length + 4} className="pt-2 text-center">
                      <span className="text-xs text-yellow-400 bg-yellow-400/10 px-3 py-0.5 rounded-full">
                        {linescore.inningState || "In Progress"} {linescore.currentInningOrdinal}
                      </span>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {/* Away team batting */}
        {awayBatters.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-white mb-2">{awayTeamName} — Batting</h4>
            <div className="overflow-x-auto border border-white/10 rounded-xl">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider border-b border-white/10">
                    <th className="text-left py-1.5 px-2">Batter</th>
                    <th className="text-center py-1.5 px-2 w-7">Pos</th>
                    <th className="text-center py-1.5 px-2 w-7">AB</th>
                    <th className="text-center py-1.5 px-2 w-7">R</th>
                    <th className="text-center py-1.5 px-2 w-7">H</th>
                    <th className="text-center py-1.5 px-2 w-8">RBI</th>
                    <th className="text-center py-1.5 px-2 w-7">BB</th>
                    <th className="text-center py-1.5 px-2 w-7">SO</th>
                    <th className="text-center py-1.5 px-2 w-8">LOB</th>
                    <th className="text-center py-1.5 px-2 w-10">AVG</th>
                  </tr>
                </thead>
                <tbody>
                  {awayBatters.map((p: any, i: number) => {
                    const s = p?.stats?.batting || {};
                    const ss = p?.seasonStats?.batting || {};
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/[0.02]">
                        <td className="py-1 px-2 text-white text-xs font-medium">{p?.person?.fullName || "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{getPos(p)}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.atBats ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.runs ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.hits ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.rbi ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.baseOnBalls ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.strikeOuts ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.leftOnBase ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center" title={`Season AVG${ss.atBats ? ` (${ss.atBats} AB)` : ""}`}>{ss.avg ?? "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Away team pitching */}
        {awayPitcherList.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-white mb-2">{awayTeamName} — Pitching</h4>
            <div className="overflow-x-auto border border-white/10 rounded-xl">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider border-b border-white/10">
                    <th className="text-left py-1.5 px-2">Pitcher</th>
                    <th className="text-center py-1.5 px-2 w-7">IP</th>
                    <th className="text-center py-1.5 px-2 w-7">H</th>
                    <th className="text-center py-1.5 px-2 w-7">R</th>
                    <th className="text-center py-1.5 px-2 w-7">ER</th>
                    <th className="text-center py-1.5 px-2 w-7">BB</th>
                    <th className="text-center py-1.5 px-2 w-7">SO</th>
                    <th className="text-center py-1.5 px-2 w-7">HR</th>
                    <th className="text-center py-1.5 px-2 w-10">ERA</th>
                  </tr>
                </thead>
                <tbody>
                  {awayPitcherList.map((p: any, i: number) => {
                    const ps = p?.stats?.pitching || {};
                    const pss = p?.seasonStats?.pitching || {};
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/[0.02]">
                        <td className="py-1 px-2 text-white text-xs font-medium">{p?.person?.fullName || "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.inningsPitched ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.hits ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.runs ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.earnedRuns ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.baseOnBalls ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.strikeOuts ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.homeRuns ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center" title="Season ERA">{pss.era ?? "-.--"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Home team batting */}
        {homeBatters.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-white mb-2">{homeTeamName} — Batting</h4>
            <div className="overflow-x-auto border border-white/10 rounded-xl">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider border-b border-white/10">
                    <th className="text-left py-1.5 px-2">Batter</th>
                    <th className="text-center py-1.5 px-2 w-7">Pos</th>
                    <th className="text-center py-1.5 px-2 w-7">AB</th>
                    <th className="text-center py-1.5 px-2 w-7">R</th>
                    <th className="text-center py-1.5 px-2 w-7">H</th>
                    <th className="text-center py-1.5 px-2 w-8">RBI</th>
                    <th className="text-center py-1.5 px-2 w-7">BB</th>
                    <th className="text-center py-1.5 px-2 w-7">SO</th>
                    <th className="text-center py-1.5 px-2 w-8">LOB</th>
                    <th className="text-center py-1.5 px-2 w-10">AVG</th>
                  </tr>
                </thead>
                <tbody>
                  {homeBatters.map((p: any, i: number) => {
                    const s = p?.stats?.batting || {};
                    const ss = p?.seasonStats?.batting || {};
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/[0.02]">
                        <td className="py-1 px-2 text-white text-xs font-medium">{p?.person?.fullName || "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{getPos(p)}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.atBats ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.runs ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.hits ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.rbi ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.baseOnBalls ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.strikeOuts ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{s.leftOnBase ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center" title={`Season AVG${ss.atBats ? ` (${ss.atBats} AB)` : ""}`}>{ss.avg ?? "-"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Home team pitching */}
        {homePitcherList.length > 0 && (
          <div>
            <h4 className="text-sm font-semibold text-white mb-2">{homeTeamName} — Pitching</h4>
            <div className="overflow-x-auto border border-white/10 rounded-xl">
              <table className="w-full text-xs">
                <thead>
                  <tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider border-b border-white/10">
                    <th className="text-left py-1.5 px-2">Pitcher</th>
                    <th className="text-center py-1.5 px-2 w-7">IP</th>
                    <th className="text-center py-1.5 px-2 w-7">H</th>
                    <th className="text-center py-1.5 px-2 w-7">R</th>
                    <th className="text-center py-1.5 px-2 w-7">ER</th>
                    <th className="text-center py-1.5 px-2 w-7">BB</th>
                    <th className="text-center py-1.5 px-2 w-7">SO</th>
                    <th className="text-center py-1.5 px-2 w-7">HR</th>
                    <th className="text-center py-1.5 px-2 w-10">ERA</th>
                  </tr>
                </thead>
                <tbody>
                  {homePitcherList.map((p: any, i: number) => {
                    const ps = p?.stats?.pitching || {};
                    const pss = p?.seasonStats?.pitching || {};
                    return (
                      <tr key={i} className="border-b border-white/5 hover:bg-white/[0.02]">
                        <td className="py-1 px-2 text-white text-xs font-medium">{p?.person?.fullName || "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.inningsPitched ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.hits ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.runs ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.earnedRuns ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.baseOnBalls ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.strikeOuts ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center">{ps.homeRuns ?? "-"}</td>
                        <td className="py-1 px-2 text-gray-400 text-xs text-center" title="Season ERA">{pss.era ?? "-.--"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {/* Team stats summary */}
        {(awayTeamData?.teamStats || homeTeamData?.teamStats) && (
          <div className="grid grid-cols-2 gap-4">
            {awayTeamData?.teamStats?.batting && (
              <div className="border border-white/10 rounded-xl p-3">
                <h5 className="text-xs font-semibold text-white mb-2">{awayTeamName}</h5>
                <div className="space-y-1 text-xs text-gray-400">
                  <div className="flex justify-between"><span>AVG</span><span>{awayTeamData.teamStats.batting.avg || "-"}</span></div>
                  <div className="flex justify-between"><span>OBP</span><span>{awayTeamData.teamStats.batting.obp || "-"}</span></div>
                  <div className="flex justify-between"><span>SLG</span><span>{awayTeamData.teamStats.batting.slg || "-"}</span></div>
                  <div className="flex justify-between"><span>OPS</span><span>{awayTeamData.teamStats.batting.ops || "-"}</span></div>
                  <div className="flex justify-between"><span>LOB</span><span>{awayTeamData.teamStats.batting.leftOnBase ?? linescore?.teams?.away?.leftOnBase ?? "-"}</span></div>
                </div>
              </div>
            )}
            {homeTeamData?.teamStats?.batting && (
              <div className="border border-white/10 rounded-xl p-3">
                <h5 className="text-xs font-semibold text-white mb-2">{homeTeamName}</h5>
                <div className="space-y-1 text-xs text-gray-400">
                  <div className="flex justify-between"><span>AVG</span><span>{homeTeamData.teamStats.batting.avg || "-"}</span></div>
                  <div className="flex justify-between"><span>OBP</span><span>{homeTeamData.teamStats.batting.obp || "-"}</span></div>
                  <div className="flex justify-between"><span>SLG</span><span>{homeTeamData.teamStats.batting.slg || "-"}</span></div>
                  <div className="flex justify-between"><span>OPS</span><span>{homeTeamData.teamStats.batting.ops || "-"}</span></div>
                  <div className="flex justify-between"><span>LOB</span><span>{homeTeamData.teamStats.batting.leftOnBase ?? linescore?.teams?.home?.leftOnBase ?? "-"}</span></div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Game status for live games */}
        {linescore?.currentInningOrdinal && game.game_status && !["Final", "Completed"].includes(game.game_status) && (
          <div className="text-center">
            <span className="text-xs text-yellow-400 bg-yellow-400/10 px-3 py-1 rounded-full">
              {game.game_status} - {linescore.inningState} {linescore.currentInningOrdinal}
            </span>
          </div>
        )}
      </div>
    );
  }

  function renderGameSummary() {
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
  }

  function renderDetailedAnalysis() {
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
  }

  function renderDetailedStats() {
    if (loadingStats) {
      return <div className="text-sm text-gray-400 text-center py-8">Loading detailed stats...</div>;
    }

    if (!predictionStats?.has_prediction) {
      return (
        <div className="text-sm text-gray-500 text-center py-8">
          No prediction data available yet.
        </div>
      );
    }

    const ps = predictionStats;

    // Parse all JSON columns (safety net for unparsed API responses)
    const parseSafe = (v: any) => (typeof v === "string" ? JSON.parse(v) : (v || {}));
    const featuresJson = parseSafe(ps.features_json);

    // ── Rich value extractor ─────────────────────────────────────────────────
    // Values can be plain (string/number) or dict {value, display_name, description}
    interface FeatureInfo { displayValue: string; displayName: string; description?: string; }
    function getInfo(val: any, fallbackKey?: string): FeatureInfo {
      if (val !== null && typeof val === "object" && "value" in val) {
        const raw = val.value;
        const dv = raw !== null && raw !== undefined
          ? (typeof raw === "number"
              ? (Number.isInteger(raw) ? raw.toLocaleString() : raw.toFixed(3))
              : String(raw))
          : "—";
        const dn = val.display_name || fallbackKey || "";
        return { displayValue: dv, displayName: dn, description: val.description };
      }
      const raw = val;
      const dv = raw !== null && raw !== undefined
        ? (typeof raw === "number"
            ? (Number.isInteger(raw) ? raw.toLocaleString() : raw.toFixed(3))
            : String(raw))
        : "—";
      return { displayValue: dv, displayName: fallbackKey || "", description: undefined };
    }

    // Convert snake_case key to a readable label (fallback when no display_name)
    function keyToLabel(k: string): string {
      return k
        .replace(/_/g, " ")
        .replace(/\b\w/g, (c) => c.toUpperCase());
    }

    // ── StatRow: a row with label, value, and CSS-only tooltip ────────────────
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
        {/* Label with dotted underline hinting at tooltip */}
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

        {/* Tooltip — appears on hover, arrow pointing up */}
        {description && (
          <div className="absolute z-50 bottom-full left-1/2 -translate-x-1/2 mb-2 hidden group-hover:block">
            <div className="bg-gray-900 border border-gray-700 rounded-lg shadow-xl px-3 py-2 w-64">
              <div className="text-gray-100 text-[11px] font-semibold mb-1">{label}</div>
              <p className="text-gray-400 text-[10px] leading-relaxed">{description}</p>
            </div>
            {/* Arrow */}
            <div className="flex justify-center -mt-px">
              <div className="w-0 h-0 border-l-[5px] border-r-[5px] border-t-[5px] border-transparent border-t-gray-700"></div>
            </div>
          </div>
        )}
      </div>
    );

    // ── Section header with gradient separator ───────────────────────────────
    const SectionHeader = ({ title }: { title: string }) => (
      <div className="flex items-center gap-2 mb-3">
        <span className="text-gray-500 text-[10px] uppercase tracking-[0.12em] font-semibold">{title}</span>
        <div className="flex-1 h-px bg-gradient-to-r from-gray-700/60 to-transparent" />
      </div>
    );

    // ── Sectioned feature renderer (grouped by pick_card_section) ────────────
    // Renders only the features from features_json assigned to the given
    // section (home_stats / away_stats / game_context / betting_lines / other).
    function sectionLabel(section: string) {
      const map: Record<string, string> = {
        home_stats: "Home Team Stats",
        away_stats: "Away Team Stats",
        game_context: "Game Context",
        betting_lines: "Betting Lines",
        other: "Other Stats",
      };
      return map[section] || section;
    }

    function renderSectionedFeatures(section: string) {
      let entries = Object.entries(featuresJson as Record<string, any>).filter(
        ([, val]) => {
          const v = val as { pick_card_section?: string } | null;
          return (
            val &&
            typeof val === "object" &&
            (v?.pick_card_section === section ||
              (section === "other" && !v?.pick_card_section))
          );
        },
      );
      // Order by the admin-chosen sort_order column (from mlb.features, attached
      // read-time by the backend). This only affects frontend display — the
      // data_loader keeps its own order for training/inference.
      entries = entries.sort((a, b) => {
        const av = (a[1] as any)?.sort_order ?? 0;
        const bv = (b[1] as any)?.sort_order ?? 0;
        return av - bv || a[0].localeCompare(b[0]);
      });
      if (entries.length === 0) return null;
      return (
        <div>
          <SectionHeader title={sectionLabel(section)} />
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

    // ── Predictions summary card with embedded tooltip ────────────────────────
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

    // ── Build component tree ─────────────────────────────────────────────────
    return (
      <div className="space-y-6 text-xs">
        {/* ── SHAP Feature Attribution (admin only) ── */}
        {user?.is_admin && ps.shap_json && (
          <ShapBreakdown data={ps.shap_json} />
        )}

        {/* ── Predictions Summary ── */}
        <div>
          <SectionHeader title="Predictions Summary" />
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <PredCard
              label="Home Run Estimate"
              value={ps.predicted_home_runs?.toFixed(1)}
              iconClass="from-earl-500/10 to-transparent border border-earl-500/20"
              tooltip={game.home_score != null
                ? `Model estimate for the home team's final run total — Actual: ${game.home_score}`
                : "Model estimate for the home team's final run total"}
            />
            <PredCard
              label="Away Run Estimate"
              value={ps.predicted_away_runs?.toFixed(1)}
              iconClass="from-cyan-500/10 to-transparent border border-cyan-500/20"
              tooltip={game.away_score != null
                ? `Model estimate for the away team's final run total — Actual: ${game.away_score}`
                : "Model estimate for the away team's final run total"}
            />
            {ps.home_spread_cover_prob != null && (
              <PredCard
                label="Home Cover %"
                value={`${(ps.home_spread_cover_prob * 100).toFixed(0)}%`}
                iconClass="from-green-500/10 to-transparent border border-green-500/20"
                tooltip="Probability the home team covers the run line spread"
              />
            )}
            {ps.away_spread_cover_prob != null && (
              <PredCard
                label="Away Cover %"
                value={`${(ps.away_spread_cover_prob * 100).toFixed(0)}%`}
                iconClass="from-blue-500/10 to-transparent border border-blue-500/20"
                tooltip="Probability the away team covers the run line spread"
              />
            )}
          </div>
        </div>

        {/* ── Game Context ── */}
        {renderSectionedFeatures("game_context")}

        {/* ── Betting Lines ── */}
        {renderSectionedFeatures("betting_lines")}

        {/* ── Home Team Stats ── */}
        {renderSectionedFeatures("home_stats")}

        {/* ── Away Team Stats ── */}
        {renderSectionedFeatures("away_stats")}
      </div>
    );
  }
  function renderPropBets() {
    return <PropBetsTab sport="mlb" gameId={gameId} />;
  }
}
