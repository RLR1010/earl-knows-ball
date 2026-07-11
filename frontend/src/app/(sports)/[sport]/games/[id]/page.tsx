"use client";
import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import { NBABoxScorePage } from "@/components/NBABoxScore";
import MLBGameTabs from "@/components/MLBGameTabs";

// ── Shared Types ─────────────────────────────────────────────

interface GameInfo {
  id: number; week: number; game_type: string; status: string; date: string;
  venue: string | null; roof_type: string | null;
  home_team: string; away_team: string;
  home_score: number | null; away_score: number | null;
}

interface BoxScoreStats {
  total_yards: number | null; pass_yards: number | null;
  rush_yards: number | null; turnovers: number | null;
  first_downs: number | null; third_down_pct: number | null;
  time_of_possession: string | null;
  penalties: number | null; penalty_yards: number | null;
  top_players: any[];
}

interface NFLBoxScore { game: GameInfo; home_stats: BoxScoreStats | null; away_stats: BoxScoreStats | null; }

interface GamePrediction {
  game_id: number; season: number; week: number;
  home_team: string; away_team: string; date: string | null;
  predicted: { home_score: number; away_score: number; total: number; margin: number };
  actual: { home_score: number; away_score: number; total: number; margin: number };
  results: { ats: string; ou: string; ml: string };
  confidence?: { overall: number | null; ats: number | null; ou: number | null; ml: number | null };
  line?: { spread: number | null; over_under: number | null };
}

function StatRow({ label, home, away, fmt, better }: {
  label: string; home: number | null | undefined; away: number | null | undefined;
  fmt?: (v: number) => string; better?: "high" | "low";
}) {
  const f = fmt || ((v: number) => v.toFixed(0));
  const hVal = home != null ? f(home) : "-";
  const aVal = away != null ? f(away) : "-";
  return (
    <tr className="border-t border-white/5">
      <td className="px-3 py-1.5 text-right font-medium text-gray-400">{aVal}</td>
      <td className="px-3 py-1 text-center text-gray-500">{label}</td>
      <td className="px-3 py-1.5 text-left font-medium text-gray-400">{hVal}</td>
    </tr>
  );
}

// ── Player rows for NFL boxscore ──
function NFLPlayerRows(stats: BoxScoreStats | null) {
  if (!stats?.top_players || stats.top_players.length === 0) {
    return <tr><td colSpan={5} className="px-3 py-4 text-center text-gray-600">No player stats available</td></tr>;
  }
  return stats.top_players.filter((p: any) => ["QB","RB","WR","TE"].includes(p.position)).slice(0, 8).map((p: any, i: number) => {
    const pass = p.pass_yards ? `${p.pass_completions}/${p.pass_attempts}, ${p.pass_yards}yds, ${p.pass_tds}TD` : "";
    const rush = p.rush_yards ? `${p.rush_attempts}car, ${p.rush_yards}yds, ${p.rush_tds}TD` : "";
    const recv = p.receptions ? `${p.receptions}rec, ${p.receiving_yards}yds, ${p.receiving_tds}TD` : "";
    const summary = [pass, rush, recv].filter(Boolean).join(" | ");
    return (
      <tr key={i} className="border-t border-white/5">
        <td className="px-3 py-1.5 text-gray-300">{p.player_name}</td>
        <td className="px-3 py-1.5 text-gray-500">{p.position}</td>
        <td className="px-3 py-1.5 text-gray-400 text-xs" colSpan={3}>{summary}</td>
      </tr>
    );
  });
}

// ── NFL Pick Card Display ──
function formatSpreadLine(spread: number | null | undefined, homeTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${homeTeam} +${spread}`;
  if (spread < 0) return `${homeTeam} ${spread}`;
  return "PK";
}

function formatLineAway(spread: number | null | undefined, awayTeam: string): string {
  if (spread == null) return "";
  if (spread > 0) return `${awayTeam} -${spread}`;
  if (spread < 0) return `${awayTeam} +${Math.abs(spread)}`;
  return "PK";
}

function ConfidenceBar({ score, size = "md" }: { score: number | null | undefined; size?: "sm" | "md" }) {
  if (score == null) return null;
  const pct = Math.round(score * 100);
  const color = pct >= 70 ? "bg-green-500" : pct >= 55 ? "bg-yellow-500" : "bg-gray-500";
  const label = pct >= 70 ? "High" : pct >= 55 ? "Med" : "Low";
  const h = size === "sm" ? "h-1" : "h-1.5";
  return (
    <div className="flex items-center gap-2 mt-1">
      <div className={`flex-1 ${h} bg-white/10 rounded-full overflow-hidden`}>
        <div className={`${h} rounded-full ${color} transition-all`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[10px] font-semibold ${pct >= 70 ? "text-green-400" : pct >= 55 ? "text-yellow-400" : "text-gray-500"}`}>{pct}%</span>
    </div>
  );
}

