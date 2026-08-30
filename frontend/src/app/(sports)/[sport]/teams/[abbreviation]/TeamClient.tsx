"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as React from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import GameCalendar from "@/components/GameCalendar";
import Link from "next/link";
import { api, Team, Game, DepthChartEntry, BoxScore } from "@/lib/api";
import TeamLogo from "@/components/TeamLogo";
import { useSeo } from "@/components/Seo";
import SchedulePicksFooter from "@/components/SchedulePicksFooter";

// ── Team metadata ─────────────────────────────────────────────────────
const NFL_TEAMS: Record<string, { name: string; conf: string; div: string }> = {
  ari: { name: "Cardinals", conf: "NFC", div: "West" },
  atl: { name: "Falcons", conf: "NFC", div: "South" },
  bal: { name: "Ravens", conf: "AFC", div: "North" },
  buf: { name: "Bills", conf: "AFC", div: "East" },
  car: { name: "Panthers", conf: "NFC", div: "South" },
  chi: { name: "Bears", conf: "NFC", div: "North" },
  cin: { name: "Bengals", conf: "AFC", div: "North" },
  cle: { name: "Browns", conf: "AFC", div: "North" },
  dal: { name: "Cowboys", conf: "NFC", div: "East" },
  den: { name: "Broncos", conf: "AFC", div: "West" },
  det: { name: "Lions", conf: "NFC", div: "North" },
  gb:  { name: "Packers", conf: "NFC", div: "North" },
  hou: { name: "Texans", conf: "AFC", div: "South" },
  ind: { name: "Colts", conf: "AFC", div: "South" },
  jax: { name: "Jaguars", conf: "AFC", div: "South" },
  kc:  { name: "Chiefs", conf: "AFC", div: "West" },
  lac: { name: "Chargers", conf: "AFC", div: "West" },
  lar: { name: "Rams", conf: "NFC", div: "West" },
  lv:  { name: "Raiders", conf: "AFC", div: "West" },
  mia: { name: "Dolphins", conf: "AFC", div: "East" },
  min: { name: "Vikings", conf: "NFC", div: "North" },
  ne:  { name: "Patriots", conf: "AFC", div: "East" },
  no:  { name: "Saints", conf: "NFC", div: "South" },
  nyg: { name: "Giants", conf: "NFC", div: "East" },
  nyj: { name: "Jets", conf: "AFC", div: "East" },
  phi: { name: "Eagles", conf: "NFC", div: "East" },
  pit: { name: "Steelers", conf: "AFC", div: "North" },
  sea: { name: "Seahawks", conf: "NFC", div: "West" },
  sf:  { name: "49ers", conf: "NFC", div: "West" },
  tb:  { name: "Buccaneers", conf: "NFC", div: "South" },
  ten: { name: "Titans", conf: "AFC", div: "South" },
  was: { name: "Commanders", conf: "NFC", div: "East" },
};

// Shared team metadata maps
const getTeamsForSport = (sport: string): Record<string, { name: string; conf: string; div: string }> => {
  if (sport === "nba") {
    return {
      atl: { name: "Hawks", conf: "Eastern", div: "Southeast" },
      bos: { name: "Celtics", conf: "Eastern", div: "Atlantic" },
      bkn: { name: "Nets", conf: "Eastern", div: "Atlantic" },
      cha: { name: "Hornets", conf: "Eastern", div: "Southeast" },
      chi: { name: "Bulls", conf: "Eastern", div: "Central" },
      cle: { name: "Cavaliers", conf: "Eastern", div: "Central" },
      dal: { name: "Mavericks", conf: "Western", div: "Southwest" },
      den: { name: "Nuggets", conf: "Western", div: "Northwest" },
      det: { name: "Pistons", conf: "Eastern", div: "Central" },
      gsw: { name: "Warriors", conf: "Western", div: "Pacific" },
      hou: { name: "Rockets", conf: "Western", div: "Southwest" },
      ind: { name: "Pacers", conf: "Eastern", div: "Central" },
      lac: { name: "Clippers", conf: "Western", div: "Pacific" },
      lal: { name: "Lakers", conf: "Western", div: "Pacific" },
      mem: { name: "Grizzlies", conf: "Western", div: "Southwest" },
      mia: { name: "Heat", conf: "Eastern", div: "Southeast" },
      mil: { name: "Bucks", conf: "Eastern", div: "Central" },
      min: { name: "Timberwolves", conf: "Western", div: "Northwest" },
      nop: { name: "Pelicans", conf: "Western", div: "Southwest" },
      nyk: { name: "Knicks", conf: "Eastern", div: "Atlantic" },
      okc: { name: "Thunder", conf: "Western", div: "Northwest" },
      orl: { name: "Magic", conf: "Eastern", div: "Southeast" },
      phi: { name: "76ers", conf: "Eastern", div: "Atlantic" },
      phx: { name: "Suns", conf: "Western", div: "Pacific" },
      por: { name: "Trail Blazers", conf: "Western", div: "Northwest" },
      sac: { name: "Kings", conf: "Western", div: "Pacific" },
      sas: { name: "Spurs", conf: "Western", div: "Southwest" },
      tor: { name: "Raptors", conf: "Eastern", div: "Atlantic" },
      uta: { name: "Jazz", conf: "Western", div: "Northwest" },
      was: { name: "Wizards", conf: "Eastern", div: "Southeast" },
    };
  }
  if (sport === "mlb") {
    return {
      ari: { name: "Diamondbacks", conf: "NL", div: "West" },
      atl: { name: "Braves", conf: "NL", div: "East" },
      bal: { name: "Orioles", conf: "AL", div: "East" },
      bos: { name: "Red Sox", conf: "AL", div: "East" },
      chc: { name: "Cubs", conf: "NL", div: "Central" },
      cin: { name: "Reds", conf: "NL", div: "Central" },
      cle: { name: "Guardians", conf: "AL", div: "Central" },
      col: { name: "Rockies", conf: "NL", div: "West" },
      cws: { name: "White Sox", conf: "AL", div: "Central" },
      det: { name: "Tigers", conf: "AL", div: "Central" },
      hou: { name: "Astros", conf: "AL", div: "West" },
      kc: { name: "Royals", conf: "AL", div: "Central" },
      laa: { name: "Angels", conf: "AL", div: "West" },
      lad: { name: "Dodgers", conf: "NL", div: "West" },
      mia: { name: "Marlins", conf: "NL", div: "East" },
      mil: { name: "Brewers", conf: "NL", div: "Central" },
      min: { name: "Twins", conf: "AL", div: "Central" },
      nym: { name: "Mets", conf: "NL", div: "East" },
      nyy: { name: "Yankees", conf: "AL", div: "East" },
      oak: { name: "Athletics", conf: "AL", div: "West" },
      phi: { name: "Phillies", conf: "NL", div: "East" },
      pit: { name: "Pirates", conf: "NL", div: "Central" },
      sd: { name: "Padres", conf: "NL", div: "West" },
      sea: { name: "Mariners", conf: "AL", div: "West" },
      sf: { name: "Giants", conf: "NL", div: "West" },
      stl: { name: "Cardinals", conf: "NL", div: "Central" },
      tb: { name: "Rays", conf: "AL", div: "East" },
      tex: { name: "Rangers", conf: "AL", div: "West" },
      tor: { name: "Blue Jays", conf: "AL", div: "East" },
      was: { name: "Nationals", conf: "NL", div: "East" },
    };
  }
  return NFL_TEAMS;
};

function getTeamColor(abbr: string): string {
  const colors: Record<string, string> = {
    ari: "#97233F", atl: "#A71930", bal: "#241773", buf: "#00338D",
    car: "#0085CA", chi: "#0B162A", cin: "#FB4F14", cle: "#311D00",
    dal: "#003594", den: "#002244", det: "#0076B6", gb: "#203731",
    hou: "#03202F", ind: "#002C5F", jax: "#006778", kc: "#E31837",
    lac: "#0080C6", lar: "#003594", lv: "#000000", mia: "#008E97",
    min: "#4F2683", ne: "#002244", no: "#D3BC8D", nyg: "#0B2265",
    nyj: "#125740", phi: "#004C54", pit: "#FFB612", sea: "#002244",
    sf: "#AA0000", tb: "#D50A0A", ten: "#0C2340", was: "#5A1414",
    // NBA
    bos: "#007A33", gsw: "#FFC72C", lal: "#552583", nop: "#0C2340",
    nyk: "#F58426", okc: "#007AC1", phx: "#E56020", sac: "#5A2D82",
    sas: "#C4CED4", tor: "#CE1141", uta: "#002B5C",
    // MLB
    chc: "#0E3386", cws: "#27251F", laa: "#BA0021", lad: "#005A9C",
    nym: "#FF5910", nyy: "#003087", oak: "#003831", sd: "#2F241D",
    stl: "#C41E3A", tex: "#C0111F",
  };
  return colors[abbr.toLowerCase()] || "#666";
}

