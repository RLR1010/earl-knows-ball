"use client";
import { useCallback, useEffect, useState } from "react";
import { useParams, useSearchParams } from "next/navigation";
import Link from "next/link";
import { useSeo } from "@/components/Seo";
import NBAGameTabs from "@/components/NBAGameTabs";
import MLBGameTabs from "@/components/MLBGameTabs";
import NFLGameTabs, { BettingLinesCard } from "@/components/NFLGameTabs";
import EarlsPicksPanel from "@/components/EarlsPicksPanel";


// ── Shared Types ─────────────────────────────────────────────

interface GameInfo {
  id: number; week: number; game_type: string; status: string; date: string;
  venue: string | null; roof_type: string | null;
  home_team: string; away_team: string;
  home_score: number | null; away_score: number | null;
  quarter: number | null; clock: string | null;
  temperature: number | null; wind_speed: number | null; weather_condition: string | null;
  spread?: number | null; over_under?: number | null;
}

interface BoxScoreStats {
  total_yards: number | null; pass_yards: number | null;
  rush_yards: number | null; turnovers: number | null;
  first_downs: number | null; third_down_pct: number | null;
  time_of_possession: string | null;
  penalties: number | null; penalty_yards: number | null;
  top_players: any[];
}

interface NFLBoxScore {
  game: GameInfo;
  home_stats: BoxScoreStats | null;
  away_stats: BoxScoreStats | null;
  betting_lines?: Array<{ spread: number | null; over_under: number | null; home_team?: string; away_team?: string; home_ml?: number | null; away_ml?: number | null }> | null;
  home_record?: { wins: number; losses: number } | null;
  away_record?: { wins: number; losses: number } | null;
}

interface GamePrediction {
  game_id: number; season: number; week: number;
  home_team: string; away_team: string; date: string | null;
  predicted: { home_score: number; away_score: number; total: number; margin: number };
  actual: { home_score: number; away_score: number; total: number; margin: number };
  results: { ats: string; ou: string; ml: string };
  expected_value?: { ats?: number | null; ou?: number | null; ml?: number | null };
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
  const ev = pred?.expected_value || {};
  const noPrediction = !results.ats || results.ats === "N/A";
  // A completed game is one where any pick has a Win/Loss/Push result.
  const isCompleted = !!(results.ats || results.ou || results.ml);