function NFLPickCard({ pred }: { pred: GamePrediction }) {
  const predicted = pred?.predicted || {};
  const actual = pred?.actual || {};
  const results = pred?.results || {};
  const line = pred?.line;
  const conf = pred?.confidence;
  const noPrediction = !results.ats || results.ats === "N/A";
  return (
    <div className="border border-white/10 rounded-xl p-4 bg-gradient-to-br from-earl-900/20 to-transparent mt-6">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Earl's Prediction</div>

      {/* Closing line display */}
      {line?.spread != null && (
        <div className="text-center mb-4">
          <span className="inline-block px-5 py-2 rounded-lg bg-gradient-to-r from-earl-800/40 via-earl-600/50 to-earl-800/40 border border-earl-500/50 text-base font-bold tracking-wide">
            <span className="text-earl-200">{formatLineAway(line.spread, pred?.away_team || "")}</span>
            <span className="mx-3 text-gray-500">|</span>
            <span className="text-earl-300">{formatSpreadLine(line.spread, pred?.home_team || "")}</span>
            {line.over_under != null && (
              <>
                <span className="mx-3 text-gray-500">|</span>
                <span className="text-white">O/U {line.over_under}</span>
              </>
            )}
          </span>
        </div>
      )}

      {/* Overall confidence badge */}
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
        <div className="grid grid-cols-3 gap-4">
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">ATS</div>
            <div className={`text-lg font-bold mt-1 ${results.ats === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ats || "-"}</div>
            <ConfidenceBar score={conf?.ats ?? conf?.overall} />
            <div className="text-xs text-gray-400 mt-1">Pred: {predicted.home_score || "?"}-{predicted.away_score || "?"} | Act: {actual.home_score || "?"}-{actual.away_score || "?"}</div>
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">O/U</div>
            <div className={`text-lg font-bold mt-1 ${results.ou === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ou || "-"}</div>
            <ConfidenceBar score={conf?.ou ?? conf?.overall} />
            <div className="text-xs text-gray-400 mt-1">Pred: {predicted.total || "?"} | Act: {actual.total || "?"}</div>
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">ML</div>
            <div className={`text-lg font-bold mt-1 ${results.ml === "Win" ? "text-green-400" : "text-red-400"}`}>{results.ml || "-"}</div>
            <ConfidenceBar score={conf?.ml ?? conf?.overall} />
            <div className="text-xs text-gray-400 mt-1">{pred?.away_team || ""} @ {pred?.home_team || ""}</div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── NFL Box Score ──
function NFLBoxScore({ data }: { data: NFLBoxScore }) {
  const { game, home_stats, away_stats } = data;
  const isFinal = game.status?.toLowerCase() === "final";
  const badge = isFinal ? { label: "FINAL", cls: "text-green-400" } : { label: game.status?.toUpperCase() || "SCHEDULED", cls: "text-earl-400" };
  const hWon = isFinal && (game.home_score ?? 0) > (game.away_score ?? 0);
  const aWon = isFinal && (game.away_score ?? 0) > (game.home_score ?? 0);

  return (
    <div className="space-y-6">
      <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 text-center">
        <span className={`text-sm font-bold ${badge.cls}`}>{badge.label}</span>
        {game.week && <span className="text-sm text-gray-500 ml-3">Week {game.week}</span>}
        <div className="flex items-center justify-center gap-8 md:gap-16 mt-4">
          <div className="text-right">
            <div className="text-lg font-semibold text-gray-300">{game.away_team}</div>
            <div className={`text-5xl font-bold mt-1 ${aWon ? "text-earl-400" : "text-gray-400"}`}>
              {(isFinal || badge.label === "LIVE") && game.away_score != null ? game.away_score : "-"}
            </div>
          </div>
          <div className="text-3xl text-gray-600 font-bold">@</div>
          <div className="text-left">
            <div className="text-lg font-semibold text-gray-300">{game.home_team}</div>
            <div className={`text-5xl font-bold mt-1 ${hWon ? "text-earl-400" : "text-gray-400"}`}>
              {(isFinal || badge.label === "LIVE") && game.home_score != null ? game.home_score : "-"}
            </div>
          </div>
        </div>
        <div className="text-sm text-gray-500 mt-4">{game.venue ? `${game.date} - ${game.venue}` : game.date}</div>
      </div>
      <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-earl-400">Team Stats</div>
        <table className="w-full text-xs">
          <thead><tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
            <th className="px-3 py-1.5 text-right w-[40%]">{game.away_team}</th><th className="px-3 py-1.5 text-center w-[20%]"></th><th className="px-3 py-1.5 text-left w-[40%]">{game.home_team}</th>
          </tr></thead>
          <tbody>
            <StatRow label="Score" home={game.home_score} away={game.away_score} fmt={(v:number)=>v.toFixed(0)} better="high" />
            <StatRow label="Total Yards" home={home_stats?.total_yards} away={away_stats?.total_yards} better="high" />
            <StatRow label="Pass Yards" home={home_stats?.pass_yards} away={away_stats?.pass_yards} better="high" />
            <StatRow label="Rush Yards" home={home_stats?.rush_yards} away={away_stats?.rush_yards} better="high" />
            <StatRow label="Turnovers" home={home_stats?.turnovers} away={away_stats?.turnovers} better="low" />
            <StatRow label="First Downs" home={home_stats?.first_downs} away={away_stats?.first_downs} better="high" />
            <StatRow label="Penalties" home={home_stats?.penalties} away={away_stats?.penalties} better="low" />
          </tbody>
        </table>
      </div>
      {away_stats && <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold">{game.away_team} - Key Players</div>
        <table className="w-full text-xs"><thead><tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
          <th className="px-3 py-1.5 text-left">Player</th><th className="px-3 py-1.5 text-left">Pos</th><th className="px-3 py-1.5 text-left">Stats</th>
        </tr></thead><tbody>{NFLPlayerRows(away_stats)}</tbody></table>
      </div>}
      {home_stats && <div className="border border-white/10 rounded-xl overflow-hidden">
        <div className="bg-white/5 px-4 py-2 text-sm font-semibold">{game.home_team} - Key Players</div>
        <table className="w-full text-xs"><thead><tr className="bg-white/[0.03] text-gray-500 uppercase text-[10px] tracking-wider">
          <th className="px-3 py-1.5 text-left">Player</th><th className="px-3 py-1.5 text-left">Pos</th><th className="px-3 py-1.5 text-left">Stats</th>
        </tr></thead><tbody>{NFLPlayerRows(home_stats)}</tbody></table>
      </div>}
    </div>
  );
}

// ── Main Page ──
export default function GameDetailPage() {
  const params = useParams<{ sport: string; id: string }>();
  const searchParams = useSearchParams();
  const sport = params?.sport; const gameId = params?.id;
  const isNfl = sport === "nfl";
  // Preserve the originating schedule context
  const returnYear = searchParams.get('year');
  const returnWeek = searchParams.get('week');
  const returnDate = searchParams.get('date');
  const backHref = returnDate
    ? `/${sport}/schedule?year=${returnYear || ''}&date=${returnDate}`
    : `/${sport}/schedule${returnYear ? `?year=${returnYear}&week=${returnWeek}` : ''}`;

  const [nflBoxScore, setNflBoxScore] = useState<NFLBoxScore | null>(null);
  const [prediction, setPrediction] = useState<GamePrediction | null>(null);
  const [gameLine, setGameLine] = useState<{ spread: number | null; over_under: number | null } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // NFL data fetching
  useEffect(() => {
    if (!gameId || !isNfl) { if (!isNfl) setLoading(false); return; }
    const gid = parseInt(gameId);
    Promise.all([
      fetch(`/api/games/${gid}/box-score`).then(r => r.json()).catch(() => null),
      fetch(`/api/handicapping/predictions/${gid}`).then(r => r.json()).catch(() => null),
      fetch(`/api/games/${gid}`).then(r => r.json()).catch(() => null),
    ]).then(([box, pred, game]) => {
      if (box?.game) setNflBoxScore(box as NFLBoxScore);
      if (pred?.game_id) setPrediction(pred as GamePrediction);
      // Always get the line from the game API (or fallback from predictions endpoint)
      const line = game?.spread != null ? { spread: game.spread, over_under: game.over_under } :
                   pred?.line?.spread != null ? pred.line : null;
      if (line) setGameLine(line);
      if (!box?.game && !pred?.game_id && !game) setError("Game not found");
      setLoading(false);
    }).catch(() => setLoading(false));
  }, [gameId, isNfl]);

  // MLB: show classic boxscore page
  if (sport === "mlb") {
    return <MLBClassicPage gameId={gameId} />;
  }

  if (sport === "nba") {
    return <NBABoxScorePage gameId={gameId} />;
  }

  if (loading) return <div className="text-center py-12 text-gray-500">Loading...</div>;

  if (error || (!nflBoxScore && !prediction)) {
    return <div className="text-center py-12"><div className="text-gray-500 mb-4">{error || "Game not found."}</div>
      <Link href={backHref} className="text-sm text-earl-400 hover:text-earl-300">← Back to Schedule</Link></div>;
  }

  // Create a 'fake' prediction with just line data for the pick card
  const lineOnlyPred: GamePrediction | null = gameLine && !prediction ? {
    game_id: parseInt(gameId || "0"), season: 0, week: 0,
    home_team: nflBoxScore?.game?.home_team || "",
    away_team: nflBoxScore?.game?.away_team || "",
    date: nflBoxScore?.game?.date || null,
    predicted: { home_score: 0, away_score: 0, total: 0, margin: 0 },
    actual: { home_score: 0, away_score: 0, total: 0, margin: 0 },
    results: { ats: "N/A", ou: "N/A", ml: "N/A" },
    confidence: { overall: null, ats: null, ou: null, ml: null },
    line: gameLine,
  } : null;

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Closing line banner - always show at top */}
      {(gameLine || prediction?.line) && (
        <div className="border border-white/10 rounded-xl p-4 bg-gradient-to-br from-earl-900/20 to-transparent text-center">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Closing Line</div>
          <span className="inline-block px-6 py-2.5 rounded-lg bg-gradient-to-r from-earl-800/40 via-earl-600/50 to-earl-800/40 border border-earl-500/50 text-lg font-bold tracking-wide">
            <span className="text-earl-200">{formatLineAway((gameLine || prediction?.line)?.spread || null, nflBoxScore?.game?.away_team || "")}</span>
            <span className="mx-4 text-gray-500">|</span>
            <span className="text-earl-300">{formatSpreadLine((gameLine || prediction?.line)?.spread || null, nflBoxScore?.game?.home_team || "")}</span>
            {(gameLine || prediction?.line)?.over_under != null && (
              <>
                <span className="mx-4 text-gray-500">|</span>
                <span className="text-white">O/U {(gameLine || prediction?.line)?.over_under}</span>
              </>
            )}
          </span>
        </div>
      )}
      {nflBoxScore && <NFLBoxScore data={nflBoxScore} />}
      {prediction && <NFLPickCard pred={prediction} />}
      {lineOnlyPred && <NFLPickCard pred={lineOnlyPred} />}
      <div className="text-center pt-4">
        <Link href={backHref} className="text-sm text-earl-400 hover:text-earl-300 transition">← Back to Schedule</Link>
      </div>
    </div>
  );
}

// ── MLB Classic Box Score Page (restored from original) ──

interface MLBBoxScoreResponse {
  game: any; boxscore: any; linescore: any;
  betting_lines: any[]; pick_card: any; splits: any;
  lineups: { home: {order:number;name:string;position:string;stats?:{avg?:string;era?:string;ops?:string}}[]; away: {order:number;name:string;position:string;stats?:{avg?:string;era?:string;ops?:string}}[] } | null;
}

function MLBClassicPage({ gameId }: { gameId: string | undefined }) {
  const [data, setData] = useState<MLBBoxScoreResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!gameId) return;
    console.log('MLBClassicPage fetching for game', gameId);
    fetch(`/api/mlb/games/${gameId}/boxscore`)
      .then(r => {
        console.log('MLBClassicPage response status', r.status);
        return r.json();
      })
      .then(d => {
        console.log('MLBClassicPage data received', d ? Object.keys(d) : 'null');
        setData(d);
        setLoading(false);
      })
      .catch(e => {
        console.error('MLBClassicPage fetch error', e);
        setLoading(false);
      });
  }, [gameId]);

  if (loading) return <div className="text-center py-12 text-gray-500">Loading...</div>;
  if (!data) return <div className="text-center py-12 text-gray-500">Game not found.</div>;

  const { game, boxscore, linescore, betting_lines, pick_card, splits, lineups } = data;
  const isUpcoming = game?.status?.toLowerCase() === "scheduled" || game?.status?.toLowerCase() === "pregame";
  const isLive = game?.status?.toLowerCase() === "in_progress";
  const isFinal = game?.status?.toLowerCase() === "final";
  const awaySide = boxscore?.teams?.away;
  const homeSide = boxscore?.teams?.home;

  function formatDate(iso: string) { const d = new Date(iso); return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/Chicago" }); }
  function formatTime(iso: string) { const d = new Date(iso); return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/Chicago" }); }
  function statusBadge(status: string) {
    switch (status?.toLowerCase()) {
      case "final": return { label: "FINAL", cls: "text-green-400" };
      case "in_progress": return { label: "LIVE", cls: "text-red-400 animate-pulse" };
      case "postponed": return { label: "PPD", cls: "text-yellow-400" };
      case "cancelled": return { label: "CANC", cls: "text-gray-500" };
      default: return { label: isUpcoming ? formatTime(game.date) : "SCHEDULED", cls: "text-earl-400" };
    }
  }
  const badge = statusBadge(game?.status || "");
  const hWon = isFinal && (game.home_score ?? 0) > (game.away_score ?? 0);
  const aWon = isFinal && (game.away_score ?? 0) > (game.home_score ?? 0);
  const totalRuns = (game.home_score ?? 0) + (game.away_score ?? 0);

  function confidenceBar(conf: number) { return conf >= 0.7 ? "bg-green-500" : conf >= 0.4 ? "bg-yellow-500" : "bg-gray-500"; }
  function confidenceLabel(conf: number) { return conf >= 0.7 ? "HIGH" : conf >= 0.4 ? "MED" : conf > 0 ? "LOW" : "-"; }
  function formatOdds(odds: number | null) { if (!odds) return "-"; return odds > 0 ? `+${odds}` : `${odds}`; }

  return (
    <div className="max-w-6xl mx-auto space-y-6">
      {/* Scoreboard */}
      <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 text-center">
        <span className={`text-sm font-bold ${badge.cls}`}>{badge.label}</span>
        {isLive && linescore?.currentInning && (
          <span className="text-sm font-semibold text-white ml-3">
            {linescore.inningState === "Top" ? "▲" : "▼"} {linescore.currentInningOrdinal || `${linescore.currentInning}`}
          </span>
        )}
        {game.date && <span className="text-xs text-gray-500 ml-3">{formatDate(game.date)}</span>}
        <div className="flex items-center justify-center gap-8 md:gap-16 mt-4">
          <div className="text-right">
            <div className="text-lg font-semibold text-gray-300">{game.away_team}</div>
            <div className={`text-5xl font-bold mt-1 ${aWon ? "text-earl-400" : "text-gray-400"}`}>
              {game.away_score != null ? game.away_score : "-"}
            </div>
          </div>
          <div className="text-3xl text-gray-600 font-bold">@</div>
          <div className="text-left">
            <div className="text-lg font-semibold text-gray-300">{game.home_team}</div>
            <div className={`text-5xl font-bold mt-1 ${hWon ? "text-earl-400" : "text-gray-400"}`}>
              {game.home_score != null ? game.home_score : "-"}
            </div>
          </div>
        </div>
        <div className="text-sm text-gray-500 mt-3">
          {game.venue && <span>{game.venue} | </span>}
          {game.attendance && <span>Att: {game.attendance.toLocaleString()} | </span>}
          {game.duration_minutes && <span>{Math.floor(game.duration_minutes / 60)}:{String(game.duration_minutes % 60).padStart(2, "0")}</span>}
        </div>
      </div>



      {/* Betting Lines - shown whenever available */}
      {betting_lines?.length > 0 && (
        <div className="border border-white/10 rounded-xl p-4 bg-white/5">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
              <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
              <div className="text-sm mt-1"><span className="text-earl-400">{game.away_team}</span> {formatOdds(betting_lines[0]?.away_moneyline)}<span className="text-gray-600 mx-2">|</span><span className="text-gray-400">{game.home_team}</span> {formatOdds(betting_lines[0]?.home_moneyline)}</div>
            </div>
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
              <div className="text-[10px] text-gray-500 uppercase">Run Line</div>
              <div className="text-sm mt-1">
                {betting_lines[0]?.spread != null ? (
                  <>
                    <span className="text-earl-400">{game.away_team}</span> {(betting_lines[0].spread * -1) > 0 ? "+"+(betting_lines[0].spread * -1) : betting_lines[0].spread * -1}
                    <span className="text-gray-500 text-xs ml-1">({formatOdds(betting_lines[0]?.spread_away_odds ?? -110)})</span>
                    <span className="text-gray-600 mx-1">|</span>
                    <span className="text-gray-400">{game.home_team}</span> {betting_lines[0].spread > 0 ? "+"+betting_lines[0].spread : betting_lines[0].spread}
                    <span className="text-gray-500 text-xs ml-1">({formatOdds(betting_lines[0]?.spread_home_odds ?? -110)})</span>
                  </>
                ) : "-"}
              </div>
            </div>
            <div className="text-center p-3 rounded-lg bg-white/[0.03]">
              <div className="text-[10px] text-gray-500 uppercase">Over/Under</div>
              <div className="text-sm mt-1 font-semibold">
                {betting_lines[0]?.over_under != null ? (
                  <>
                    O/U {betting_lines[0].over_under}
                    <span className="text-gray-500 text-xs ml-2 font-normal">Over {formatOdds(betting_lines[0]?.over_odds ?? -110)}</span>
                    <span className="text-gray-500 text-xs ml-2 font-normal">Under {formatOdds(betting_lines[0]?.under_odds ?? -110)}</span>
                  </>
                ) : "-"}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Lineups - warm-up / pregame only, hide once game is live */}
      {isUpcoming && lineups && (lineups.home?.length > 0 || lineups.away?.length > 0) && (
        <div className="border border-white/10 rounded-xl p-4 bg-gradient-to-r from-blue-900/20 to-transparent">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Starting Lineups</div>
          <div className="grid grid-cols-2 gap-4">
            {(["away", "home"] as const).map(side => {
              const pitcher = (lineups[side] || []).find((l: any) => l.order === 0);
              const batters = (lineups[side] || []).filter((l: any) => l.order >= 1 && l.order <= 9);
              return (
                <div key={side}>
                  <h4 className={`text-sm font-semibold ${side === "away" ? "text-blue-300" : "text-red-300"} mb-2`}>{game[side === "away" ? "away_team" : "home_team"]}</h4>
                  {pitcher && (
                    <div className="text-xs text-gray-300 font-medium mb-2 pb-2 border-b border-white/10">
                      <span className="text-earl-400 text-[10px] uppercase font-semibold mr-2">SP</span>
                      {pitcher.name}
                      {pitcher.stats?.era != null && <span className="text-gray-500 ml-2">ERA {pitcher.stats.era}</span>}
                    </div>
                  )}
                  <ol className="text-xs space-y-1">
                    {batters.map((l: any, i: number) => (
                      <li key={i} className="flex gap-2 text-gray-300">
                        <span className="text-gray-500 w-4 shrink-0">{l.order}.</span>
                        <span className="font-medium">{l.name}</span>
                        <span className="text-gray-500">{l.position}</span>
                        {l.stats?.avg && <span className="text-gray-400 ml-auto">{l.stats.avg}</span>}
                      </li>
                    ))}
                  </ol>
                </div>
              );
            })}
          </div>
        </div>
      )}

        {/* Under-lineups game tabs: Box Score, Game Summary, Earl's Picks, Detailed Analysis, Detailed Stats */}
        <MLBGameTabs
          gameId={game.id}
          pickCard={pick_card}
          game={game}
          formatOdds={formatOdds}
          boxscore={boxscore}
          linescore={linescore}
        />

      <div className="text-center"><Link href="/mlb/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">← Back to Schedule</Link></div>
    </div>
  );
}