// ── Helpers ──────────────────────────────────────────────────────────
function formatGameDate(iso: string) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
    timeZone: "America/New_York",
  });
}

function formatGameTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric", minute: "2-digit", timeZone: "America/New_York",
  }) + " ET";
}

function formatDate(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function formatYards(yds: number): string {
  if (yds >= 1000) return (yds / 1000).toFixed(1) + "k";
  return yds.toFixed(0);
}

type Tab = "schedule" | "articles" | "depth-chart" | "news" | "roster";

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    active: "bg-green-900/50 text-green-400 border-green-700",
    injured: "bg-red-900/50 text-red-400 border-red-700",
    rookie: "bg-blue-900/50 text-blue-400 border-blue-700",
    fa_acq: "bg-amber-900/50 text-amber-400 border-amber-700",
    udfa: "bg-purple-900/50 text-purple-400 border-purple-700",
    drafted: "bg-indigo-900/50 text-indigo-400 border-indigo-700",
    trade: "bg-cyan-900/50 text-cyan-400 border-cyan-700",
  };
  const label: Record<string, string> = {
    active: "Active", injured: "Injured", rookie: "Rookie",
    fa_acq: "FA Acq.", udfa: "UDFA", drafted: "Drafted", trade: "Trade",
  };
  const cls = colors[status] || "bg-gray-800 text-gray-400 border-gray-600";
  return (
    <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded border font-semibold uppercase tracking-wider ${cls}`}>
      {label[status] || status}
    </span>
  );
}

// ── Box Score Row ────────────────────────────────────────────────────
function PlayerStatRow({ player }: { player: BoxScore["away_stats"]["top_players"][0] }) {
  const isQB = player.position === "QB";
  const isRB = player.position === "RB";
  const isWRTE = player.position === "WR" || player.position === "TE";
  const isK = player.position === "K";
  const isDST = player.position === "DST";

  return (
    <tr className="border-t border-white/5 text-xs hover:bg-white/5">
      <td className="px-3 py-1.5 font-medium whitespace-nowrap">
        {player.player_name}
        <span className="ml-1.5 text-[10px] text-gray-500">{player.position}</span>
      </td>
      {isQB && (
        <>
          <td className="px-2 py-1.5 text-center">{player.pass_completions}/{player.pass_attempts}</td>
          <td className="px-2 py-1.5 text-center">{formatYards(player.pass_yards)}</td>
          <td className="px-2 py-1.5 text-center text-green-400">{player.pass_tds || "-"}</td>
          <td className="px-2 py-1.5 text-center text-red-400">{player.pass_int || "0"}</td>
          <td className="px-2 py-1.5 text-center">{player.rush_attempts || "-"}</td>
          <td className="px-2 py-1.5 text-center">{player.rush_yards ? formatYards(player.rush_yards) : "-"}</td>
          <td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">-</td>
        </>
      )}
      {isRB && (
        <>
          <td className="px-2 py-1.5 text-center">-</td><td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">-</td><td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">{player.rush_attempts || "-"}</td>
          <td className="px-2 py-1.5 text-center">{player.rush_yards ? formatYards(player.rush_yards) : "-"}</td>
          <td className="px-2 py-1.5 text-center">{player.rush_tds || "-"}</td>
          <td className="px-2 py-1.5 text-center">{player.receptions || "-"}</td>
        </>
      )}
      {isWRTE && (
        <>
          <td className="px-2 py-1.5 text-center">-</td><td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">-</td><td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">-</td><td className="px-2 py-1.5 text-center">-</td>
          <td className="px-2 py-1.5 text-center">{player.receiving_yards ? formatYards(player.receiving_yards) : "-"}</td>
          <td className="px-2 py-1.5 text-center">{player.receptions || "-"}</td>
        </>
      )}
      {isK && (
        <>
          <td className="px-2 py-1.5 text-center" colSpan={3}>FG: {player.field_goals_made}/{player.field_goals_attempted}</td>
          <td className="px-2 py-1.5 text-center" colSpan={5}>XP: {player.extra_points_made}</td>
        </>
      )}
      {isDST && (
        <>
          <td className="px-2 py-1.5 text-center" colSpan={2}>Sacks: {player.sacks}</td>
          <td className="px-2 py-1.5 text-center" colSpan={2}>Int: {player.interceptions}</td>
          <td className="px-2 py-1.5 text-center" colSpan={2}>FR: {player.fumbles_recovered}</td>
          <td className="px-2 py-1.5 text-center" colSpan={2}>TD: {player.defensive_tds}</td>
        </>
      )}
      {!isQB && !isRB && !isWRTE && !isK && !isDST && (
        <td className="px-2 py-1.5 text-gray-500" colSpan={8}>
          {player.rush_attempts ? `${player.rush_attempts} car, ${formatYards(player.rush_yards)} yds` : ""}
          {player.receptions ? ` ${player.receptions} rec, ${formatYards(player.receiving_yards)} yds` : ""}
          {player.sacks ? ` ${player.sacks} sacks` : ""}
          {player.interceptions ? ` ${player.interceptions} int` : ""}
          {!player.rush_attempts && !player.receptions && !player.sacks && !player.interceptions ? "No stats" : ""}
        </td>
      )}
    </tr>
  );
}

function BoxScoreTable({ teamAbbr, players }: { teamAbbr: string; players: BoxScore["away_stats"]["top_players"] }) {
  const color = getTeamColor(teamAbbr?.toLowerCase() || "");
  if (players.length === 0) return null;
  return (
    <div className="border border-white/10 rounded-lg overflow-hidden bg-white/[0.02]">
      <div className="px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-white" style={{ backgroundColor: color + "60" }}>
        {teamAbbr}
      </div>
      {players.map((p, idx) => (
        <div key={`${p.player_id}-${idx}`} className="border-t border-white/5 px-3 py-2">
          <div className="flex items-center justify-between mb-0.5">
            <span className="text-xs font-semibold">{p.player_name}</span>
            <span className="text-[10px] text-gray-500">{p.position}</span>
          </div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-gray-400">
            {p.pass_attempts > 0 && <span><span className="text-gray-500">Pass: </span>{p.pass_completions}/{p.pass_attempts}, {formatYards(p.pass_yards)}yd, {p.pass_tds}TD</span>}
            {p.rush_attempts > 0 && <span><span className="text-gray-500">Rush: </span>{p.rush_attempts}car, {formatYards(p.rush_yards)}yd, {p.rush_tds}TD</span>}
            {p.receptions > 0 && <span><span className="text-gray-500">Rec: </span>{p.receptions}rec, {formatYards(p.receiving_yards)}yd, {p.receiving_tds}TD</span>}
            {p.targets > 0 && p.receptions === 0 && <span><span className="text-gray-500">Targets: </span>{p.targets}</span>}
            {p.field_goals_attempted > 0 && <span><span className="text-gray-500">FG: </span>{p.field_goals_made}/{p.field_goals_attempted}</span>}
            {p.extra_points_made > 0 && <span><span className="text-gray-500">XP: </span>{p.extra_points_made}</span>}
            {p.sacks > 0 && <span><span className="text-gray-500">Sacks: </span>{p.sacks}</span>}
            {p.interceptions > 0 && <span><span className="text-gray-500">Int: </span>{p.interceptions}</span>}
            {p.defensive_tds > 0 && <span><span className="text-gray-500">TD: </span>{p.defensive_tds}</span>}
            {p.pass_attempts === 0 && p.rush_attempts === 0 && p.receptions === 0 && p.field_goals_attempted === 0 && p.sacks === 0 && p.interceptions === 0 && (
              <span className="text-gray-600">No recorded stats</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Page Component ───────────────────────────────────────────────────
export default function TeamDetailPage() {
  const routeParams = useParams<{ sport: string; abbreviation: string }>();
  const searchParams = useSearchParams();
  const router = useRouter();
  const sport = routeParams?.sport || "nfl";
  const abbr = routeParams?.abbreviation?.toLowerCase() || "";
  const abbrUpper = abbr.toUpperCase();
  const teams = getTeamsForSport(sport);
  const meta = teams[abbr];

  const sportLabel = sport === "nfl" ? "NFL" : sport === "nba" ? "NBA" : sport === "mlb" ? "MLB" : sport.toUpperCase();

  const [team, setTeam] = useState<Team | null>(null);
  const [loading, setLoading] = useState(true);
  const [games, setGames] = useState<any[]>([]);
  const [gamesLoading, setGamesLoading] = useState(false);
  const isMLB = sport === "mlb";

  const teamName = team?.name || meta?.name || abbrUpper;
  useSeo({
    title: `${teamName} — ${sportLabel} Team | Earl Knows Ball`,
    description: `View ${teamName} ${sportLabel} schedule, odds, depth chart, and stats on Earl Knows Ball.`,
    keywords: `${sport}, ${sportLabel}, ${teamName}, team, schedule, odds, depth chart, stats, Earl Knows Ball`,
  });

  const [depthChart, setDepthChart] = useState<DepthChartEntry[]>([]);
  const [depthLoading, setDepthLoading] = useState(false);
  const VALID_TABS: Tab[] = ["schedule", "articles", "depth-chart", "news", "roster"];
  const initTabParam = searchParams.get("tab") as Tab;
  const [tab, setTab] = useState<Tab>(VALID_TABS.includes(initTabParam) ? initTabParam : "schedule");

  // Keep the active tab in the URL so it's shareable / bookmarkable.
  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    if (tab === "schedule") {
      params.delete("tab");
    } else {
      params.set("tab", tab);
    }
    router.replace(`/${sport}/teams/${abbrUpper}${params.size > 0 ? `?${params.toString()}` : ""}`, { scroll: false });
  }, [tab, sport, abbrUpper]);
  const [seasonYear, setSeasonYear] = useState(() => {
    // MLB follows calendar year; NBA season is keyed by the backend (`/api/nba/seasons`),
    // where the year is the season's label in nba.seasons (e.g. 2026 = the 2025-26 season).
    // Start from the current calendar year; it gets corrected to the most recent
    // backend season once /api/nba/seasons loads (see fetch effect below).
    return new Date().getFullYear();
  });
  const [error, setError] = useState("");
  const [availableYears, setAvailableYears] = useState<number[]>([]);


  // Fetch available seasons
  useEffect(() => {
    if (isMLB) {
      // Pull real seasons from the backend (matches the schedule page
      // convention), NOT a hardcoded list. The backend clamps to the 2022
      // season onward, so the team page only offers years >= 2022.
      fetch("/api/mlb/seasons")
        .then((r) => r.json())
        .then((mlbYears: number[]) => {
          setAvailableYears(mlbYears);
          if (mlbYears.length > 0 && !mlbYears.includes(seasonYear)) {
            setSeasonYear(mlbYears[0]); // most recent season with games
          }
        })
        .catch(() => {});
    } else if (sport === "nba") {
      // Pull real seasons from the backend (matches the schedule page convention), not a hardcoded list.
      fetch("/api/nba/seasons")
        .then((r) => r.json())
        .then((seasons: number[]) => {
          setAvailableYears(seasons);
          if (seasons.length > 0 && !seasons.includes(seasonYear)) {
            setSeasonYear(seasons[0]); // most recent season with games
          }
        })
        .catch(() => {});
    } else {
      api.seasons.list().then((years) => {
        setAvailableYears(years);
        if (years.length > 0 && !years.includes(seasonYear)) setSeasonYear(years[0]);
      }).catch(() => {});
    }
  }, [isMLB, sport]);

  // Fetch team data
  useEffect(() => {
    if (!abbr) return;
    setLoading(true);
    setError("");
    api.teams.getByAbbr(abbrUpper).then(setTeam).catch(() => setError("Team not found in database.")).finally(() => setLoading(false));
  }, [abbr]);

  // Fetch games on season change
  useEffect(() => {
    if (isMLB) {
      // MLB doesn't use the team DB model — it fetches via abbreviation directly
      setGamesLoading(true);
      fetch(`/api/mlb/games?year=${seasonYear}&team_abbr=${abbrUpper}`)
        .then(r => r.json())
        .then(setGames)
        .catch(() => setGames([]))
        .finally(() => setGamesLoading(false));
    } else if (sport === "nba") {
      // NBA: fetch full season via abbreviation (monthly grid, MLB-style)
      setGamesLoading(true);
      fetch(`/api/nba/games?year=${seasonYear}&team_abbr=${abbrUpper}`)
        .then(r => r.json())
        .then(setGames)
        .catch(() => setGames([]))
        .finally(() => setGamesLoading(false));
    } else {
      if (!team) return;
      setGamesLoading(true);
      api.games.list({ season_year: seasonYear, team_id: team.id }).then(setGames).catch(() => setGames([])).finally(() => setGamesLoading(false));
    }
  }, [team, seasonYear, isMLB, sport, abbrUpper]);

  // Fetch depth chart on tab switch
  useEffect(() => {
    if (tab !== "depth-chart" || !team) return;
    setDepthLoading(true);
    api.teams.depthChart(team.id).then(setDepthChart).catch(() => setDepthChart([])).finally(() => setDepthLoading(false));
  }, [tab, team]);



  // Not found
  if (!meta) {
    return (
      <div className="text-center py-24 space-y-4">
        <div className="text-6xl">🤷</div>
        <h1 className="font-display text-3xl font-bold text-gray-300">Team Not Found</h1>
        <p className="text-gray-500">No team with that abbreviation exists.</p>
        <Link href={`/${sport}/teams`} className="text-earl-400 hover:underline inline-block mt-2">← Back to Teams</Link>
      </div>
    );
  }

  const teamColor = getTeamColor(abbr);

  // Depth chart
  const positionOrder = ["QB","RB","WR","TE","OT","OG","C","DE","DT","NT","LB","CB","S","K","P","LS"];
  const groupedDepth: Record<string, DepthChartEntry[]> = {};
  for (const entry of depthChart) {
    if (!groupedDepth[entry.position]) groupedDepth[entry.position] = [];
    groupedDepth[entry.position].push(entry);
  }

  return (
    <div className="space-y-6">
      {/* Team Header */}
      <div className="rounded-2xl p-6 md:p-8 border" style={{ borderColor: teamColor + "40", background: `linear-gradient(135deg, ${teamColor}20 0%, transparent 80%)` }}>
        <div className="flex items-center gap-4">
          <TeamLogo abbr={abbrUpper} sport={sport} size={56} />
          <div>
            <h1 className="font-display text-3xl md:text-4xl font-bold">{meta.name}</h1>
            <p className="text-sm text-gray-400 mt-1">
              {meta.conf} {meta.div}
              {team?.byeweek && <span className="ml-3">Bye: Week {team.byeweek}</span>}
            </p>
          </div>
        </div>
        {error && <p className="mt-3 text-sm text-amber-400">⚠ {error} Some features may be limited.</p>}
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 border-b border-white/10">
        <button onClick={() => setTab("schedule")} className={`px-5 py-3 text-sm font-semibold transition rounded-t-lg ${tab === "schedule" ? "text-earl-400 border-b-2 border-earl-400" : "text-gray-500 hover:text-gray-300"}`}>Schedule</button>
        <button onClick={() => setTab("articles")} className={`px-5 py-3 text-sm font-semibold transition rounded-t-lg ${tab === "articles" ? "text-earl-400 border-b-2 border-earl-400" : "text-gray-500 hover:text-gray-300"}`}>Articles</button>
        {sport === "nfl" && (
          <button onClick={() => setTab("depth-chart")} className={`px-5 py-3 text-sm font-semibold transition rounded-t-lg ${tab === "depth-chart" ? "text-earl-400 border-b-2 border-earl-400" : "text-gray-500 hover:text-gray-300"}`}>Depth Chart</button>
        )}
        <button onClick={() => setTab("news")} className={`px-5 py-3 text-sm font-semibold transition rounded-t-lg ${tab === "news" ? "text-earl-400 border-b-2 border-earl-400" : "text-gray-500 hover:text-gray-300"}`}>News</button>
        {isMLB && (
          <button onClick={() => setTab("roster")} className={`px-5 py-3 text-sm font-semibold transition rounded-t-lg ${tab === "roster" ? "text-earl-400 border-b-2 border-earl-400" : "text-gray-500 hover:text-gray-300"}`}>Roster</button>
        )}
      </div>

      {/* Schedule Tab */}
      {tab === "schedule" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-400 font-medium">Season:</label>
            <select value={seasonYear} onChange={e => setSeasonYear(Number(e.target.value))}
              className="px-3 py-1.5 rounded-lg bg-white/10 border border-white/20 text-sm font-semibold text-white focus:outline-none focus:border-earl-500 cursor-pointer appearance-none"
              style={{
                backgroundImage: `url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' fill='none' viewBox='0 0 20 20'%3e%3cpath stroke='%236b7280' stroke-linecap='round' stroke-linejoin='round' stroke-width='1.5' d='M6 8l4 4 4-4'/%3e%3c/svg%3e")`,
                backgroundPosition: "right 0.5rem center", backgroundRepeat: "no-repeat", backgroundSize: "1.25rem", paddingRight: "2rem",
              }}>
              {availableYears.length === 0 && <option value={seasonYear} className="bg-gray-900">{seasonYear}</option>}
              {availableYears.map(yr => <option key={yr} value={yr} className="bg-gray-900">{sport === "nba" ? `${yr}-${yr + 1} Season` : `${yr} Season`}</option>)}
            </select>
          </div>
          {gamesLoading ? (
            <div className="text-center py-16 text-gray-500">Loading games...</div>
          ) : (
            <NFLMLBTeamSchedule games={games} sport={sport} abbrUpper={abbrUpper} seasonYear={seasonYear} formatGameDate={formatGameDate} formatGameTime={formatGameTime} isMLB={isMLB} isNBA={sport === "nba"} />
          )}
        </div>
      )}
      {/* Articles Tab */}
      {tab === "articles" && (
        <div className="space-y-4">
          <TeamArticles sport={sport} abbreviation={abbrUpper} teamName={meta.name} />
        </div>
      )}
      {/* Depth Chart Tab (NFL only) */}
      {tab === "depth-chart" && (
        <div className="space-y-6">
          {depthLoading ? (
            <div className="text-center py-16 text-gray-500">Loading depth chart...</div>
          ) : depthChart.length === 0 ? (
            <div className="text-center py-16 space-y-3">
              <div className="text-4xl">📋</div>
              <p className="text-gray-500">No depth chart data available for {meta.name}.</p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {positionOrder.filter(pos => groupedDepth[pos]).map(pos => {
                const entries = groupedDepth[pos];
                const isOffense = ["QB","RB","WR","TE","OT","OG","C"].includes(pos);
                const isDefense = ["DE","DT","NT","LB","CB","S"].includes(pos);
                let sectionColor = "border-white/10";
                let sectionLabel = "text-gray-400";
                if (isOffense) { sectionColor = "border-green-800/40"; sectionLabel = "text-green-400"; }
                else if (isDefense) { sectionColor = "border-blue-800/40"; sectionLabel = "text-blue-400"; }
                else { sectionColor = "border-amber-800/40"; sectionLabel = "text-amber-400"; }

                return (
                  <div key={pos} className={`border rounded-xl bg-white/5 overflow-hidden ${sectionColor}`}>
                    <div className="px-4 py-2 border-b border-inherit bg-white/[0.03]">
                      <span className={`text-sm font-bold uppercase tracking-wider ${sectionLabel}`}>{pos}</span>
                    </div>
                    <div className="divide-y divide-white/5">
                      {entries.map(entry => (
                        <div key={entry.id} className="px-4 py-2.5 flex items-center justify-between">
                          <div className="flex items-center gap-3 min-w-0">
                            <span className="text-xs text-gray-600 font-mono w-4 shrink-0">
                              {entry.slot === 1 ? <span className="text-earl-400 font-bold">1</span> : entry.slot}
                            </span>
                            <div className="min-w-0">
                              <span className="text-sm font-medium truncate block">{entry.player_name}</span>
                              {entry.jersey_number && <span className="text-[10px] text-gray-600">#{entry.jersey_number}</span>}
                            </div>
                          </div>
                          <div className="flex items-center gap-2 shrink-0">
                            {entry.acquisition_info && <span className="text-[10px] text-gray-500 font-mono">{entry.acquisition_info}</span>}
                            {entry.status && entry.status !== "active" && <StatusBadge status={entry.status} />}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Roster Tab */}
      {tab === "roster" && (
        <TeamRoster sport={sport} teamAbbr={abbrUpper} teamName={meta.name} />
      )}

      {/* News Tab */}
      {tab === "news" && (
        <TeamNews sport={sport} abbreviation={abbrUpper} />
      )}
    </div>
  );
}

// ── Team Roster Component ────────────────────────────────────────────

interface RosterPlayer {
  id: number;
  name: string;
  position: string;
  jersey_number: number | null;
  bats: string | null;
  throws: string | null;
  years_exp: number | null;
  height: number | null;
  weight: number | null;
  college: string | null;
  headshot_url: string | null;
  status: string | null;
  games_played: number;
  games_started: number;
  has_current_stats: number;
  // Pitching stats
  wins: number | null;
  losses: number | null;
  saves: number | null;
  era: number | null;
  whip: number | null;
  innings_pitched: number | null;
  k: number | null;
  bb: number | null;
  // Batting stats
  avg: number | null;
  obp: number | null;
  slg: number | null;
  ops: number | null;
  home_runs: number | null;
  rbi: number | null;
  sb: number | null;
  at_bats: number | null;
  hits: number | null;
}

interface RosterData {
  team_abbr: string;
  year: number;
  roster: {
    starting_pitchers: RosterPlayer[];
    relief_pitchers: RosterPlayer[];
    catchers: RosterPlayer[];
    infielders: RosterPlayer[];
    outfielders: RosterPlayer[];
    designated_hitters: RosterPlayer[];
  };
}

const ROSTER_LABELS: Record<string, { title: string; icon: string }> = {
  starting_pitchers: { title: "Starting Pitchers", icon: "⚾" },
  relief_pitchers: { title: "Relief Pitchers", icon: "🔥" },
  catchers: { title: "Catchers", icon: "🛡️" },
  infielders: { title: "Infielders", icon: "🟢" },
  outfielders: { title: "Outfielders", icon: "🧢" },
  designated_hitters: { title: "Designated Hitters", icon: "🔋" },
};

// ── Pitcher Row ──────────────────────────────────────────────────────
function PitcherRow({ player, sport }: { player: RosterPlayer; sport: string }) {
  return (
    <Link
      href={`/${sport}/players/${player.id}`}
      className={`flex items-center gap-3 px-4 py-2.5 border-b border-white/5 hover:bg-white/[0.03] transition text-sm ${!player.has_current_stats ? "opacity-50" : ""}`}
    >
      {/* Number + Name */}
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <span className="text-[11px] text-gray-600 font-mono w-6 text-right shrink-0">
          {player.jersey_number ? `#${player.jersey_number}` : "—"}
        </span>
        {player.has_current_stats ? (
          <span className="w-1.5 h-1.5 rounded-full bg-earl-500 shrink-0" title="Current season player" />
        ) : null}
        <span className="font-medium text-white truncate">{player.name}</span>
      </div>

      {/* Throws */}
      <div className="w-10 text-center shrink-0">
        <span className="text-[11px] text-gray-500 uppercase">{player.throws || "-"}</span>
      </div>

      {/* W-L / Saves */}
      <div className="w-16 text-center shrink-0">
        {player.wins != null ? (
          <span className="text-xs text-gray-300">
            {player.wins}-{player.losses ?? 0}
          </span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
        {player.saves != null && player.saves > 0 && (
          <span className="text-[10px] text-earl-400 ml-1">{player.saves} SV</span>
        )}
      </div>

      {/* ERA */}
      <div className="w-16 text-center shrink-0">
        {player.era != null ? (
          <span className="text-xs font-semibold text-white">{player.era.toFixed(2)}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* WHIP */}
      <div className="w-16 text-center shrink-0">
        {player.whip != null ? (
          <span className="text-xs text-gray-400">{player.whip.toFixed(2)}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* K/BB */}
      <div className="w-20 text-center shrink-0">
        <span className="text-xs text-gray-400">
          {player.k != null ? player.k : "—"}
          <span className="text-gray-600">/</span>
          {player.bb != null ? player.bb : "—"}
        </span>
      </div>

      {/* IP */}
      <div className="w-14 text-center shrink-0">
        {player.innings_pitched != null ? (
          <span className="text-xs text-gray-400">{player.innings_pitched}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>
    </Link>
  );
}

// ── Batter Row ───────────────────────────────────────────────────────
function BatterRow({ player, sport }: { player: RosterPlayer; sport: string }) {
  // Determine primary display position
  const displayPos = player.position;

  return (
    <Link
      href={`/${sport}/players/${player.id}`}
      className={`flex items-center gap-3 px-4 py-2.5 border-b border-white/5 hover:bg-white/[0.03] transition text-sm ${!player.has_current_stats ? "opacity-50" : ""}`}
    >
      {/* Number + Position + Name */}
      <div className="flex items-center gap-2 min-w-0 flex-1">
        <span className="text-[11px] text-gray-600 font-mono w-6 text-right shrink-0">
          {player.jersey_number ? `#${player.jersey_number}` : "—"}
        </span>
        <span className="text-[10px] font-bold uppercase tracking-wider text-earl-400 w-7 shrink-0">
          {displayPos}
        </span>
        {player.has_current_stats ? (
          <span className="w-1.5 h-1.5 rounded-full bg-earl-500 shrink-0" title="Current season player" />
        ) : null}
        <span className="font-medium text-white truncate">{player.name}</span>
        <span className="text-[10px] text-gray-600 shrink-0">
          {player.bats || ""}{player.bats && player.throws ? "/" : ""}{player.throws || ""}
        </span>
      </div>

      {/* GP */}
      <div className="w-10 text-center shrink-0">
        <span className="text-xs text-gray-400">{player.games_played}</span>
      </div>

      {/* AVG */}
      <div className="w-14 text-center shrink-0">
        {player.avg != null ? (
          <span className="text-xs font-semibold text-white">{player.avg.toFixed(3).slice(1)}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* OPS */}
      <div className="w-16 text-center shrink-0">
        {player.ops != null ? (
          <span className="text-xs text-gray-400">{player.ops.toFixed(3)}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* HR */}
      <div className="w-10 text-center shrink-0">
        {player.home_runs != null ? (
          <span className="text-xs font-semibold text-earl-400">{player.home_runs}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* RBI */}
      <div className="w-10 text-center shrink-0">
        {player.rbi != null ? (
          <span className="text-xs text-gray-300">{player.rbi}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>

      {/* SB */}
      <div className="w-10 text-center shrink-0">
        {player.sb != null ? (
          <span className="text-xs text-gray-400">{player.sb}</span>
        ) : (
          <span className="text-xs text-gray-600">—</span>
        )}
      </div>
    </Link>
  );
}

function RosterSection({
  groupKey,
  players,
  sport,
}: {
  groupKey: string;
  players: RosterPlayer[];
  sport: string;
}) {
  const info = ROSTER_LABELS[groupKey];
  const isPitchers = groupKey === "starting_pitchers" || groupKey === "relief_pitchers";

  if (players.length === 0) return null;

  return (
    <div className="border border-white/10 rounded-xl overflow-hidden bg-white/[0.02]">
      {/* Section header */}
      <div className="px-4 py-2.5 bg-white/[0.04] border-b border-white/10 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm">{info.icon}</span>
          <span className="text-sm font-bold text-white">{info.title}</span>
          <span className="text-[11px] text-gray-500 font-mono">{players.length}</span>
        </div>
      </div>

      {/* Column headers */}
      {isPitchers ? (
        <div className="flex items-center gap-3 px-4 py-1.5 border-b border-white/5 text-[10px] text-gray-600 uppercase tracking-wider font-semibold">
          <div className="flex-1" />
          <div className="w-10 text-center">Thr</div>
          <div className="w-16 text-center">W-L (SV)</div>
          <div className="w-16 text-center">ERA</div>
          <div className="w-16 text-center">WHIP</div>
          <div className="w-20 text-center">K/BB</div>
          <div className="w-14 text-center">IP</div>
        </div>
      ) : (
        <div className="flex items-center gap-3 px-4 py-1.5 border-b border-white/5 text-[10px] text-gray-600 uppercase tracking-wider font-semibold">
          <div className="flex-1" />
          <div className="w-10 text-center">GP</div>
          <div className="w-14 text-center">AVG</div>
          <div className="w-16 text-center">OPS</div>
          <div className="w-10 text-center">HR</div>
          <div className="w-10 text-center">RBI</div>
          <div className="w-10 text-center">SB</div>
        </div>
      )}

      {/* Player rows */}
      <div className="divide-y-0">
        {players.map((p) =>
          isPitchers ? (
            <PitcherRow key={p.id} player={p} sport={sport} />
          ) : (
            <BatterRow key={p.id} player={p} sport={sport} />
          )
        )}
      </div>
    </div>
  );
}

interface ILPlayer {
  id: number | null;
  mlb_id: number;
  name: string;
  position: string;
  status: string;
  jersey_number: number | null;
  headshot_url: string | null;
  team_abbr: string;
}

function InjuredListSection({
  injured,
  sport,
  abbr,
}: {
  injured: ILPlayer[];
  sport: string;
  abbr: string;
}) {
  if (injured.length === 0) return null;

  return (
    <div className="border border-red-900/30 rounded-xl overflow-hidden bg-red-950/10">
      {/* Header */}
      <div className="px-4 py-2.5 bg-red-900/10 border-b border-red-900/30 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-sm">🩹</span>
          <span className="text-sm font-bold text-red-400">Injured List</span>
          <span className="text-[11px] text-gray-500 font-mono">{injured.length}</span>
        </div>
      </div>

      {/* Column headers */}
      <div className="flex items-center gap-3 px-4 py-1.5 border-b border-white/5 text-[10px] text-gray-600 uppercase tracking-wider font-semibold">
        <div className="flex-1">Player</div>
        <div className="w-16 text-center">Status</div>
        <div className="w-10 text-center">Pos</div>
      </div>

      {/* IL rows */}
      <div className="divide-y-0">
        {injured.map((p) => (
          <Link
            key={p.mlb_id}
            href={`/${sport}/players/${p.id}`}
            className={`flex items-center gap-3 px-4 py-2.5 border-b border-white/5 hover:bg-white/[0.03] transition text-sm ${!p.id ? "pointer-events-none opacity-60" : ""}`}
          >
            <div className="flex items-center gap-2 min-w-0 flex-1">
              <span className="text-[11px] text-gray-600 font-mono w-6 text-right shrink-0">
                {p.jersey_number ? `#${p.jersey_number}` : "—"}
              </span>
              <span className="font-medium text-white truncate">{p.name}</span>
            </div>
            <div className="w-16 text-center shrink-0">
              <span className="text-[11px] font-semibold text-red-400 whitespace-nowrap">
                {p.status}
              </span>
            </div>
            <div className="w-10 text-center shrink-0">
              <span className="text-[10px] text-gray-500 uppercase font-bold">{p.position}</span>
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

function TeamRoster({ sport, teamAbbr, teamName }: { sport: string; teamAbbr: string; teamName: string }) {
  const [roster, setRoster] = useState<RosterData | null>(null);
  const [injured, setInjured] = useState<ILPlayer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!teamAbbr) return;
    setLoading(true);
    setError(null);
    Promise.all([
      fetch(`/api/mlb/teams/${teamAbbr}/roster`).then((r) => r.json()),
      fetch(`/api/mlb/injured-list?team_abbr=${teamAbbr}`).then((r) => r.json()),
    ])
      .then(([rosterData, ilData]) => {
        setRoster(rosterData);
        setInjured(ilData);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [teamAbbr]);

  if (loading) {
    return (
      <div className="text-center py-16">
        <div className="animate-pulse space-y-4">
          {[1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="h-12 bg-white/5 rounded-xl" />
          ))}
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="text-center py-16 space-y-3">
        <div className="text-4xl">👥</div>
        <p className="text-gray-500">Couldn't load roster data</p>
        <p className="text-xs text-gray-600">{error}</p>
      </div>
    );
  }

  if (!roster || Object.values(roster.roster).every((g) => g.length === 0)) {
    return (
      <div className="text-center py-16 space-y-3">
        <div className="text-4xl">👥</div>
        <p className="text-gray-500">No roster data available for the current season</p>
        <p className="text-xs text-gray-600">Roster data is based on 2026 season stats.</p>
      </div>
    );
  }

  const rosterOrder = ["starting_pitchers", "relief_pitchers", "catchers", "infielders", "outfielders", "designated_hitters"];
  const totalActive = Object.values(roster.roster).reduce((sum, g) => sum + g.length, 0);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm text-gray-400 font-semibold">Current Roster</h3>
        <div className="flex items-center gap-3 text-xs text-gray-500">
          <span>{totalActive} active</span>
          {injured.length > 0 && <span className="text-red-400">{injured.length} IL</span>}
        </div>
      </div>

      {rosterOrder.map((key) =>
        roster.roster[key as keyof typeof roster.roster].length > 0 ? (
          <RosterSection
            key={key}
            groupKey={key}
            players={roster.roster[key as keyof typeof roster.roster]}
            sport={sport}
          />
        ) : null
      )}

      {injured.length > 0 && (
        <InjuredListSection injured={injured} sport={sport} abbr={teamAbbr} />
      )}
    </div>
  );
}


// ── Team News Component ────────────────────────────────────────────────
// ── NBA Team Schedule Component ─────────────────────────────────────
interface NBATeamScheduleProps {
  sport: string;
  abbrUpper: string;
  formatGameDate: (d: string) => string;
  formatGameTime: (d: string) => string;
}

interface NBATeamGame {
  id: number;
  home_team: string;
  away_team: string;
  home_score: number | null;
  away_score: number | null;
  date: string;
  time: string;
  status: string;
  game_type: string;
  game_id: number;
  homeTeam?: string;
  awayTeam?: string;
  homeScore?: number | null;
  awayScore?: number | null;
  is_final?: boolean;
  isFinal?: boolean;
  game_status?: string;
  spread?: number;
  over_under?: number;
  home_moneyline?: number | null;
  away_moneyline?: number | null;
  pick_spread?: string | null;
  pick_over_under?: string | null;
  pick_moneyline?: string | null;
  pick_ats_ev?: number | null;
  pick_ou_ev?: number | null;
  pick_ml_ev?: number | null;
  result_spread?: string | null;
  result_over_under?: string | null;
  result_moneyline?: string | null;
}

const CURRENT_YEAR_NBA = 2026;

function NBATeamSchedule({ sport, abbrUpper, formatGameDate, formatGameTime }: NBATeamScheduleProps) {
  const searchParams = useSearchParams();
  const router = useRouter();

  const [availableSeasons, setAvailableSeasons] = useState<number[]>([]);
  const [year, setYear] = useState(() => {
    const yp = searchParams.get("year");
    return yp ? parseInt(yp) : CURRENT_YEAR_NBA;
  });
  const [selectedDate, setSelectedDate] = useState(() => {
    return searchParams.get("date") || todayStr();
  });
  const [games, setGames] = useState<NBATeamGame[]>([]);
  const [showCalendar, setShowCalendar] = useState(false);
  const [loading, setLoading] = useState(false);
  const [gameDates, setGameDates] = useState<string[]>([]);
  const cancelSearchRef = useRef(false);
  const autoSearchRef = useRef<"idle" | "done">("idle");

  // Sync URL params for back-button support
  useEffect(() => {
    const params = new URLSearchParams(searchParams.toString());
    params.set('year', String(year));
    params.set('date', selectedDate);
    router.replace(`/${sport}/teams/${abbrUpper}?${params.toString()}`, { scroll: false });
  }, [year, selectedDate, sport, abbrUpper]);

  function todayStr(): string {
    const d = new Date();
    const offset = d.getTimezoneOffset();
    return new Date(d.getTime() - offset * 60_000).toISOString().slice(0, 10);
  }

  const isCurrentYear = year === CURRENT_YEAR_NBA;

  // Fetch available seasons
  useEffect(() => {
    fetch(`/api/nba/seasons`)
      .then((r) => r.json())
      .then((seasons: number[]) => {
        setAvailableSeasons(seasons);
        if (seasons.length > 0 && !seasons.includes(year)) {
          setYear(seasons[0]);
        }
      });
  }, []);

  // Fetch game dates for calendar
  useEffect(() => {
    fetch(`/api/nba/games/dates?year=${year}&team_abbr=${abbrUpper}`)
      .then((r) => r.json())
      .then(setGameDates)
      .catch(() => {});
  }, [year, abbrUpper]);

  // Fetch games
  useEffect(() => {
    setLoading(true);
    fetch(`/api/nba/games?year=${year}&team_abbr=${abbrUpper}&date=${selectedDate}`)
      .then((r) => r.json())
      .then((data: NBATeamGame[]) => setGames(Array.isArray(data) ? data : []))
      .catch(() => setGames([]))
      .finally(() => setLoading(false));
  }, [year, selectedDate, abbrUpper]);

  // Auto-search: when games load empty, query backend for nearest date with games
  useEffect(() => {
    if (autoSearchRef.current === "done" || loading) return;

    if (games.length === 0 && !loading) {
      autoSearchRef.current = "done";
      cancelSearchRef.current = false;
      findNearestGame(year, selectedDate);
    } else if (games.length > 0) {
      autoSearchRef.current = "done";
    }
  }, [year, selectedDate, loading, games]);

  function todayStr2(): string {
    const d = new Date();
    const offset = d.getTimezoneOffset();
    return new Date(d.getTime() - offset * 60_000).toISOString().slice(0, 10);
  }
  const todayStrVal = todayStr2();

  // Set autoSearchRef to idle on initial mount (allow one auto-search)
  useEffect(() => {
    const t = setTimeout(() => {
      if (autoSearchRef.current === "idle") {
        // check if we should auto-search (only if no date was in URL)
        const urlDate = searchParams.get("date");
        if (!urlDate) {
          autoSearchRef.current = "idle";
        } else {
          autoSearchRef.current = "done";
        }
      }
    }, 100);
    return () => clearTimeout(t);
  }, []);

  async function findNearestGame(currentYear: number, date: string) {
    if (currentYear < 2009) return;
    try {
      const r = await fetch(
        `/api/nba/games/nearest-date?year=${currentYear}&date=${encodeURIComponent(date)}&team_abbr=${abbrUpper}`
      );
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        if (!cancelSearchRef.current) {
          setYear(res.year);
          setSelectedDate(res.date);
        }
      } else {
        if (!cancelSearchRef.current) {
          findNearestGame(currentYear - 1, date);
        }
      }
    } catch {}
  }

  async function goDay(delta: number) {
    autoSearchRef.current = "done";
    cancelSearchRef.current = true;
    const direction = delta > 0 ? "forward" : "backward";
    try {
      const r = await fetch(
        `/api/nba/games/nearest-date?year=${year}&date=${encodeURIComponent(selectedDate)}&direction=${direction}&team_abbr=${abbrUpper}`
      );
      const res: { date: string | null; year: number | null } = await r.json();
      if (res.date && res.year) {
        setYear(res.year);
        setSelectedDate(res.date);
      }
    } catch {}
  }

  // Auto-poll live scores for today
  useEffect(() => {
    if (!isCurrentYear || selectedDate !== todayStrVal) return;
    const interval = setInterval(() => {
      fetch(`/api/nba/games?year=${CURRENT_YEAR_NBA}&team_abbr=${abbrUpper}&date=${selectedDate}`)
        .then((r) => r.json())
        .then((data: NBATeamGame[]) => setGames(Array.isArray(data) ? data : []))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(interval);
  }, [isCurrentYear, selectedDate, abbrUpper]);

  const minDate =
    availableSeasons.length > 0 ? `${Math.min(...availableSeasons)}-10-01` : "2009-10-01";
  const maxDate =
    availableSeasons.length > 0
      ? `${Math.max(...availableSeasons) + 1}-06-30`
      : `${CURRENT_YEAR_NBA + 1}-06-30`;

  const dateObj = (() => {
    if (selectedDate) {
      const d = new Date(selectedDate + "T12:00:00-05:00");
      if (!isNaN(d.getTime())) return d;
    }
    return new Date();
  })();

  const dateLabel = dateObj.toLocaleDateString("en-US", {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <select
          value={year}
          onChange={(e) => {
            cancelSearchRef.current = true;
            const newYear = Number(e.target.value);
            setYear(newYear);
            setSelectedDate(todayStr());
            setTimeout(() => {
              cancelSearchRef.current = false;
              autoSearchRef.current = "idle";
              findNearestGame(newYear, todayStr());
            }, 100);
          }}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {availableSeasons.map((sy) => (
            <option key={sy} value={sy} className="text-black">
              {sy}-{sy + 1} Season
            </option>
          ))}
        </select>

        <button
          onClick={() => goDay(-1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 transition"
        >
          &larr;
        </button>

        <div className="relative">
          <button
            onClick={() => setShowCalendar(!showCalendar)}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 hover:bg-white/10 transition"
          >
            {selectedDate}
          </button>
          {showCalendar && (
            <GameCalendar
              gameDates={gameDates}
              selectedDate={selectedDate}
              onSelect={(d: string) => {
                cancelSearchRef.current = true;
                autoSearchRef.current = "done";
                setSelectedDate(d);
              }}
              onClose={() => setShowCalendar(false)}
              minDate={minDate}
              maxDate={maxDate}
            />
          )}
        </div>

        <button
          onClick={() => goDay(1)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 transition"
        >
          &rarr;
        </button>

        {isCurrentYear && (
          <button
            onClick={() => {
              autoSearchRef.current = "idle";
              setSelectedDate(todayStr());
            }}
            className={selectedDate === todayStr() ? "px-3 py-1.5 rounded-lg border text-xs transition bg-earl-600/20 border-earl-500/30 text-earl-400" : "px-3 py-1.5 rounded-lg border text-xs transition bg-white/5 border-white/10 text-gray-400 hover:bg-white/10"
            }
          >
            Today
          </button>
        )}
      </div>

      <p className="text-gray-400 text-sm">{dateLabel}</p>

      {loading ? (
        <p className="text-gray-400">Loading...</p>
      ) : games.length === 0 ? (
        <p className="text-gray-400">No games for {abbrUpper} on this date.</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {games.map((g: NBATeamGame) => {
            const homeTeam = g.home_team || g.homeTeam || "";
            const awayTeam = g.away_team || g.awayTeam || "";
            const homeScore = g.home_score ?? g.homeScore ?? null;
            const awayScore = g.away_score ?? g.awayScore ?? null;
            const status = g.status || g.game_status || "";
            const isFinal =
              status === "FINAL" || g.is_final === true || g.isFinal === true || (homeScore !== null && awayScore !== null);
            const isLive = status === "LIVE" || status === "IN_PROGRESS";
            const isScheduled = !isFinal && !isLive;
            const gameId = g.game_id ?? g.id;

            const awayWon = isFinal && awayScore != null && homeScore != null && awayScore > homeScore;
            const homeWon = isFinal && homeScore != null && awayScore != null && homeScore > awayScore;

            return (
              <Link
                key={gameId}
                href={"/" + sport + "/games/" + gameId + "?year=" + year + "&date=" + selectedDate}
                className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
              >
                {/* Teams row with logos, names, and scores */}
                <div className="flex items-center justify-center gap-1.5 text-lg">
                  {awayTeam && (
                    <TeamLogo abbr={awayTeam} sport="nba" size={20} />
                  )}
                  <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>{awayTeam}</div>
                  {isFinal && awayScore !== null && <span className="font-bold text-white">{awayScore}</span>}
                  {isLive && awayScore !== null && <span className="font-bold text-red-400">{awayScore}</span>}
                  <span className="text-gray-500 font-medium">@</span>
                  {isFinal && homeScore !== null && <span className="font-bold text-white">{homeScore}</span>}
                  {isLive && homeScore !== null && <span className="font-bold text-red-400">{homeScore}</span>}
                  <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>{homeTeam}</div>
                  {homeTeam && (
                    <TeamLogo abbr={homeTeam} sport="nba" size={20} />
                  )}
                </div>

                {/* Status badge or time */}
                <div className="mt-1">
                  {isFinal && (
                    <span className="text-[10px] font-bold uppercase tracking-wider text-gray-400">Final</span>
                  )}
                  {isLive && (
                    <span className="text-[10px] font-bold uppercase tracking-wider text-red-400">Live</span>
                  )}
                  {isScheduled && (
                    <div className="text-xs text-gray-500">{formatGameTime(selectedDate + "T" + (g.time || "19:00"))}</div>
                  )}
                </div>

                {/* Odds section + premium picks — identical to the schedule game cards */}
                <SchedulePicksFooter game={g} />
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

interface NFLMLBTeamScheduleProps {
  games: any[];
  sport: string;
  abbrUpper: string;
  seasonYear: number;
  formatGameDate: (d: string) => string;
  formatGameTime: (d: string) => string;
  isMLB: boolean;
  isNBA?: boolean;
}

function MLBMonthsFor(isMLB: boolean, isNBA: boolean): string[] {
  if (isMLB) return ["March","April","May","June","July","August","September","October"];
  if (isNBA) return ["October","November","December","January","February","March","April","May","June"];
  return [];
}

function NFLMLBTeamSchedule({ games, sport, abbrUpper, seasonYear, formatGameDate, formatGameTime, isMLB, isNBA }: NFLMLBTeamScheduleProps) {
  const months = MLBMonthsFor(!!isMLB, !!isNBA);
  const [monthIdx, setMonthIdx] = useState(() => {
    const monthList = MLBMonthsFor(!!isMLB, !!isNBA);
    const now = new Date();
    const m = now.getMonth() + 1;
    const idx = monthList.findIndex(name => name.toLowerCase() === now.toLocaleString("en-US", { month: "long" }).toLowerCase());
    return idx >= 0 ? idx : 0;
  });

  if (isMLB || isNBA) {
    // Resolve the calendar month (1-12) from the displayed month name — the months
    // arrays are NOT zero-indexed from January (MLB starts at March, NBA at October).
    const filterMonth = new Date(Date.parse(months[monthIdx] + " 1, 2000")).getMonth() + 1;
    // Games are stored in UTC but displayed in America/New_York (matches formatGameDate/Time below),
    // so resolve each game's month in ET to keep cards under the same month the user sees.
    const etMonthOf = (iso: string) =>
      Number(new Intl.DateTimeFormat("en-US", { month: "numeric", timeZone: "America/New_York" }).format(new Date(iso)));
    const monthGames = games.filter((g: any) => etMonthOf(g.date) === filterMonth);

    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <button onClick={() => setMonthIdx(i => Math.max(0, i - 1))} disabled={monthIdx === 0}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
          >← {months[monthIdx - 1] || ""}</button>
          <span className="text-sm font-semibold text-white px-4">{months[monthIdx]}</span>
          <button onClick={() => setMonthIdx(i => Math.min(months.length - 1, i + 1))} disabled={monthIdx === months.length - 1}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
          >{months[monthIdx + 1] || ""} →</button>
        </div>

        {monthGames.length === 0 ? (
          <div className="text-center py-8 text-gray-500">No games in {months[monthIdx]}.</div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {monthGames.map((g: any) => {
              const isFinal = g.status.toLowerCase() === "final";
              const isHome = g.home_team === abbrUpper;
              const teamScore = isHome ? g.home_score : g.away_score;
              const oppScore = isHome ? g.away_score : g.home_score;
              const won = isFinal && teamScore != null && oppScore != null && teamScore > oppScore;
              const lost = isFinal && teamScore != null && oppScore != null && teamScore < oppScore;
              const opponent = isHome ? g.away_team : g.home_team;
              return (
                <Link key={g.id} href={"/" + sport + "/games/" + g.id}
                  className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
                >
                  {/* Date */}
                  <div className="text-[10px] text-gray-500 uppercase tracking-wider">{formatGameDate(g.date)}</div>

                  {/* Opponent */}
                  <div className="flex items-center justify-center gap-2 mt-2">
                    <TeamLogo abbr={opponent} sport={sport} size={24} />
                    <span className="text-sm font-semibold text-gray-200">{opponent}</span>
                  </div>

                  {/* vs/@ indicator */}
                  <div className="text-[11px] text-gray-600 mt-0.5">{isHome ? "vs" : "@"} {isHome ? abbrUpper : opponent}</div>

                  {/* Score or Time */}
                  {isFinal ? (
                    <div className="mt-2 flex items-center justify-center gap-3">
                      <span className={"text-base font-bold " + (won ? "text-earl-400" : "text-white")}>{isHome ? g.away_score : g.home_score}</span>
                      <span className={"text-[10px] font-bold uppercase tracking-wider " + (won ? "text-green-400" : lost ? "text-red-400" : "text-gray-500")}>{won ? "W" : lost ? "L" : "T"}</span>
                      <span className={"text-base font-bold " + (!isHome ? "text-earl-400" : "text-white")}>{isHome ? g.home_score : g.away_score}</span>
                    </div>
                  ) : (
                    <div className="mt-2 text-sm font-semibold text-gray-400">{formatGameTime(g.date)}</div>
                  )}

                  {/* Extra info */}
                  {isFinal && g.actual_innings && g.actual_innings > 9 && <div className="text-[10px] text-gray-500 mt-1">{g.actual_innings} inn</div>}
                  {!isFinal && g.venue && <div className="text-[10px] text-gray-600 truncate mt-1 px-2">{g.venue}</div>}

                  {/* Betting line + premium picks — identical to the schedule game cards */}
                  <SchedulePicksFooter game={g} />
                </Link>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // NFL schedule: 3-column grid (same layout as MLB/NBA team schedule pages)
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
      {games.map((g: any) => {
        const isFinal = g.status === "final";
        const isHome = g.home_team === abbrUpper;
        const teamScore = isHome ? g.home_score : g.away_score;
        const oppScore = isHome ? g.away_score : g.home_score;
        const won = isFinal && teamScore != null && oppScore != null && teamScore > oppScore;
        const lost = isFinal && teamScore != null && oppScore != null && teamScore < oppScore;
        const opponent = isHome ? g.away_team : g.home_team;
        return (
          <Link key={g.id} href={"/" + sport + "/games/" + g.id}
            className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
          >
            {/* Week + Date (preseason → PS Week N, matching the NFL schedule page) */}
            <div className="text-[10px] text-gray-500 uppercase tracking-wider">
              {g.week
                ? (g.game_type === "PRE" ? "PS Week " + (g.week - 29) + " · " : "Week " + g.week + " · ")
                : ""}{formatGameDate(g.date)}
            </div>

            {/* Opponent */}
            <div className="flex items-center justify-center gap-2 mt-2">
              <TeamLogo abbr={opponent} sport="nfl" size={24} />
              <span className="text-sm font-semibold text-gray-200">{opponent}</span>
            </div>

            {/* vs/@ indicator */}
            <div className="text-[11px] text-gray-600 mt-0.5">{isHome ? "vs" : "@"} {isHome ? abbrUpper : opponent}</div>

            {/* Score or Time */}
            {isFinal ? (
              <div className="mt-2 flex items-center justify-center gap-3">
                <span className={"text-base font-bold " + (won ? "text-earl-400" : "text-white")}>{teamScore}</span>
                <span className={"text-[10px] font-bold uppercase tracking-wider " + (won ? "text-green-400" : lost ? "text-red-400" : "text-gray-500")}>{won ? "W" : lost ? "L" : "T"}</span>
                <span className={"text-base font-bold " + (lost ? "text-red-400" : "text-white")}>{oppScore}</span>
              </div>
            ) : (
              <div className="mt-2 text-sm font-semibold text-gray-400">{formatGameTime(g.date)}</div>
            )}

            {/* Betting lines + premium picks — identical to the schedule game cards */}
            <SchedulePicksFooter game={g} />
          </Link>
        );
      })}
    </div>
  );
}

// ── END NFL/MLB Team Schedule Component ──────────────────────────────

interface TeamNewsProps {
  sport: string;
  abbreviation: string;
}

interface TeamArticle {
  id: number;
  title: string;
  description?: string;
  url?: string;
  published_date?: string;
  source?: string;
  source_name?: string;
  source_url?: string;
  content?: string;
  image_url?: string;
  excerpt?: string;
  author?: string;
  category?: string;
  published_at?: string;
}

function TeamNews({ sport, abbreviation }: TeamNewsProps) {
  const [articles, setArticles] = useState<TeamArticle[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchNews = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/articles/team/${sport}/${abbreviation}?limit=30&days_back=30`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setArticles(data.articles || []);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };
    fetchNews();
  }, [sport, abbreviation]);

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm text-gray-400 font-semibold">Recent News & Articles</h3>
        {!loading && !error && (
          <span className="text-xs text-gray-500">{articles.length} articles (last 30 days)</span>
        )}
      </div>

      {loading ? (
        <div className="text-center py-16">
          <div className="animate-pulse space-y-3">
            {[1, 2, 3, 4, 5].map((i) => (
              <div key={i} className="h-16 bg-white/5 rounded-lg"></div>
            ))}
          </div>
        </div>
      ) : error ? (
        <div className="text-center py-16 space-y-3">
          <div className="text-4xl">📡</div>
          <p className="text-gray-500">Couldn't load team news</p>
          <p className="text-xs text-gray-600">{error}</p>
        </div>
      ) : articles.length === 0 ? (
        <div className="text-center py-16 space-y-3">
          <div className="text-4xl">📰</div>
          <p className="text-gray-500">No recent articles for this team</p>
          <p className="text-xs text-gray-600">Articles appear as they're scraped from team-specific and national RSS feeds.</p>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex flex-wrap gap-2 mb-4">
            <span className="text-[10px] text-gray-500 uppercase tracking-wider">Sources:</span>
            {Array.from(new Set(articles.map(a => a.source_name).filter(Boolean))).sort().map(s => (
              <span key={s} className="px-2 py-0.5 bg-white/5 rounded text-[10px] text-gray-400">{s}</span>
            ))}
          </div>

          {articles.map((article) => (
            <a
              key={article.id}
              href={article.source_url || "#"}
              target={article.source_url ? "_blank" : undefined}
              rel={article.source_url ? "noreferrer" : undefined}
              className="block px-4 py-3 bg-white/[0.02] border border-white/10 rounded-lg hover:bg-white/[0.05] hover:border-earl-600/30 transition group"
            >
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="text-sm text-white font-medium group-hover:text-earl-400 transition line-clamp-2">
                    {article.title}
                  </div>
                  {article.excerpt && (
                    <p className="text-xs text-gray-500 mt-1 line-clamp-2">
                      {article.excerpt}
                    </p>
                  )}
                  <div className="flex items-center gap-3 mt-2 text-[10px] text-gray-600">
                    <span className="font-medium text-gray-400">{article.source_name}</span>
                    {article.author && <span>by {article.author}</span>}
                    {article.published_at && (
                      <span>{new Date(article.published_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}</span>
                    )}
                    {article.category && (
                      <span className="px-1.5 py-0.5 bg-white/5 rounded text-[9px] uppercase">{article.category.replace(/_/g, " ")}</span>
                    )}
                  </div>
                </div>
                <svg className="w-4 h-4 text-gray-600 group-hover:text-earl-400 shrink-0 mt-1 transition" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

interface TeamContentItem {
  type: "writeup" | "article";
  game_id?: number;
  id?: number;
  title?: string;
  slug?: string;
  summary?: string;
  author?: string;
  link?: string;
  published_at?: string;
  game_date?: string;
  home_abbr?: string;
  away_abbr?: string;
  matchup?: string;
  teams?: string[];
}

function TeamArticles({ sport, abbreviation, teamName }: {
  sport: string;
  abbreviation: string;
  teamName: string;
}) {
  const [items, setItems] = useState<TeamContentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [pages, setPages] = useState(1);
  const searchParams = useSearchParams();
  const router = useRouter();
  const pageParam = searchParams.get("page");
  const currentPage = Math.max(1, parseInt(pageParam || "1", 10) || 1);
  const PER_PAGE = 10;

  useEffect(() => {
    const fetchContent = async () => {
      setLoading(true);
      setError(null);
      try {
        const res = await fetch(`/api/articles/team-content/${sport}/${abbreviation}?page=${currentPage}&per_page=${PER_PAGE}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setItems(data.items || []);
        setTotal(data.total || 0);
        setPages(data.pages || 1);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };
    fetchContent();
  }, [sport, abbreviation, currentPage]);

  const goToPage = (p: number) => {
    const params = new URLSearchParams(searchParams.toString());
    if (p <= 1) params.delete("page");
    else params.set("page", String(p));
    params.set("tab", "articles");
    router.replace(`/${sport}/teams/${abbreviation.toUpperCase()}${params.size > 0 ? `?${params.toString()}` : ""}`, { scroll: false });
  };

  return (
    <div className="space-y-2">

      {loading ? (
        <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
          Loading…
        </div>
      ) : error ? (
        <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
          Couldn't load team articles — {error}
        </div>
      ) : items.length === 0 ? (
        <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
          No writeups or articles for {teamName} yet.
        </div>
      ) : (
        <ul className="divide-y divide-white/10 border border-white/10 rounded-lg bg-white/[0.02]">
          {items.map((item) => {
            const isWriteup = item.type === "writeup";
            const logos = (isWriteup
              ? [item.home_abbr, item.away_abbr]
              : item.teams || []).filter((x): x is string => Boolean(x));
            const displayDate = item.published_at;
            const author = item.author || "Earl";
            return (
              <li key={isWriteup ? `w-${item.game_id}` : `a-${item.id}`}>
                <Link
                  href={item.link || "#"}
                  className="block px-5 py-4 hover:bg-white/[0.04] transition group"
                >
                  {/* Team logos — horizontal row above the content, left to right */}
                  {logos.length > 0 && (
                    <div className="flex items-center gap-1.5 mb-2">
                      {logos.slice(0, 4).map((abbr) => (
                        <TeamLogo key={abbr} abbr={abbr} sport={sport} size={26} />
                      ))}
                    </div>
                  )}
                  <div className="text-sm text-gray-500 mb-1">
                    {displayDate ? formatDate(displayDate) : "Recent"}
                    <span> · by {author}</span>
                  </div>
                  <div className="text-lg font-semibold group-hover:text-earl-400 transition">
                    {item.title}
                  </div>
                  {item.summary && (
                    <p className="text-sm text-gray-400 mt-1 line-clamp-2">{item.summary}</p>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      )}

      {/* Pagination */}
      {!loading && !error && pages > 1 && (
        <div className="flex items-center justify-between pt-2">
          <button
            onClick={() => goToPage(currentPage - 1)}
            disabled={currentPage <= 1}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-white/[0.04] border border-white/10 text-gray-300 hover:bg-white/[0.08] transition disabled:opacity-30 disabled:cursor-not-allowed"
          >
            ← Prev
          </button>
          <div className="text-sm text-gray-500">
            Page {currentPage} of {pages}
          </div>
          <button
            onClick={() => goToPage(currentPage + 1)}
            disabled={currentPage >= pages}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-white/[0.04] border border-white/10 text-gray-300 hover:bg-white/[0.08] transition disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Next →
          </button>
        </div>
      )}
    </div>
  );
}
