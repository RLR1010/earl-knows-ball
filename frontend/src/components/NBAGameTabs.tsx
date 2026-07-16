"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";

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

export default function NBAGameTabs({ gameId }: NBAGameTabsProps) {
  const [data, setData] = useState<NBABoxScoreData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<string>("boxscore");

  const isFinal = data?.status === "FINAL" || data?.status === "final";
  const hasBoxscore = !!(data && isFinal);

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

  const tabs = [
    { key: "boxscore", label: "Box Score", enabled: hasBoxscore },
    { key: "summary", label: "Game Preview", enabled: true },
    { key: "picks", label: "Earl's Picks", enabled: true },
    { key: "analysis", label: "Detailed Analysis", enabled: true },
    { key: "stats", label: "Detailed Stats", enabled: true },
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
      <div className="max-w-3xl mx-auto p-4">
        <div className="text-center py-12">
          <div className="text-gray-500 mb-4">{error || "Game not found."}</div>
          <Link href="/nba/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">
            ← Back to Schedule
          </Link>
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

  const renderGamePreview = () => (
    <div className="text-center py-16 text-gray-500">
      <div className="text-4xl mb-3">📋</div>
      <p className="text-lg font-medium text-gray-400 mb-2">Game Preview</p>
      <p className="text-sm">AI-powered preview coming soon.</p>
    </div>
  );

  const renderPicks = () => (
    <div className="text-center py-16 text-gray-500">
      <div className="text-4xl mb-3">🎯</div>
      <p className="text-lg font-medium text-gray-400 mb-2">Earl's Picks</p>
      <p className="text-sm">Pick cards and predictions coming soon.</p>
    </div>
  );

  const renderAnalysis = () => (
    <div className="text-center py-16 text-gray-500">
      <div className="text-4xl mb-3">🔍</div>
      <p className="text-lg font-medium text-gray-400 mb-2">Detailed Analysis</p>
      <p className="text-sm">Deep-dive writeups coming soon.</p>
    </div>
  );

  const renderStats = () => (
    <div className="text-center py-16 text-gray-500">
      <div className="text-4xl mb-3">📊</div>
      <p className="text-lg font-medium text-gray-400 mb-2">Detailed Stats</p>
      <p className="text-sm">Advanced stats and splits coming soon.</p>
    </div>
  );

  return (
    <div className="max-w-3xl mx-auto p-4 space-y-4">
      {/* Back link */}
      <div className="mb-2">
        <Link href="/nba/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">
          ← Back to Schedule
        </Link>
      </div>

      {/* Score Card */}
      <div className="border border-white/10 rounded-xl p-6 mb-2">
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
        <div className="border border-white/10 rounded-xl p-4 bg-white/5">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
              <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
              <div className="text-sm mt-1">
                <span className="text-gray-400">{data.away.team}</span> {formatOdds(data.betting_lines.closing_away_ml)}
                <span className="text-gray-600 mx-2">|</span>
                <span className="text-earl-400">{data.home.team}</span> {formatOdds(data.betting_lines.closing_home_ml)}
              </div>
            </div>
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
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
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
              <div className="text-[10px] text-gray-500 uppercase">Over / Under</div>
              <div className="text-sm mt-1">
                {data.betting_lines.closing_ou != null ? (
                  <>
                    O/U {data.betting_lines.closing_ou}
                    <span className="text-gray-500 text-xs ml-2 font-normal">Over {formatOdds(data.betting_lines.closing_over_odds ?? -110)}</span>
                    <span className="text-gray-500 text-xs ml-2 font-normal">Under {formatOdds(data.betting_lines.closing_under_odds ?? -110)}</span>
                  </>
                ) : "-"}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      {activeTabs.length > 0 && (
        <div className="border-b border-white/10">
          <div className="flex gap-1 -mb-px overflow-x-auto">
            {activeTabs.map((tab) => (
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
        </div>
      )}

      {/* Tab Content */}
      <div>
        {activeTab === "boxscore" && renderBoxScore()}
        {activeTab === "summary" && renderGamePreview()}
        {activeTab === "picks" && renderPicks()}
        {activeTab === "analysis" && renderAnalysis()}
        {activeTab === "stats" && renderStats()}
      </div>
    </div>
  );
}
