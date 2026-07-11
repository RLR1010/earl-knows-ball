"use client";

import { useState, useEffect, useRef } from "react";

interface MLBGameTabsProps {
  gameId: number;
  pickCard: any;
  game: any;
  formatOdds: (v: any) => string;
}

export default function MLBGameTabs({ gameId, pickCard, game, formatOdds }: MLBGameTabsProps) {
  const [activeTab, setActiveTab] = useState<string>("boxscore");
  const [writeup, setWriteup] = useState<any>(null);
  const [predictionStats, setPredictionStats] = useState<any>(null);
  const [loadingWriteup, setLoadingWriteup] = useState(false);
  const [loadingStats, setLoadingStats] = useState(false);

  const hasBoxscore = !!(game.boxscore_data?.home_score != null || game.boxscore_data?.away_score != null);

  // Track whether we've already attempted each fetch (prevents infinite loops on error)
  const writeupAttempted = useRef(false);
  const statsAttempted = useRef(false);

  // Set default tab based on whether the game has started
  useEffect(() => {
    if (!hasBoxscore) {
      setActiveTab("picks");
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

  // Fetch prediction stats when needed — fire once per gameId change
  useEffect(() => {
    if (activeTab === "stats") {
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
  }, [activeTab, gameId]);

  const tabs = [
    { key: "boxscore", label: "Box Score", enabled: hasBoxscore },
    { key: "summary", label: "Game Summary", enabled: true },
    { key: "picks", label: "Earl's Picks", enabled: true },
    { key: "analysis", label: "Detailed Analysis", enabled: true },
    { key: "stats", label: "Detailed Stats", enabled: true },
  ];

  // If no boxscore, default to summary; but show boxscore tab as disabled/absent
  const visibleTabs = tabs.filter(t => t.key !== "boxscore" || hasBoxscore);

  return (
    <div className="border border-white/10 rounded-xl bg-gradient-to-br from-blue-900/20 to-transparent mt-4">
      {/* Tabs */}
      <div className="flex border-b border-white/10">
        {visibleTabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-xs uppercase tracking-wider font-medium transition-colors ${
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
        {activeTab === "picks" && renderEarlsPicks()}
        {activeTab === "analysis" && renderDetailedAnalysis()}
        {activeTab === "stats" && renderDetailedStats()}
      </div>
    </div>
  );

  function renderBoxScore() {
    const bd = game.boxscore_data || {};
    const homeScore = bd.home_score ?? "-";
    const awayScore = bd.away_score ?? "-";
    const homeTeam = game.home_team || "Home";
    const awayTeam = game.away_team || "Away";

    return (
      <div className="space-y-4">
        {/* Final Score */}
        <div className="flex items-center justify-between px-4 py-2 bg-white/5 rounded-lg">
          <div className="flex-1 text-right">
            <div className="text-lg font-bold text-white">{awayTeam}</div>
            <div className="text-3xl font-bold text-gray-300">{awayScore}</div>
          </div>
          <div className="px-6 text-gray-500 font-bold text-lg">-</div>
          <div className="flex-1">
            <div className="text-lg font-bold text-white">{homeTeam}</div>
            <div className="text-3xl font-bold text-gray-300">{homeScore}</div>
          </div>
        </div>

        {/* Inning-by-inning linescore */}
        {bd.linescore && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-gray-500 border-b border-white/10">
                  <th className="text-left py-1 px-2"></th>
                  {bd.linescore.innings?.map((_: any, i: number) => (
                    <th key={i} className="text-center py-1 px-2">{i + 1}</th>
                  ))}
                  <th className="text-center py-1 px-2 font-bold">R</th>
                  <th className="text-center py-1 px-2 font-bold">H</th>
                  <th className="text-center py-1 px-2 font-bold">E</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b border-white/5">
                  <td className="py-1 px-2 text-white font-medium">{awayTeam}</td>
                  {bd.linescore.innings?.map((inn: any, i: number) => (
                    <td key={i} className="text-center py-1 px-2 text-gray-300">{inn.away ?? "-"}</td>
                  ))}
                  <td className="text-center py-1 px-2 text-white font-bold">{awayScore}</td>
                  <td className="text-center py-1 px-2 text-gray-300">{bd.linescore.away_hits ?? "-"}</td>
                  <td className="text-center py-1 px-2 text-gray-300">{bd.linescore.away_errors ?? "-"}</td>
                </tr>
                <tr>
                  <td className="py-1 px-2 text-white font-medium">{homeTeam}</td>
                  {bd.linescore.innings?.map((inn: any, i: number) => (
                    <td key={i} className="text-center py-1 px-2 text-gray-300">{inn.home ?? "-"}</td>
                  ))}
                  <td className="text-center py-1 px-2 text-white font-bold">{homeScore}</td>
                  <td className="text-center py-1 px-2 text-gray-300">{bd.linescore.home_hits ?? "-"}</td>
                  <td className="text-center py-1 px-2 text-gray-300">{bd.linescore.home_errors ?? "-"}</td>
                </tr>
              </tbody>
            </table>
          </div>
        )}

        {/* Game status */}
        {game.game_status && game.game_status !== "Final" && game.game_status !== "Completed" && (
          <div className="text-center">
            <span className="text-xs text-yellow-400 bg-yellow-400/10 px-3 py-1 rounded-full">
              {game.game_status} {bd.inning ? `- ${bd.inning_text || `Top ${bd.inning}`}` : ""}
            </span>
          </div>
        )}
      </div>
    );
  }

  function renderGameSummary() {
    if (loadingWriteup) {
      return <div className="text-sm text-gray-400 text-center py-8">Loading game summary...</div>;
    }

    const content = writeup?.public_content;
    if (!content) {
      return (
        <div className="text-sm text-gray-500 text-center py-8">
          No game summary available yet. Check back after the game.
        </div>
      );
    }

    return (
      <div className="prose prose-invert prose-sm max-w-none">
        {writeup.title && (
          <div className="text-sm font-semibold text-white mb-3">{writeup.title}</div>
        )}
        <div className="text-gray-300 leading-relaxed whitespace-pre-wrap">{content}</div>
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
      <div className="prose prose-invert prose-sm max-w-none">
        {writeup.title && (
          <div className="text-sm font-semibold text-white mb-3">{writeup.title}</div>
        )}
        <div className="text-gray-300 leading-relaxed whitespace-pre-wrap">{content}</div>
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
    // Parse features_json if it's a string (safety net for unparsed API responses)
    const featuresJson = typeof ps.features_json === "string" ? JSON.parse(ps.features_json) : (ps.features_json || {});

    const displayNames: Record<string, string> = {
      home_era: "Home ERA", away_era: "Away ERA",
      home_era_r5: "Home ERA (L5)", away_era_r5: "Away ERA (L5)",
      home_era_r10: "Home ERA (L10)", away_era_r10: "Away ERA (L10)",
      home_runs_scored_avg: "Home Runs/G", away_runs_scored_avg: "Away Runs/G",
      home_runs_scored_avg_r5: "Home Runs/G (L5)", away_runs_scored_avg_r5: "Away Runs/G (L5)",
      home_runs_scored_avg_r10: "Home Runs/G (L10)", away_runs_scored_avg_r10: "Away Runs/G (L10)",
      home_runs_allowed_avg: "Home Runs Allowed/G", away_runs_allowed_avg: "Away Runs Allowed/G",
      home_batting_avg: "Home AVG", away_batting_avg: "Away AVG",
      home_ops: "Home OPS", away_ops: "Away OPS",
      home_pitcher_era: "SP ERA", away_pitcher_era: "Opp SP ERA",
      home_pitcher_fip: "SP FIP", away_pitcher_fip: "Opp SP FIP",
      home_pitcher_xfip: "SP xFIP", away_pitcher_xfip: "Opp SP xFIP",
      home_pitcher_k9: "SP K/9", away_pitcher_k9: "Opp SP K/9",
      home_pitcher_bb9: "SP BB/9", away_pitcher_bb9: "Opp SP BB/9",
      home_pitcher_whip: "SP WHIP", away_pitcher_whip: "Opp SP WHIP",
      home_pitcher_war: "SP WAR", away_pitcher_war: "Opp SP WAR",
      home_bullpen_era: "BP ERA", away_bullpen_era: "Opp BP ERA",
      home_bullpen_fip: "BP FIP", away_bullpen_fip: "Opp BP FIP",
      home_bullpen_whip: "BP WHIP", away_bullpen_whip: "Opp BP WHIP",
      home_rest: "Home Rest Days", away_rest: "Away Rest Days",
      is_dome: "Dome", is_division: "Division Game",
      combo_era_r10: "Combo ERA (L10)", combo_era_r10_diff: "ERA Diff (L10)",
      combo_runs_r10: "Combo Runs (L10)", combo_runs_r10_diff: "Runs Diff (L10)",
      ou_line: "OU Line",
      w: "Wins", l: "Losses", win_pct: "Win %",
      runs_scored_per_game: "Runs/G", runs_allowed_per_game: "Runs Allowed/G",
      run_diff_per_game: "Run Diff/G",
      last_10_w: "L10 Wins", last_10_l: "L10 Losses",
      streak: "Streak",
      home_w: "Home Wins", home_l: "Home Losses", home_win_pct: "Home Win %",
      away_w: "Away Wins", away_l: "Away Losses", away_win_pct: "Away Win %",
    };

    const featureDefs: Record<string, string> = {
      home_era: "Earned Run Average (season)",
      away_era: "Opponent Earned Run Average (season)",
      home_ops: "On-base + Slugging Percentage",
      away_ops: "Opponent OPS",
      home_pitcher_era: "Starting Pitcher ERA",
      home_pitcher_fip: "Fielding Independent Pitching",
      home_pitcher_xfip: "Expected FIP",
      home_pitcher_k9: "Strikeouts per 9 innings",
      home_pitcher_bb9: "Walks per 9 innings",
      home_pitcher_whip: "Walks + Hits per Inning Pitched",
      home_pitcher_war: "Wins Above Replacement",
      combo_era_r10_diff: "Home ERA minus Away ERA (last 10)",
      combo_runs_r10_diff: "Home Runs minus Away Runs (last 10)",
    };

    // Team Stats Section
    const hs = ps.home_stats_json || {};
    const as = ps.away_stats_json || {};
    const sit = ps.situational_json || {};
    const spl = ps.splits_json || {};

    return (
      <div className="space-y-6 text-xs">
        {/* Predictions Summary */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="bg-white/5 rounded-lg p-3 text-center">
            <div className="text-gray-500 mb-1">Home Run Estimate</div>
            <div className="text-lg font-bold text-white">{ps.predicted_home_runs?.toFixed(1)}</div>
          </div>
          <div className="bg-white/5 rounded-lg p-3 text-center">
            <div className="text-gray-500 mb-1">Away Run Estimate</div>
            <div className="text-lg font-bold text-white">{ps.predicted_away_runs?.toFixed(1)}</div>
          </div>
          <div className="bg-white/5 rounded-lg p-3 text-center">
            <div className="text-gray-500 mb-1">Predicted Total</div>
            <div className="text-lg font-bold text-white">{ps.predicted_total?.toFixed(1)}</div>
          </div>
          <div className="bg-white/5 rounded-lg p-3 text-center">
            <div className="text-gray-500 mb-1">Margin</div>
            <div className="text-lg font-bold text-white">{ps.predicted_margin != null ? (ps.predicted_margin > 0 ? '+' : '') + ps.predicted_margin.toFixed(1) : '-'}</div>
          </div>
        </div>

        {/* Probabilities */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {ps.home_win_prob != null && (
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 mb-1">Home Win %</div>
              <div className="text-lg font-bold text-earl-400">{(ps.home_win_prob * 100).toFixed(0)}%</div>
            </div>
          )}
          {ps.over_prob != null && (
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 mb-1">Over %</div>
              <div className="text-lg font-bold text-earl-400">{(ps.over_prob * 100).toFixed(0)}%</div>
            </div>
          )}
          {ps.home_spread_cover_prob != null && (
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 mb-1">Home Cover %</div>
              <div className="text-lg font-bold text-earl-400">{(ps.home_spread_cover_prob * 100).toFixed(0)}%</div>
            </div>
          )}
          {ps.away_spread_cover_prob != null && (
            <div className="bg-white/5 rounded-lg p-3 text-center">
              <div className="text-gray-500 mb-1">Away Cover %</div>
              <div className="text-lg font-bold text-earl-400">{(ps.away_spread_cover_prob * 100).toFixed(0)}%</div>
            </div>
          )}
        </div>

        {/* Team Stats */}
        {hs && Object.keys(hs).length > 0 && (
          <div>
            <div className="text-gray-500 uppercase tracking-wider mb-2">Home Team Stats</div>
            <div className="grid grid-cols-3 md:grid-cols-4 gap-x-4 gap-y-1">
              {Object.entries(hs).map(([key, val]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-gray-500">{displayNames[key] || key}:</span>
                  <span className="text-white">{(val as any)?.toFixed?.(2) ?? String(val)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {as && Object.keys(as).length > 0 && (
          <div>
            <div className="text-gray-500 uppercase tracking-wider mb-2">Away Team Stats</div>
            <div className="grid grid-cols-3 md:grid-cols-4 gap-x-4 gap-y-1">
              {Object.entries(as).map(([key, val]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-gray-500">{displayNames[key] || key}:</span>
                  <span className="text-white">{(val as any)?.toFixed?.(2) ?? String(val)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Situational */}
        {sit && Object.keys(sit).length > 0 && (
          <div>
            <div className="text-gray-500 uppercase tracking-wider mb-2">Situational</div>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1">
              {Object.entries(sit).map(([key, val]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-gray-500">{displayNames[key] || key}:</span>
                  <span className="text-white">{(val as any)?.toFixed?.(2) ?? String(val)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Splits / Betting Trends */}
        {spl && Object.keys(spl).length > 0 && (
          <div>
            <div className="text-gray-500 uppercase tracking-wider mb-2">Splits / Betting Trends</div>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1">
              {Object.entries(spl).map(([key, val]) => (
                <div key={key} className="flex justify-between">
                  <span className="text-gray-500">{displayNames[key] || key}:</span>
                  <span className="text-white">{(val as any)?.toFixed?.(2) ?? String(val)}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* All Features */}
        {featuresJson && Object.keys(featuresJson).length > 0 && (
          <div>
            <div className="text-gray-500 uppercase tracking-wider mb-2">All Model Features</div>
            <div className="max-h-96 overflow-y-auto">
              <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-1">
                {Object.entries(featuresJson).map(([key, val]) => (
                  <div key={key} className="flex justify-between">
                    <span className="text-gray-500" title={featureDefs[key] || ""}>
                      {displayNames[key] || key}
                    </span>
                    <span className="text-white">{(val as any)?.toFixed?.(4) ?? String(val)}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* Actual Results (if game is over) */}
        {(ps.actual_home_runs != null || ps.actual_away_runs != null) && (
          <div className="mt-4 pt-4 border-t border-white/10">
            <div className="text-gray-500 uppercase tracking-wider mb-2">Actual Results</div>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-gray-500 mb-1">Home Runs</div>
                <div className="text-lg font-bold text-white">{ps.actual_home_runs ?? "-"}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-gray-500 mb-1">Away Runs</div>
                <div className="text-lg font-bold text-white">{ps.actual_away_runs ?? "-"}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-gray-500 mb-1">Actual Total</div>
                <div className="text-lg font-bold text-white">{ps.actual_total ?? "-"}</div>
              </div>
              <div className="bg-white/5 rounded-lg p-3 text-center">
                <div className="text-gray-500 mb-1">Margin</div>
                <div className="text-lg font-bold text-white">{ps.actual_margin != null ? (ps.actual_margin > 0 ? '+' : '') + ps.actual_margin.toFixed(0) : "-"}</div>
              </div>
            </div>
          </div>
        )}
      </div>
    );
  }

  function renderEarlsPicks() {
    const isFinal = !!(game.home_score != null && game.away_score != null);

    if (!pickCard) {
      return (
        <div className="text-center py-12">
          <div className="text-gray-500 text-sm">No picks available for this game yet</div>
          <div className="text-gray-600 text-xs mt-2">Picks are generated closer to game time</div>
        </div>
      );
    }

    return (
      <div className="space-y-4">
        {/* Predicted score */}
        {pickCard.predictions?.home_runs != null && (
          <div className="text-center mb-4">
            <div className="inline-block border border-white/10 rounded-lg px-6 py-2 bg-white/5">
              <span className="text-xs text-gray-500">Predicted</span>
              <div className="text-lg font-bold tracking-tight">
                <span className="text-gray-300">{game.away_team}</span>
                <span className="text-white mx-2">{pickCard.predictions.away_runs}</span>
                <span className="text-gray-600">@</span>
                <span className="text-white mx-2">{pickCard.predictions.home_runs}</span>
                <span className="text-gray-300">{game.home_team}</span>
              </div>
              <div className="text-xs text-gray-500 mt-1">
                Total: {pickCard.predictions.total} | Margin: {pickCard.predictions.margin >= 0 ? "+" : ""}{pickCard.predictions.margin}
              </div>
            </div>
          </div>
        )}

        {/* Actual score for completed games */}
        {isFinal && pickCard.actual?.home_runs != null && (
          <div className="text-center mb-4">
            <div className="inline-block border border-green-500/20 rounded-lg px-6 py-2 bg-green-500/5">
              <span className="text-xs text-gray-500">Actual</span>
              <div className="text-lg font-bold tracking-tight">
                <span className="text-gray-300">{game.away_team}</span>
                <span className="text-white mx-2">{pickCard.actual.away_runs}</span>
                <span className="text-gray-600">@</span>
                <span className="text-white mx-2">{pickCard.actual.home_runs}</span>
                <span className="text-gray-300">{game.home_team}</span>
              </div>
              <div className="text-xs text-gray-500 mt-1">
                Total: {pickCard.actual.total} | Margin: {pickCard.actual.margin >= 0 ? "+" : ""}{pickCard.actual.margin}
              </div>
            </div>
          </div>
        )}

        {/* Three pick cards: ATS, OU, Moneyline */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          {/* ATS / Run Line */}
          <div className="rounded-lg p-3 border border-amber-500/40 bg-amber-500/10">
            <div className="text-[10px] text-gray-500 uppercase">Run Line</div>
            {isFinal && pickCard.results?.run_line ? (
              <>
                <div className={`text-lg font-bold mt-1 ${pickCard.results.run_line === "Win" ? "text-green-400" : "text-red-400"}`}>
                  {pickCard.results.run_line}
                </div>
                <div className="flex items-center gap-2 mt-1">
                  {pickCard.expected_value?.rl != null && (
                    <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.rl >= 0 ? "text-green-400" : "text-red-400"}`}>
                      EV: {pickCard.expected_value.rl >= 0 ? "+" : ""}{pickCard.expected_value.rl.toFixed(1)}¢
                    </span>
                  )}
                  Pick: {pickCard.picks?.run_line || "-"}
                </div>
              </>
            ) : isFinal && pickCard.results?.run_line === "Push" ? (
              <div className="text-sm font-bold mt-1 text-gray-400">Push</div>
            ) : pickCard.picks?.run_line && pickCard.picks.run_line !== "-" ? (
              <>
                <div className="text-lg font-bold mt-1 text-amber-400">{pickCard.picks.run_line.toUpperCase()}</div>
                {pickCard.expected_value?.rl != null && (
                  <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.rl >= 0 ? "text-green-400" : "text-red-400"}`}>
                    EV: {pickCard.expected_value.rl >= 0 ? "+" : ""}{pickCard.expected_value.rl.toFixed(1)}¢
                  </span>
                )}
                <div className="text-xs text-gray-500 mt-1">
                  {pickCard.lines?.run_line != null && `Run Line ${pickCard.lines.run_line}`}
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-400 mt-1">No RL data</div>
            )}
          </div>

          {/* Over/Under */}
          <div className="rounded-lg p-3 border border-yellow-500/40 bg-yellow-500/10">
            <div className="text-[10px] text-gray-500 uppercase">Over/Under</div>
            {isFinal && pickCard.results?.over_under ? (
              <>
                <div className={`text-lg font-bold mt-1 ${pickCard.results.over_under === "Win" ? "text-green-400" : "text-red-400"}`}>
                  {pickCard.results.over_under}
                </div>
                <div className="flex items-center gap-2 mt-1">
                  {pickCard.expected_value?.ou != null && (
                    <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.ou >= 0 ? "text-green-400" : "text-red-400"}`}>
                      EV: {pickCard.expected_value.ou >= 0 ? "+" : ""}{pickCard.expected_value.ou.toFixed(1)}¢
                    </span>
                  )}
                  Pick: {pickCard.picks?.over_under || "-"}
                </div>
              </>
            ) : isFinal && pickCard.results?.over_under === "Push" ? (
              <div className="text-sm font-bold mt-1 text-gray-400">Push</div>
            ) : pickCard.picks?.over_under && pickCard.picks.over_under !== "-" ? (
              <>
                <div className="text-lg font-bold mt-1 text-yellow-400">{pickCard.picks.over_under.toUpperCase()}</div>
                {pickCard.expected_value?.ou != null && (
                  <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.ou >= 0 ? "text-green-400" : "text-red-400"}`}>
                    EV: {pickCard.expected_value.ou >= 0 ? "+" : ""}{pickCard.expected_value.ou.toFixed(1)}¢
                  </span>
                )}
                <div className="text-xs text-gray-500 mt-1">
                  {pickCard.lines?.over_under != null && `O/U ${pickCard.lines.over_under}`}
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-400 mt-1">No OU data</div>
            )}
          </div>

          {/* Moneyline */}
          <div className="rounded-lg p-3 border border-cyan-500/40 bg-cyan-500/10">
            <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
            {isFinal && pickCard.results?.moneyline ? (
              <>
                <div className={`text-lg font-bold mt-1 ${pickCard.results.moneyline === "Win" ? "text-green-400" : "text-red-400"}`}>
                  {pickCard.results.moneyline}
                </div>
                <div className="flex items-center gap-2 mt-1">
                  {pickCard.expected_value?.ml != null && (
                    <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.ml >= 0 ? "text-green-400" : "text-red-400"}`}>
                      EV: {pickCard.expected_value.ml >= 0 ? "+" : ""}{pickCard.expected_value.ml.toFixed(1)}¢
                    </span>
                  )}
                  Pick: {pickCard.picks?.moneyline === "home" ? game.home_team : pickCard.picks?.moneyline === "away" ? game.away_team : pickCard.picks?.moneyline || "-"}
                </div>
              </>
            ) : pickCard.picks?.moneyline && pickCard.picks.moneyline !== "-" ? (
              <>
                <div className="text-lg font-bold mt-1 text-cyan-400">
                  {pickCard.picks.moneyline === "home" ? game.home_team :
                   pickCard.picks.moneyline === "away" ? game.away_team : pickCard.picks.moneyline}
                </div>
                {pickCard.expected_value?.ml != null && (
                  <span className={`text-[10px] font-semibold mt-1 ${pickCard.expected_value.ml >= 0 ? "text-green-400" : "text-red-400"}`}>
                    EV: {pickCard.expected_value.ml >= 0 ? "+" : ""}{pickCard.expected_value.ml.toFixed(1)}¢
                  </span>
                )}
                <div className="text-xs text-gray-500 mt-1">
                  {pickCard.lines?.home_moneyline != null && pickCard.lines.away_moneyline != null && (
                    <>{game.away_team} {formatOdds(pickCard.lines.away_moneyline)} | {game.home_team} {formatOdds(pickCard.lines.home_moneyline)}</>
                  )}
                </div>
              </>
            ) : (
              <div className="text-xs text-gray-400 mt-1">No ML data</div>
            )}
          </div>
        </div>
      </div>
    );
  }
}