  // Normalize result casing + color: Win=green, Loss=red, Push=grey.
  const resultColor = (r?: string | null) => {
    const norm = r ? r.charAt(0).toUpperCase() + r.slice(1).toLowerCase() : null;
    if (norm === "Win") return "text-green-400";
    if (norm === "Loss") return "text-red-400";
    return "text-gray-400"; // Push (or unknown)
  };
  const normalize = (r?: string | null) =>
    r ? r.charAt(0).toUpperCase() + r.slice(1).toLowerCase() : "-";
  // EV string: show sign, no cents marker.
  const evStr = (v?: number | null) =>
    v == null ? null : (v >= 0 ? "EV: +" : "EV: ") + v.toFixed(1);
  const evColor = (v?: number | null) =>
    v == null ? "text-gray-500" : v >= 0 ? "text-green-400" : "text-red-400";
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
            {isCompleted && results.ats ? (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ats)}`}>{normalize(results.ats)}</div>
                {evStr(ev.ats) != null && (
                  <div className={`text-xs font-semibold mt-1 ${evColor(ev.ats)}`}>{evStr(ev.ats)}</div>
                )}
              </>
            ) : (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ats)}`}>{normalize(results.ats)}</div>
                <ConfidenceBar score={conf?.ats ?? conf?.overall} />
              </>
            )}
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">O/U</div>
            {isCompleted && results.ou ? (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ou)}`}>{normalize(results.ou)}</div>
                {evStr(ev.ou) != null && (
                  <div className={`text-xs font-semibold mt-1 ${evColor(ev.ou)}`}>{evStr(ev.ou)}</div>
                )}
              </>
            ) : (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ou)}`}>{normalize(results.ou)}</div>
                <ConfidenceBar score={conf?.ou ?? conf?.overall} />
              </>
            )}
          </div>
          <div className="text-center p-3 rounded-lg bg-white/[0.03]">
            <div className="text-[10px] text-gray-500 uppercase">ML</div>
            {isCompleted && results.ml ? (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ml)}`}>{normalize(results.ml)}</div>
                {evStr(ev.ml) != null && (
                  <div className={`text-xs font-semibold mt-1 ${evColor(ev.ml)}`}>{evStr(ev.ml)}</div>
                )}
              </>
            ) : (
              <>
                <div className={`text-lg font-bold mt-1 ${resultColor(results.ml)}`}>{normalize(results.ml)}</div>
                <ConfidenceBar score={conf?.ml ?? conf?.overall} />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── NFL Box Score ──
// ── Weather icon helper ────────────────────────────────
function WeatherIcon({ condition }: { condition?: string | null }) {
  const c = (condition || "").toLowerCase();
  let icon = "☀️";
  if (c.includes("rain") || c.includes("drizzle") || c.includes("shower")) icon = "🌧️";
  else if (c.includes("snow") || c.includes("sleet")) icon = "❄️";
  else if (c.includes("thunder") || c.includes("storm")) icon = "⛈️";
  else if (c.includes("fog")) icon = "🌫️";
  else if (c.includes("cloud") || c.includes("overcast") || c.includes("mist")) icon = "☁️";
  else if (c.includes("part") || c.includes("mix")) icon = "🌤️";
  else if (c.includes("clear") || c.includes("sunny") || c.includes("fair")) icon = "☀️";
  return <span aria-hidden="true">{icon}</span>;
}

function NFLBoxScore({ data }: { data: NFLBoxScore }) {
  const { game, home_stats, away_stats } = data;
  const isLive = game.status?.toLowerCase() === "in_progress";
  const isFinal = game.status?.toLowerCase() === "final";
  function nflStatusBadge(s?: string) {
    switch (s?.toLowerCase()) {
      case "in_progress": return { label: "LIVE", cls: "text-red-400 animate-pulse" };
      case "final": return { label: "FINAL", cls: "text-green-400" };
      default: return { label: (s || "SCHEDULED").toUpperCase(), cls: "text-earl-400" };
    }
  }
  const badge = nflStatusBadge(game.status);
  function formatDate(iso: string) { const d = new Date(iso); return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/New_York" }); }
  function quarterOrdinal(n?: number | null) {
    if (n == null) return "";
    const s = ["", "1st", "2nd", "3rd", "4th", "OT", "2OT", "3OT"][n] || `${n}Q`;
    return s;
  }
  const hWon = isFinal && (game.home_score ?? 0) > (game.away_score ?? 0);
  const aWon = isFinal && (game.away_score ?? 0) > (game.home_score ?? 0);
  const roofType = (game.roof_type || "").toLowerCase();
  // Weather is only shown for open-air / open-roof stadiums (indoor domes don't have weather).
  const isOutdoor = !roofType || roofType === "outdoor" || roofType === "open";

  return (
    <div className="space-y-6">
      <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 text-center">
        <span className={`text-sm font-bold ${badge.cls}`}>{badge.label}</span>
        {isLive && game.quarter != null && (
          <span className="text-sm font-semibold text-white ml-3">
            {quarterOrdinal(game.quarter)}
            {game.clock && <span className="text-gray-400 ml-1">· {game.clock}</span>}
          </span>
        )}
        {game.date && <span className="text-xs text-gray-500 ml-3">{formatDate(game.date)}</span>}
        <div className="flex items-center justify-center gap-8 md:gap-16 mt-4">
          <div className="text-right">
            <div className="text-lg font-semibold text-gray-300">{game.away_team}</div>
            <div className={`text-5xl font-bold mt-1 ${aWon ? "text-earl-400" : "text-gray-400"}`}>
              {(isFinal || isLive) && game.away_score != null ? game.away_score : "-"}
            </div>
          </div>
          <div className="text-3xl text-gray-600 font-bold">@</div>
          <div className="text-left">
            <div className="text-lg font-semibold text-gray-300">{game.home_team}</div>
            <div className={`text-5xl font-bold mt-1 ${hWon ? "text-earl-400" : "text-gray-400"}`}>
              {(isFinal || isLive) && game.home_score != null ? game.home_score : "-"}
            </div>
          </div>
        </div>
        <div className="flex items-center justify-center flex-wrap gap-x-3 gap-y-1 text-sm text-gray-500 mt-4">
          {game.venue && <span className="font-medium text-gray-400">{game.venue}</span>}
          {isOutdoor && (
            <span className="inline-flex items-center gap-1">
              <WeatherIcon condition={game.weather_condition} />
              {game.temperature != null && <span>{Math.round(game.temperature)}°F</span>}
              {game.weather_condition && <span>{game.weather_condition}</span>}
              {game.wind_speed != null && (
                <span className="text-gray-500">Wind {game.wind_speed} mph</span>
              )}
            </span>
          )}
        </div>
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

  // NBA data
  const [nbaPrediction, setNbaPrediction] = useState<GamePrediction | null>(null);
  const [nbaGameLine, setNbaGameLine] = useState<{ spread: number | null; over_under: number | null } | null>(null);

  // SEO: build a dynamic title from whichever team data is available.
  const matchup =
    (prediction?.away_team && prediction.home_team)
      ? `${prediction.away_team} @ ${prediction.home_team}`
      : (nbaPrediction?.away_team && nbaPrediction.home_team)
        ? `${nbaPrediction.away_team} @ ${nbaPrediction.home_team}`
        : null;
  useSeo(
    matchup
      ? {
          title: `${matchup} Picks, Odds & Prediction — ${sport.toUpperCase()} | Earl Knows Ball`,
          description: `AI handicapping for ${matchup}: spread, over/under, and moneyline picks with probabilities, betting trends, and key matchups for the ${sport.toUpperCase()} game.`,
          keywords: `${sport} picks, ${matchup}, ${sport} betting odds, ${sport} spread, ${sport} prediction, ${sport} over under, AI handicapper`,
        }
      : {
          title: `${sport.toUpperCase()} Game Picks & Prediction — Earl Knows Ball`,
          description: `AI handicapping and betting picks for the ${sport.toUpperCase()} game with probabilities, lines, and key matchups.`,
          keywords: `${sport} picks, ${sport} prediction, ${sport} betting odds, AI handicapper`,
        },
  );

  // NFL data fetching (re-fetched on an interval while the game is live)
  // so the scoreboard auto-updates during a game — the same "live" feel as
  // MLB, but it polls our own /box-score endpoint (which reads the DB that
  // the nfl/live-refresh task keeps synced from ESPN), not ESPN directly.
  useEffect(() => {
    if (!gameId || !isNfl) { if (!isNfl) return; }
    const gid = parseInt(gameId);

    const fetchNfl = (): Promise<string> => {
      return Promise.all([
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
        return box?.game?.status ?? "";
      }).catch(() => { setLoading(false); return ""; });
    };

    const status = fetchNfl();
    // Keep polling the box-score while the game is in progress. Stop once
    // it goes final (or if it was never live) to avoid pointless requests.
    let timer: ReturnType<typeof setInterval> | null = null;
    status.then((s: string) => {
      const sl = (s || "").toLowerCase();
      if (sl === "in_progress" || sl === "live") {
        timer = setInterval(() => {
          fetch(`/api/games/${gid}/box-score`).then(r => r.json()).then((box) => {
            if (box?.game) {
              setNflBoxScore(box as NFLBoxScore);
              const st = (box.game.status || "").toLowerCase();
              if (st === "final" || st === "scheduled") {
                if (timer) { clearInterval(timer); timer = null; }
              }
            }
          }).catch(() => null);
        }, 30000); // refresh every 30s while live
      }
    });

    return () => { if (timer) clearInterval(timer); };
  }, [gameId, isNfl]);

  // NBA data fetching
  useEffect(() => {
    if (!gameId || sport !== "nba") return;
    const gid = parseInt(gameId);
    Promise.all([
      fetch(`/api/handicapping/nba/predictions/${gid}`).then(r => r.json()).catch(() => null),
      fetch(`/api/nba/games/${gid}`).then(r => r.json()).catch(() => null),
    ]).then(([pred, game]) => {
      if (pred?.game_id) setNbaPrediction(pred as GamePrediction);
      if (game?.spread != null) setNbaGameLine({ spread: game.spread, over_under: game.over_under });
      else if (pred?.line?.spread != null) setNbaGameLine(pred.line);
    });
  }, [gameId, sport]);

  // MLB: show classic boxscore page
  if (sport === "mlb") {
    return <MLBClassicPage gameId={gameId} backHref={backHref} />;
  }

  if (sport === "nba") {
    const nbaPredForTabs = nbaPrediction || (nbaGameLine ? ({
      game_id: parseInt(gameId || "0"),
      season: 0, week: 0,
      home_team: "",
      away_team: "",
      date: null,
      predicted: { home_score: 0, away_score: 0, total: 0, margin: 0 },
      actual: { home_score: 0, away_score: 0, total: 0, margin: 0 },
      results: { ats: "N/A", ou: "N/A", ml: "N/A" },
      confidence: { overall: null, ats: null, ou: null, ml: null },
      line: nbaGameLine,
    }) as unknown as GamePrediction : null);
    return (
      <div>
        <NBAGameTabs gameId={parseInt(gameId || "0")} prediction={nbaPredForTabs} />
        <div className="text-center pt-4">
          <Link href={backHref} className="text-sm text-earl-400 hover:text-earl-300 transition">← Back to Schedule</Link>
        </div>
      </div>
    );
  }

  if (loading) return <div className="text-center py-12 text-gray-500">Loading...</div>;

  if (error || (!nflBoxScore && !prediction)) {
    return <div className="text-center py-12"><div className="text-gray-500 mb-4">{error || "Game not found."}</div>
      <Link href={backHref} className="text-sm text-earl-400 hover:text-earl-300">← Back to Schedule</Link></div>;
  }

  // Build combined prediction (use real pred or line-only fallback)
  const predForTabs = prediction || (gameLine ? ({
    game_id: parseInt(gameId || "0"),
    season: 0, week: nflBoxScore?.game?.week || 0,
    home_team: nflBoxScore?.game?.home_team || "",
    away_team: nflBoxScore?.game?.away_team || "",
    date: nflBoxScore?.game?.date || null,
    predicted: { home_score: 0, away_score: 0, total: 0, margin: 0 },
    actual: { home_score: 0, away_score: 0, total: 0, margin: 0 },
    results: { ats: "N/A", ou: "N/A", ml: "N/A" },
    confidence: { overall: null, ats: null, ou: null, ml: null },
    line: gameLine,
  }) as unknown as GamePrediction : null);

  const nflGameStatus = nflBoxScore?.game?.status?.toLowerCase() || "";
  const isNflLive = nflGameStatus === "in_progress";
  const isNflFinal = nflGameStatus === "final";
  function nflBadgeLabel(s?: string) {
    switch (s?.toLowerCase()) {
      case "in_progress": return "LIVE";
      case "final": return "FINAL";
      default: return (s || "SCHEDULED").toUpperCase();
    }
  }
  function nflBadgeCls(s?: string) {
    switch (s?.toLowerCase()) {
      case "in_progress": return "text-red-400 animate-pulse";
      case "final": return "text-green-400";
      default: return "text-earl-400";
    }
  }
  const nflBadge = { label: nflBadgeLabel(nflBoxScore?.game?.status), cls: nflBadgeCls(nflBoxScore?.game?.status) };
  function nflQuarter(n?: number | null) {
    if (n == null) return "";
    return ["", "1st", "2nd", "3rd", "4th", "OT", "2OT", "3OT"][n] || `${n}Q`;
  }
  function nflFmtDate(iso?: string | null) { if (!iso) return ""; return new Date(iso).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/New_York" }); }
  const hWon = isNflFinal && (nflBoxScore?.game?.home_score ?? 0) > (nflBoxScore?.game?.away_score ?? 0);
  const aWon = isNflFinal && (nflBoxScore?.game?.away_score ?? 0) > (nflBoxScore?.game?.home_score ?? 0);

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* Score Card */}
      {nflBoxScore && (
        <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 text-center">
          <span className={`text-sm font-bold ${nflBadge.cls}`}>{nflBadge.label}</span>
          {isNflLive && nflBoxScore.game.quarter != null && (
            <span className="text-sm font-semibold text-white ml-3">
              {nflQuarter(nflBoxScore.game.quarter)}
              {nflBoxScore.game.clock && <span className="text-gray-400 ml-1">· {nflBoxScore.game.clock}</span>}
            </span>
          )}
          {nflBoxScore.game.date && <span className="text-xs text-gray-500 ml-3">{nflFmtDate(nflBoxScore.game.date)}</span>}
          <div className="flex items-center justify-center gap-8 md:gap-16 mt-4">
            <div className="flex flex-col items-center gap-1">
              <div className={`text-2xl font-bold ${aWon ? "opacity-100 text-gray-300" : "opacity-60 text-gray-400"}`}>{nflBoxScore.game.away_team?.slice(0, 3).toUpperCase()}</div>
              {nflBoxScore.away_record != null && (
                <div className="text-xs text-gray-400">{nflBoxScore.away_record.wins}-{nflBoxScore.away_record.losses}</div>
              )}
              <span className={`text-5xl font-bold mt-1 ${aWon ? "text-earl-400" : "text-gray-400"}`}>
                {nflBoxScore.game.away_score != null ? nflBoxScore.game.away_score : "-"}
              </span>
            </div>
            <div className="text-4xl text-gray-600 font-black">@</div>
            <div className="flex flex-col items-center gap-1">
              <div className={`text-2xl font-bold ${hWon ? "opacity-100 text-white" : "opacity-60 text-gray-400"}`}>{nflBoxScore.game.home_team?.slice(0, 3).toUpperCase()}</div>
              {nflBoxScore.home_record != null && (
                <div className="text-xs text-gray-400">{nflBoxScore.home_record.wins}-{nflBoxScore.home_record.losses}</div>
              )}
              <span className={`text-5xl font-bold mt-1 ${hWon ? "text-earl-400" : "text-gray-400"}`}>
                {nflBoxScore.game.home_score != null ? nflBoxScore.game.home_score : "-"}
              </span>
            </div>
          </div>
          {(() => {
            if (!nflBoxScore?.game) return null;
            const v = nflBoxScore.game;
            const rt = (v.roof_type || "").toLowerCase();
            const outdoor = !rt || rt === "outdoor" || rt === "open";
            return (
              <div className="flex items-center justify-center flex-wrap gap-x-3 gap-y-1 text-sm text-gray-500 mt-4">
                {v.venue && <span className="font-medium text-gray-400">{v.venue}</span>}
                {outdoor && (
                  <span className="inline-flex items-center gap-1">
                    <WeatherIcon condition={v.weather_condition} />
                    {v.temperature != null && <span>{Math.round(v.temperature)}°F</span>}
                    {v.weather_condition && <span>{v.weather_condition}</span>}
                    {v.wind_speed != null && <span className="text-gray-500">Wind {v.wind_speed} mph</span>}
                  </span>
                )}
              </div>
            );
          })()}
        </div>
      )}

      {/* Betting Lines Card — from boxscore endpoint's betting_lines (same pattern as MLB) */}
      {(() => {
        const bl = nflBoxScore?.betting_lines?.[0];
        const spread = bl?.spread ?? null;
        const over_under = bl?.over_under ?? null;
        const homeML = bl?.home_ml ?? null;
        const awayML = bl?.away_ml ?? null;
        if (spread == null && over_under == null && homeML == null && awayML == null) return null;
        return (
          <BettingLinesCard
            homeTeam={nflBoxScore?.game?.home_team || ""}
            awayTeam={nflBoxScore?.game?.away_team || ""}
            spread={spread}
            over_under={over_under}
            homeML={homeML}
            awayML={awayML}
            prediction={predForTabs}
          />
        );
      })()}

      {/* NFL Game Tabs */}
      {nflBoxScore && (
        <NFLGameTabs
          gameId={gameId || ""}
          boxscore={nflBoxScore}
          prediction={predForTabs}
          isFinal={isNflFinal}
        />
      )}

      {/* Back link */}
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
  away_record?: string | null;
  home_record?: string | null;
  lineups: { home: {order:number;name:string;position:string;stats?:{avg?:string;era?:string;ops?:string}}[]; away: {order:number;name:string;position:string;stats?:{avg?:string;era?:string;ops?:string}}[] } | null;
}

function MLBClassicPage({ gameId, backHref }: { gameId: string | undefined; backHref: string }) {
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

  const { game, boxscore, linescore, betting_lines, pick_card, splits, lineups, away_record, home_record } = data;
  const isUpcoming = game?.status?.toLowerCase() === "scheduled" || game?.status?.toLowerCase() === "pregame";
  const isLive = game?.status?.toLowerCase() === "in_progress";
  const isFinal = game?.status?.toLowerCase() === "final";
  const awaySide = boxscore?.teams?.away;
  const homeSide = boxscore?.teams?.home;

  function formatDate(iso: string) { const d = new Date(iso); return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric", year: "numeric", timeZone: "America/New_York" }); }
  function formatTime(iso: string) { const d = new Date(iso); return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET"; }
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
    <div className="max-w-4xl mx-auto space-y-6">
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
            {away_record && (
              <div className="text-xs font-medium text-gray-500 mt-0.5">{away_record}</div>
            )}
            <div className={`text-5xl font-bold mt-1 ${aWon ? "text-earl-400" : "text-gray-400"}`}>
              {game.away_score != null ? game.away_score : "-"}
            </div>
          </div>
          <div className="text-3xl text-gray-600 font-bold">@</div>
          <div className="text-left">
            <div className="text-lg font-semibold text-gray-300">{game.home_team}</div>
            {home_record && (
              <div className="text-xs font-medium text-gray-500 mt-0.5">{home_record}</div>
            )}
            <div className={`text-5xl font-bold mt-1 ${hWon ? "text-earl-400" : "text-gray-400"}`}>
              {game.home_score != null ? game.home_score : "-"}
            </div>
          </div>
        </div>
        <div className="flex items-center justify-center flex-wrap gap-x-3 gap-y-1 text-sm text-gray-500 mt-3">
          {game.venue && <span className="font-medium text-gray-400">{game.venue}</span>}
          {(() => {
            const rt = (game.roof_type || "").toLowerCase();
            const outdoor = !rt || rt === "outdoor" || rt === "open";
            return outdoor ? (
              <span className="inline-flex items-center gap-1">
                <WeatherIcon condition={game.weather_condition} />
                {game.temperature != null && <span>{Math.round(game.temperature)}°F</span>}
                {game.weather_condition && <span>{game.weather_condition}</span>}
                {game.wind_speed != null && <span className="text-gray-500">Wind {game.wind_speed} mph</span>}
              </span>
            ) : null;
          })()}
          {game.attendance && <span> · Att: {game.attendance.toLocaleString()}</span>}
          {game.duration_minutes && <span> · {Math.floor(game.duration_minutes / 60)}:{String(game.duration_minutes % 60).padStart(2, "0")}</span>}
        </div>
      </div>



      {/* Betting Lines - shown whenever available */}
      {betting_lines?.length > 0 && (
        <div className="rounded-xl p-4 border border-white/10 bg-gradient-to-r from-white/5 to-white/0">
          <div className="text-xs text-gray-500 uppercase tracking-wider mb-3">Betting Lines</div>
          <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-white/10">
            <div className="text-center py-3 md:px-3">
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
            <div className="text-center py-3 md:px-3">
              <div className="text-[10px] text-gray-500 uppercase">Moneyline</div>
              <div className="text-sm mt-1"><span className="text-earl-400">{game.away_team}</span> {formatOdds(betting_lines[0]?.away_moneyline)}<span className="text-gray-600 mx-2">|</span><span className="text-gray-400">{game.home_team}</span> {formatOdds(betting_lines[0]?.home_moneyline)}</div>
            </div>
            <div className="text-center py-3 md:px-3">
              <div className="text-[10px] text-gray-500 uppercase">Over/Under</div>
              <div className="text-sm mt-1 font-semibold">
                {betting_lines[0]?.over_under != null ? (
                  <>
                    O/U {betting_lines[0].over_under}
                    <span className="text-gray-500 text-xs ml-2 font-normal">Over {formatOdds(betting_lines[0]?.over_odds ?? -110)}</span>
                    <span className="text-gray-500 text-xs ml-1 font-normal">| Under {formatOdds(betting_lines[0]?.under_odds ?? -110)}</span>
                  </>
                ) : "-"}
              </div>
            </div>
          </div>

          {pick_card && (
            <EarlsPicksPanel
              title="Earl's Picks"
              predicted={
                pick_card.predictions?.home_runs != null
                  ? {
                      awayLabel: game.away_team,
                      homeLabel: game.home_team,
                      awayScore: pick_card.predictions.away_runs,
                      homeScore: pick_card.predictions.home_runs,
                      total: pick_card.predictions.total,
                      margin: pick_card.predictions.margin,
                    }
                  : null
              }
              items={[
                {
                  label: "Run Line",
                  pick: pick_card.picks?.run_line && pick_card.picks.run_line !== "-" ? pick_card.picks.run_line.toUpperCase() : "—",
                  ev: pick_card.expected_value?.rl ?? null,
                  line: pick_card.lines?.run_line != null ? `Run Line ${pick_card.lines.run_line}` : null,
                  result: pick_card.results?.run_line || null,
                  pickColor: "text-amber-400",
                },
                {
                  label: "Over/Under",
                  pick: pick_card.picks?.over_under && pick_card.picks.over_under !== "-" ? pick_card.picks.over_under.toUpperCase() : "—",
                  ev: pick_card.expected_value?.ou ?? null,
                  line: pick_card.lines?.over_under != null ? `O/U ${pick_card.lines.over_under}` : null,
                  result: pick_card.results?.over_under || null,
                  pickColor: "text-yellow-400",
                },
                {
                  label: "Moneyline",
                  pick: pick_card.picks?.moneyline && pick_card.picks.moneyline !== "-" ? pick_card.picks.moneyline.toUpperCase() : "—",
                  ev: pick_card.expected_value?.ml ?? null,
                  line:
                    pick_card.lines?.home_moneyline != null
                      ? `ML ${formatOdds(pick_card.lines.away_moneyline)} | ${formatOdds(pick_card.lines.home_moneyline)}`
                      : null,
                  result: pick_card.results?.moneyline || null,
                  pickColor: "text-cyan-400",
                },
              ]}
            />
          )}
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
                        {l.stats?.avg != null && <span className="text-gray-400 ml-auto">.{(l.stats.avg as number).toFixed(3).slice(1)}</span>}
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
          game={game}
          formatOdds={formatOdds}
          boxscore={boxscore}
          linescore={linescore}
        />

      <div className="text-center"><Link href={backHref} className="text-sm text-earl-400 hover:text-earl-300 transition">← Back to Schedule</Link></div>
    </div>
  );
}
