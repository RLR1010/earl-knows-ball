"use client";

import { useEffect, useState } from "react";
import * as React from "react";
import { useParams } from "next/navigation";
import Image from "next/image";
import Link from "next/link";
import { api, Team, Game, DepthChartEntry, BoxScore, formatSpread, formatSpreadAway, formatOverUnder } from "@/lib/api";
import { getTeamLogoUrl } from "@/lib/team_logos";

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
function predBadge(label: string, result: string | null): React.ReactNode {
  if (!result) return null;
  const resultLower = result.toLowerCase();
  let cls: string;
  let letter: string;
  if (resultLower === "win") {
    cls = "bg-green-900/40 text-green-400 border border-green-500/30";
    letter = "W";
  } else if (resultLower === "push") {
    cls = "bg-gray-700/40 text-gray-400 border border-gray-600/30";
    letter = "P";
  } else {
    cls = "bg-red-900/40 text-red-400 border border-red-500/30";
    letter = "L";
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider ${cls}`}>
      {label} {letter}
    </span>
  );
}

function formatGameDate(iso: string) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    weekday: "short", month: "short", day: "numeric", year: "numeric",
    timeZone: "America/Chicago",
  });
}

function formatGameTime(iso: string) {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric", minute: "2-digit", timeZone: "America/Chicago",
  });
}

function formatYards(yds: number): string {
  if (yds >= 1000) return (yds / 1000).toFixed(1) + "k";
  return yds.toFixed(0);
}

type Tab = "schedule" | "depth-chart" | "news" | "roster";

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
  const sport = routeParams?.sport || "nfl";
  const abbr = routeParams?.abbreviation?.toLowerCase() || "";
  const abbrUpper = abbr.toUpperCase();
  const teams = getTeamsForSport(sport);
  const meta = teams[abbr];

  const [team, setTeam] = useState<Team | null>(null);
  const [loading, setLoading] = useState(true);
  const [games, setGames] = useState<any[]>([]);
  const [gamesLoading, setGamesLoading] = useState(false);
  const isMLB = sport === "mlb";

  // NBA schedule day-by-day state
  const [nbaDate, setNbaDate] = useState(() => {
    const d = new Date();
    const offset = d.getTimezoneOffset();
    const local = new Date(d.getTime() - offset * 60_000);
    return local.toISOString().slice(0, 10);
  });

  const MLB_MONTHS = ["March","April","May","June","July","August","September","October"];
  function currentMlbMonthIndex(): number {
    const now = new Date();
    const m = now.getMonth() + 1; // 1-12
    if (m >= 3 && m <= 10) return m - 3; // Mar=0, Apr=1, ..., Oct=7
    return 0; // Default March if offseason
  }
  const [mlbMonthIdx, setMlbMonthIdx] = useState(currentMlbMonthIndex());
  const [depthChart, setDepthChart] = useState<DepthChartEntry[]>([]);
  const [depthLoading, setDepthLoading] = useState(false);
  const [tab, setTab] = useState<Tab>("schedule");
  const [seasonYear, setSeasonYear] = useState(2026);
  const [error, setError] = useState("");
  const [availableYears, setAvailableYears] = useState<number[]>([]);


  // Fetch available seasons
  useEffect(() => {
    if (isMLB) {
      const mlbYears = [2026,2025,2024,2023,2022,2021,2020,2019,2018,2017,2016,2015,2014,2013,2012,2011,2010,2009,2008,2007,2006];
      setAvailableYears(mlbYears);
      if (!mlbYears.includes(seasonYear)) setSeasonYear(2026);
    } else {
      api.seasons.list().then((years) => {
        setAvailableYears(years);
        if (years.length > 0 && !years.includes(seasonYear)) setSeasonYear(years[0]);
      }).catch(() => {});
    }
  }, [isMLB]);

  // Fetch team data
  useEffect(() => {
    if (!abbr) return;
    setLoading(true);
    setError("");
    api.teams.getByAbbr(abbrUpper).then(setTeam).catch(() => setError("Team not found in database.")).finally(() => setLoading(false));
  }, [abbr]);

  // Fetch games on season or date change
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
      if (!team) return;
      setGamesLoading(true);
      fetch(`/api/nba/games?year=${seasonYear}&team_abbr=${abbrUpper}&date=${nbaDate}`)
        .then(r => r.json())
        .then(setGames)
        .catch(() => setGames([]))
        .finally(() => setGamesLoading(false));
    } else {
      if (!team) return;
      setGamesLoading(true);
      api.games.list({ season_year: seasonYear, team_id: team.id }).then(setGames).catch(() => setGames([])).finally(() => setGamesLoading(false));
    }
  }, [team, seasonYear, nbaDate, isMLB, sport, abbrUpper]);

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
          <div className="w-14 h-14 rounded-xl flex items-center justify-center shrink-0 bg-white/10 p-1.5">
            {sport === "nfl" ? (
              <Image src={`/logos/${abbrUpper}.png`} alt={meta.name} width={56} height={56} className="object-contain w-full h-full" />
            ) : (
              <img src={getTeamLogoUrl(abbrUpper, sport) || undefined} alt={meta.name} width={56} height={56} className="object-contain w-full h-full" style={{ filter: 'brightness(1.1)' }} />
            )}
          </div>
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
              {availableYears.map(yr => <option key={yr} value={yr} className="bg-gray-900">{yr} Season</option>)}
            </select>
            <span className="text-xs text-gray-500">{games.length} game{games.length !== 1 ? "s" : ""}</span>
          </div>

          {gamesLoading ? (
            <div className="text-center py-16 text-gray-500">Loading games...</div>
          ) : sport === "nba" ? (
            <NBATeamSchedule
              games={games}
              loading={gamesLoading}
              sport={sport}
              abbrUpper={abbrUpper}
              seasonYear={seasonYear}
              nbaDate={nbaDate}
              setNbaDate={setNbaDate}
              formatGameDate={formatGameDate}
              formatGameTime={formatGameTime}
            />
          ) : (
            <NFLMLBTeamSchedule games={games} sport={sport} abbrUpper={abbrUpper} seasonYear={seasonYear} formatGameDate={formatGameDate} formatGameTime={formatGameTime} isMLB={isMLB} />
          )}
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
  games: any[];
  loading: boolean;
  sport: string;
  abbrUpper: string;
  seasonYear: number;
  nbaDate: string;
  setNbaDate: (d: string) => void;
  formatGameDate: (d: string) => string;
  formatGameTime: (d: string) => string;
}

function NBATeamSchedule({ games, loading, sport, abbrUpper, seasonYear, nbaDate, setNbaDate, formatGameDate, formatGameTime }: NBATeamScheduleProps) {
  return (
    <div className="space-y-4">
      {/* Date navigation */}
      <div className="flex items-center gap-3 flex-wrap">
        <button
          onClick={() => {
            const d = new Date(nbaDate + "T12:00:00");
            d.setDate(d.getDate() - 1);
            const offset = d.getTimezoneOffset();
            const local = new Date(d.getTime() - offset * 60_000);
            setNbaDate(local.toISOString().slice(0, 10));
          }}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 transition"
        >
          ← Previous Day
        </button>

        <input
          type="date"
          value={nbaDate}
          onChange={e => setNbaDate(e.target.value)}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500 [color-scheme:dark]"
        />

        <button
          onClick={() => {
            const d = new Date();
            const offset = d.getTimezoneOffset();
            const local = new Date(d.getTime() - offset * 60_000);
            setNbaDate(local.toISOString().slice(0, 10));
          }}
          className="px-3 py-1.5 rounded-lg bg-earl-600/20 border border-earl-500/30 text-xs text-earl-400 hover:bg-earl-600/30 transition"
        >
          Today
        </button>

        <button
          onClick={() => {
            const d = new Date(nbaDate + "T12:00:00");
            d.setDate(d.getDate() + 1);
            const offset = d.getTimezoneOffset();
            const local = new Date(d.getTime() - offset * 60_000);
            setNbaDate(local.toISOString().slice(0, 10));
          }}
          className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-gray-300 hover:bg-white/10 transition"
        >
          Next Day →
        </button>
      </div>

      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      ) : games.length === 0 ? (
        <div className="text-center py-12 text-gray-500">No games for {abbrUpper} on this date.</div>
      ) : (
        games.map(g => {
          const isFinal = g.status === "final";
          const isHome = g.home_team === abbrUpper;
          const oppScore = isHome ? g.away_score : g.home_score;
          const teamScore = isHome ? g.home_score : g.away_score;
          const won = isFinal && teamScore != null && oppScore != null && teamScore > oppScore;
          const lost = isFinal && teamScore != null && oppScore != null && teamScore < oppScore;
          const opponent = isHome ? g.away_team : g.home_team;
          return (
            <Link key={g.id} href={"/" + sport + "/games/" + g.id + "?year=" + seasonYear + "&date=" + nbaDate}
              className="block w-full text-left border rounded-xl p-4 transition hover:border-earl-500/50 hover:bg-earl-600/10 border-white/10 bg-white/5 hover:bg-white/10"
            >
              <div className="flex items-center justify-between">
                <div className="w-24 shrink-0">
                  <div className="text-xs text-gray-500 mt-0.5">{formatGameDate(g.date)}</div>
                </div>
                <div className="flex-1 flex items-center justify-center gap-4">
                  <div className="flex items-center gap-2">
                    <div className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center shrink-0 overflow-hidden">
                      <img src={getTeamLogoUrl(opponent, sport) || undefined} alt={opponent} width={24} height={24} className="object-contain" style={{ filter: "brightness(1.1)" }} />
                    </div>
                    <span className={"text-sm font-semibold " + (!isHome ? "text-white" : "text-gray-400")}>{opponent}</span>
                    {isFinal && <span className={"text-base font-bold " + (won ? "text-earl-400" : lost ? "text-red-400" : "text-white")}>{!isHome ? g.away_score : g.home_score}</span>}
                  </div>
                  <div className="text-center min-w-[60px]">
                    {isFinal ? (
                      <span className={"text-[10px] font-bold uppercase tracking-wider " + (won ? "text-green-400" : lost ? "text-red-400" : "text-gray-500")}>
                        {won ? "W" : lost ? "L" : ""}
                      </span>
                    ) : (
                      <span className="text-[10px] text-gray-500">{formatGameTime(g.date)}</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    {isFinal && <span className={"text-base font-bold " + (won && isHome ? "text-earl-400" : lost && isHome ? "text-red-400" : "text-white")}>{isHome ? g.home_score : g.away_score}</span>}
                    <span className={"text-sm font-semibold " + (isHome ? "text-white" : "text-gray-400")}>{isHome ? abbrUpper : opponent}</span>
                    <div className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center shrink-0 overflow-hidden">
                      <img src={getTeamLogoUrl(isHome ? g.home_team : g.away_team, sport) || undefined} alt={abbrUpper} width={24} height={24} className="object-contain" style={{ filter: "brightness(1.1)" }} />
                    </div>
                  </div>
                </div>
                <div className="w-28 shrink-0 text-right">
                  {!isFinal ? <div className="text-xs text-gray-500">{formatGameTime(g.date)}</div> : <div className="text-[10px] text-gray-500">FINAL</div>}
                </div>
              </div>
              {/* Betting Line */}
              {g.spread != null && (
                <div className="mt-3 text-center">
                  <span className="inline-block px-4 py-1.5 rounded-lg bg-gradient-to-r from-earl-700/30 via-earl-600/40 to-earl-700/30 border border-earl-500/40 text-sm font-bold tracking-wide">
                    <span className="text-earl-300">{isHome ? formatSpreadAway(g.spread, opponent) : formatSpread(g.spread, opponent)}</span>
                    <span className="mx-3 text-gray-600">|</span>
                    <span className="text-earl-400">{isHome ? formatSpread(g.spread, abbrUpper) : formatSpreadAway(g.spread, abbrUpper)}</span>
                    {g.over_under != null && (
                      <>
                        <span className="mx-3 text-gray-600">|</span>
                        <span className="text-gray-200">{formatOverUnder(g.over_under)}</span>
                      </>
                    )}
                  </span>
                </div>
              )}
            </Link>
          );
        })
      )}
    </div>
  );
}

interface TeamNewsProps {
  sport: string;
  abbreviation: string;
}

interface TeamArticle {
  id: number;
  title: string;
  excerpt: string | null;
  source_name: string | null;
  source_url: string | null;
  category: string | null;
  author: string | null;
  published_at: string | null;
}

// ── NFL/MLB Team Schedule Component ──────────────────────────────────
interface NFLMLBTeamScheduleProps {
  games: any[];
  sport: string;
  abbrUpper: string;
  seasonYear: number;
  formatGameDate: (d: string) => string;
  formatGameTime: (d: string) => string;
  isMLB: boolean;
}

function NFLMLBTeamSchedule({ games, sport, abbrUpper, seasonYear, formatGameDate, formatGameTime, isMLB }: NFLMLBTeamScheduleProps) {
  const MLB_MONTHS = ["March","April","May","June","July","August","September","October"];
  const [mlbMonthIdx, setMlbMonthIdx] = useState(() => {
    const now = new Date();
    const m = now.getMonth() + 1;
    if (m >= 3 && m <= 10) return m - 3;
    return 0;
  });

  if (isMLB) {
    const filterMonth = mlbMonthIdx + 3;
    const monthGames = games.filter((g: any) => {
      const d = new Date(g.date);
      return d.getUTCMonth() + 1 === filterMonth;
    });

    return (
      <div className="space-y-4">
        <div className="flex items-center gap-2">
          <button onClick={() => setMlbMonthIdx(i => Math.max(0, i - 1))} disabled={mlbMonthIdx === 0}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
          >← {MLB_MONTHS[mlbMonthIdx - 1] || ""}</button>
          <span className="text-sm font-semibold text-white px-4">{MLB_MONTHS[mlbMonthIdx]}</span>
          <button onClick={() => setMlbMonthIdx(i => Math.min(MLB_MONTHS.length - 1, i + 1))} disabled={mlbMonthIdx === MLB_MONTHS.length - 1}
            className="px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-gray-300 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed transition"
          >{MLB_MONTHS[mlbMonthIdx + 1] || ""} →</button>
        </div>

        {monthGames.length === 0 ? (
          <div className="text-center py-8 text-gray-500">No games in {MLB_MONTHS[mlbMonthIdx]}.</div>
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
                    <img src={getTeamLogoUrl(opponent, "mlb") || undefined} alt={opponent} width={24} height={24} className="object-contain shrink-0" style={{ filter: "brightness(1.1)" }} />
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

                  {/* Betting line */}
                  {g.spread != null && (
                    <div className="mt-2 pt-2 border-t border-white/10 text-xs">
                      <span className="text-earl-300">{isHome ? formatSpreadAway(g.spread, g.away_team) : formatSpread(g.spread, g.home_team)}</span>
                      <span className="mx-2 text-gray-700">|</span>
                      <span className="text-earl-400">{isHome ? formatSpread(g.spread, g.home_team) : formatSpreadAway(g.spread, g.away_team)}</span>
                      {g.over_under != null && (
                        <><span className="mx-2 text-gray-700">|</span><span className="text-gray-400">{formatOverUnder(g.over_under)}</span></>
                      )}
                    </div>
                  )}

                  {/* Prediction badges — RL | OU | ML */}
                  {(g.pred_ml_result || g.pred_rl_result || g.pred_ou_result) && (
                    <div className="mt-2 pt-2 border-t border-white/10 flex items-center justify-center gap-2">
                      {predBadge("RL", g.pred_rl_result)}
                      {predBadge("OU", g.pred_ou_result)}
                      {predBadge("ML", g.pred_ml_result)}
                    </div>
                  )}
                </Link>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // NFL schedule: flat list
  return (
    <div className="space-y-2">
      {games.map(g => {
        const isFinal = g.status === "final";
        const isHome = g.home_team === abbrUpper;
        const teamScore = isHome ? g.home_score : g.away_score;
        const oppScore = isHome ? g.away_score : g.home_score;
        const won = isFinal && teamScore != null && oppScore != null && teamScore > oppScore;
        const lost = isFinal && teamScore != null && oppScore != null && teamScore < oppScore;
        return (
          <Link key={g.id} href={"/" + sport + "/games/" + g.id}
            className={"block w-full text-left border rounded-xl p-4 transition hover:border-earl-500/50 hover:bg-earl-600/10 border-white/10 bg-white/5 hover:bg-white/10"}
          >
            <div className="flex items-center justify-between">
              <div className="w-24 shrink-0">
                <div className="text-xs font-semibold text-gray-500">{g.week ? "Week " + g.week : ""}</div>
                <div className="text-xs text-gray-500 mt-0.5">{formatGameDate(g.date)}</div>
              </div>
              <div className="flex-1 flex items-center justify-center gap-4">
                <div className="flex items-center gap-2">
                  <div className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center shrink-0 overflow-hidden">
                    <Image src={"/logos/" + g.away_team + ".png"} alt={g.away_team || ""} width={24} height={24} className="object-contain" />
                  </div>
                  <span className={"text-sm font-semibold " + (g.away_team === abbrUpper ? "text-white" : "text-gray-400")}>{g.away_team}</span>
                  {isFinal && <span className={"text-base font-bold " + (won && !isHome ? "text-earl-400" : lost && !isHome ? "text-red-400" : "text-white")}>{g.away_score}</span>}
                </div>
                <div className="text-center min-w-[60px]">
                  {isFinal ? (
                    <span className={"text-[10px] font-bold uppercase tracking-wider " + (won ? "text-green-400" : lost ? "text-red-400" : "text-gray-500")}>
                      {won ? "W" : lost ? "L" : "TIE"}
                    </span>
                  ) : <span className="text-[10px] text-gray-500">vs</span>}
                </div>
                <div className="flex items-center gap-2">
                  {isFinal && <span className={"text-base font-bold " + (won && isHome ? "text-earl-400" : lost && isHome ? "text-red-400" : "text-white")}>{g.home_score}</span>}
                  <span className={"text-sm font-semibold " + (g.home_team === abbrUpper ? "text-white" : "text-gray-400")}>{g.home_team}</span>
                  <div className="w-6 h-6 rounded-full bg-white/10 flex items-center justify-center shrink-0 overflow-hidden">
                    <Image src={"/logos/" + g.home_team + ".png"} alt={g.home_team || ""} width={24} height={24} className="object-contain" />
                  </div>
                </div>
              </div>
              <div className="w-28 shrink-0 text-right">
                {!isFinal ? <div className="text-xs text-gray-500">{formatGameTime(g.date)}</div> : <div className="text-[10px] text-gray-500">FINAL</div>}
              </div>
            </div>
            {g.spread != null && (
              <div className="mt-3 text-center">
                <span className="inline-block px-4 py-1.5 rounded-lg bg-gradient-to-r from-earl-700/30 via-earl-600/40 to-earl-700/30 border border-earl-500/40 text-sm font-bold tracking-wide">
                  <span className="text-earl-300">{formatSpreadAway(g.spread, g.away_team || "")}</span>
                  <span className="mx-3 text-gray-600">|</span>
                  <span className="text-earl-400">{formatSpread(g.spread, g.home_team || "")}</span>
                  {g.over_under != null && (<><span className="mx-3 text-gray-600">|</span><span className="text-gray-200">{formatOverUnder(g.over_under)}</span></>)}
                </span>
              </div>
            )}
          </Link>
        );
      })}
    </div>
  );
}

// ── END NFL/MLB Team Schedule Component ──────────────────────────────


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
