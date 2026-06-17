"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";

// ── NFL Types ─────────────────────────────────────────────────────

interface PlayerStatRow {
  player_id: number; player_name: string; position: string; team_abbr: string | null;
  games: number; pass_attempts: number; pass_completions: number;
  pass_yards: number; pass_tds: number; pass_int: number; comp_pct: number;
  yards_per_att: number; passer_rating: number; rush_attempts: number;
  rush_yards: number; rush_tds: number; yards_per_carry: number;
  targets: number; receptions: number; receiving_yards: number;
  receiving_tds: number; yards_per_rec: number; fumbles: number;
  fumbles_lost: number; fantasy_points_ppr: number;
  fantasy_points_std: number; fantasy_points_half: number; snaps_offense: number;
}

interface TeamStatRow {
  team_id: number; team_name: string; team_abbr: string;
  conference: string; division: string; games: number; wins: number;
  losses: number; ties: number; points_for: number; points_against: number;
  point_diff: number; yds_for: number; yds_against: number; yds_diff: number;
  pass_yds_for: number; pass_yds_against: number; rush_yds_for: number;
  rush_yds_against: number; to_takeaways: number; to_giveaways: number; to_margin: number;
}

// ── MLB Types ─────────────────────────────────────────────────────

interface MLBBattingRow {
  player_id: number; player_name: string; position: string; team_abbr: string | null;
  games_played: number; plate_appearances: number; at_bats: number; runs: number;
  hits: number; doubles: number; triples: number; home_runs: number;
  runs_batted_in: number; stolen_bases: number; caught_stealing: number;
  base_on_balls: number; intentional_walks: number; strikeouts: number;
  hit_by_pitch: number; sacrifice_flies: number;
  avg: number | null; obp: number | null; slg: number | null; ops: number | null;
  babip: number | null; total_bases: number; ground_into_double_play: number;
}

interface MLBPitchingRow {
  player_id: number; player_name: string; position: string; team_abbr: string | null;
  games_played: number; games_started: number; wins: number; losses: number;
  saves: number; blown_saves: number; holds: number;
  innings_pitched: number; hits: number; runs: number; earned_runs: number;
  home_runs: number; base_on_balls: number; strikeouts: number;
  era: number | null; whip: number | null; avg: number | null;
  obp: number | null; slg: number | null; ops: number | null;
  strikeouts_per_9: number | null; walks_per_9: number | null;
  strikeout_walk_ratio: number | null;
  complete_games: number; shutouts: number; batters_faced: number;
  hit_by_pitch: number; wild_pitches: number;
}

interface StatsResponse<T> {
  data: T[]; total: number; limit: number; offset: number; sort: string; order: string;
}

// ── Helpers ────────────────────────────────────────────────────────────

function fmt(val: number | null, decimals = 0): string {
  if (val === null || val === undefined) return "-";
  return val.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtRate(val: number | null): string {
  if (val === null || val === undefined) return "-";
  return val.toFixed(3).replace(/^0\./, ".");
}

function sortIcon(sort: string, col: string, order: string): string {
  if (sort !== col) return " ↕";
  return order === "desc" ? " ↓" : " ↑";
}

// ════════════════════════════════════════════════════════════════════
// NFL Stats
// ════════════════════════════════════════════════════════════════════

const NFL_POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K"];
const NFL_YEARS = Array.from({ length: 22 }, (_, i) => 2026 - i);

const NFL_PLAYER_COLS = [
  { key: "player_name", label: "Player", align: "left" as const },
  { key: "position", label: "Pos", align: "left" as const },
  { key: "team_abbr", label: "Team", align: "left" as const },
  { key: "games", label: "GP", align: "right" as const },
  { key: "pass_attempts", label: "Att", align: "right" as const },
  { key: "pass_completions", label: "Cmp", align: "right" as const },
  { key: "pass_yards", label: "Yds", align: "right" as const },
  { key: "pass_tds", label: "TD", align: "right" as const },
  { key: "pass_int", label: "Int", align: "right" as const },
  { key: "rush_yards", label: "RushYds", align: "right" as const },
  { key: "rush_tds", label: "RushTD", align: "right" as const },
  { key: "receptions", label: "Rec", align: "right" as const },
  { key: "receiving_yards", label: "RecYds", align: "right" as const },
  { key: "receiving_tds", label: "RecTD", align: "right" as const },
  { key: "fantasy_points_ppr", label: "FantPt", align: "right" as const },
];

const NFL_TEAM_COLS = [
  { key: "team_name", label: "Team", align: "left" as const },
  { key: "conference", label: "Conf", align: "left" as const },
  { key: "division", label: "Div", align: "left" as const },
  { key: "wins", label: "W", align: "right" as const },
  { key: "losses", label: "L", align: "right" as const },
  { key: "point_diff", label: "Diff", align: "right" as const },
  { key: "yds_for", label: "Yds/G", align: "right" as const },
  { key: "yds_against", label: "YdsA/G", align: "right" as const },
  { key: "to_margin", label: "TO Diff", align: "right" as const },
];

function NFLStats({ sport }: { sport: string }) {
  const [tab, setTab] = useState<"players" | "teams">("players");
  const [year, setYear] = useState(2025);
  const [position, setPosition] = useState("ALL");
  const [sort, setSort] = useState("pass_yards");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [playerData, setPlayerData] = useState<PlayerStatRow[]>([]);
  const [teamData, setTeamData] = useState<TeamStatRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [minGames, setMinGames] = useState(1);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    try {
      if (tab === "players") {
        const p = new URLSearchParams({ year: String(year), sort, order, limit: "200", offset: "0", min_games: String(minGames) });
        if (position !== "ALL") p.set("position", position);
        const res = await fetch(`/api/stats/players?${p}`);
        const json: StatsResponse<PlayerStatRow> = await res.json();
        setPlayerData(json.data); setTotal(json.total);
      } else {
        const p = new URLSearchParams({ year: String(year), sort, order, limit: "32", offset: "0" });
        const res = await fetch(`/api/stats/teams?${p}`);
        const json: StatsResponse<TeamStatRow> = await res.json();
        setTeamData(json.data); setTotal(json.total);
      }
    } catch (e) { console.error(e); } finally { setLoading(false); }
  }, [tab, year, position, sort, order, minGames]);

  useEffect(() => { fetchStats(); }, [fetchStats]);

  const handleSort = (col: string) => {
    if (sort === col) setOrder(order === "desc" ? "asc" : "desc");
    else { setSort(col); setOrder("desc"); }
  };

  return (
    <>
      {/* Tabs */}
      <div className="flex gap-2 border-b border-white/10 pb-3">
        {(["players", "teams"] as const).map((t) => (
          <button key={t} onClick={() => { setTab(t); setSort(t === "players" ? "pass_yards" : "wins"); setOrder("desc"); }}
            className={`px-5 py-2 rounded-t-lg text-sm font-semibold transition ${tab === t ? "bg-white/10 text-white border-b-2 border-earl-500" : "text-gray-400 hover:text-gray-200"}`}>
            {t === "players" ? "Player Stats" : "Team Stats"}
          </button>
        ))}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <select value={year} onChange={e => setYear(Number(e.target.value))}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500">
          {NFL_YEARS.map(y => <option key={y} value={y} className="text-black">{y}</option>)}
        </select>
        {tab === "players" && (
          <>
            <div className="flex gap-1">
              {NFL_POSITIONS.map(p => (
                <button key={p} onClick={() => setPosition(p)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition ${position === p ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400 hover:bg-white/10"}`}>{p}</button>
              ))}
            </div>
            <div className="flex items-center gap-2 ml-auto">
              <label className="text-xs text-gray-500">Min GP:</label>
              <select value={minGames} onChange={e => setMinGames(Number(e.target.value))}
                className="px-2 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-white focus:outline-none focus:border-earl-500">
                {[1, 2, 4, 6, 8, 10, 12, 14, 16].map(n => <option key={n} value={n} className="text-black">{n}</option>)}
              </select>
            </div>
          </>
        )}
        <span className="text-xs text-gray-500 ml-auto">{total} results</span>
      </div>

      {/* Player table */}
      {tab === "players" && (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full text-xs">
            <thead>
              <tr className="bg-white/5 text-gray-400 uppercase text-[10px] tracking-wider">
                {NFL_PLAYER_COLS.map(c => (
                  <th key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"} cursor-pointer hover:text-white select-none ${["player_name","position","team_abbr","games"].includes(c.key) ? "sticky left-0 bg-[#0a0a0f] z-10" : ""}`}
                    onClick={() => handleSort(c.key)}>
                    {c.label}{sortIcon(sort, c.key, order)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? <tr><td colSpan={16} className="text-center py-12 text-gray-500">Loading...</td></tr>
              : playerData.length === 0 ? <tr><td colSpan={16} className="text-center py-12 text-gray-500">No stats found.</td></tr>
              : playerData.map(r => (
                <tr key={r.player_id} className="border-t border-white/5 hover:bg-white/5">
                  <td className="px-3 py-2 sticky left-0 bg-[#0a0a0f]"><Link href={`/${sport}/players/${r.player_id}`} className="font-medium hover:text-earl-400 whitespace-nowrap">{r.player_name}</Link></td>
                  <td className="px-3 py-2 text-earl-400 font-semibold">{r.position}</td>
                  <td className="px-3 py-2">{r.team_abbr || "FA"}</td>
                  <td className="px-3 py-2 text-right text-gray-400">{r.games}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.pass_attempts)}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.pass_completions)}</td>
                  <td className="px-3 py-2 text-right font-semibold">{fmt(r.pass_yards)}</td>
                  <td className="px-3 py-2 text-right text-green-400">{r.pass_tds}</td>
                  <td className="px-3 py-2 text-right text-red-400">{r.pass_int}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.rush_yards)}</td>
                  <td className="px-3 py-2 text-right text-green-400">{r.rush_tds}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.receptions)}</td>
                  <td className="px-3 py-2 text-right">{fmt(r.receiving_yards)}</td>
                  <td className="px-3 py-2 text-right text-green-400">{r.receiving_tds}</td>
                  <td className="px-3 py-2 text-right font-semibold text-earl-400">{r.fantasy_points_ppr?.toFixed(1) ?? "-"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Team table */}
      {tab === "teams" && (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-white/5 text-gray-400 uppercase text-xs tracking-wider">
                {NFL_TEAM_COLS.map(c => (
                  <th key={c.key} className={`px-4 py-3 ${c.align === "right" ? "text-right" : "text-left"} cursor-pointer hover:text-white`} onClick={() => handleSort(c.key)}>
                    {c.label}{sortIcon(sort, c.key, order)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? <tr><td colSpan={9} className="text-center py-12 text-gray-500">Loading...</td></tr>
              : teamData.map(r => (
                <tr key={r.team_id} className="border-t border-white/5 hover:bg-white/5">
                  <td className="px-4 py-3"><Link href={`/${sport}/teams/${r.team_abbr.toLowerCase()}`} className="font-medium hover:text-earl-400">{r.team_name}</Link></td>
                  <td className="px-4 py-3">{r.conference}</td>
                  <td className="px-4 py-3 text-gray-400">{r.division}</td>
                  <td className="px-4 py-3 text-right text-green-400 font-semibold">{r.wins}</td>
                  <td className="px-4 py-3 text-right text-red-400">{r.losses}</td>
                  <td className={`px-4 py-3 text-right font-semibold ${r.point_diff > 0 ? "text-green-400" : r.point_diff < 0 ? "text-red-400" : ""}`}>{r.point_diff > 0 ? "+" : ""}{r.point_diff}</td>
                  <td className="px-4 py-3 text-right">{fmtRate(r.yds_for)}</td>
                  <td className="px-4 py-3 text-right">{fmtRate(r.yds_against)}</td>
                  <td className={`px-4 py-3 text-right font-semibold ${r.to_margin > 0 ? "text-green-400" : r.to_margin < 0 ? "text-red-400" : ""}`}>{r.to_margin > 0 ? "+" : ""}{r.to_margin}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// MLB Stats
// ════════════════════════════════════════════════════════════════════

const MLB_YEARS = Array.from({ length: 22 }, (_, i) => 2026 - i); // 2005-2026
const MLB_POSITIONS = ["ALL", "P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH"];

const MLB_BATTING_COLS = [
  { key: "player_name", label: "Player", align: "left" as const },
  { key: "team_abbr", label: "Team", align: "left" as const },
  { key: "games_played", label: "G", align: "right" as const },
  { key: "at_bats", label: "AB", align: "right" as const },
  { key: "runs", label: "R", align: "right" as const },
  { key: "hits", label: "H", align: "right" as const },
  { key: "doubles", label: "2B", align: "right" as const },
  { key: "triples", label: "3B", align: "right" as const },
  { key: "home_runs", label: "HR", align: "right" as const },
  { key: "runs_batted_in", label: "RBI", align: "right" as const },
  { key: "stolen_bases", label: "SB", align: "right" as const },
  { key: "base_on_balls", label: "BB", align: "right" as const },
  { key: "strikeouts", label: "SO", align: "right" as const },
  { key: "avg", label: "AVG", align: "right" as const },
  { key: "obp", label: "OBP", align: "right" as const },
  { key: "slg", label: "SLG", align: "right" as const },
  { key: "ops", label: "OPS", align: "right" as const },
  { key: "ops_plus", label: "OPS+", align: "right" as const },
];

const MLB_PITCHING_COLS = [
  { key: "player_name", label: "Player", align: "left" as const },
  { key: "team_abbr", label: "Team", align: "left" as const },
  { key: "games_played", label: "G", align: "right" as const },
  { key: "games_started", label: "GS", align: "right" as const },
  { key: "wins", label: "W", align: "right" as const },
  { key: "losses", label: "L", align: "right" as const },
  { key: "saves", label: "SV", align: "right" as const },
  { key: "innings_pitched", label: "IP", align: "right" as const },
  { key: "hits", label: "H", align: "right" as const },
  { key: "earned_runs", label: "ER", align: "right" as const },
  { key: "home_runs", label: "HR", align: "right" as const },
  { key: "base_on_balls", label: "BB", align: "right" as const },
  { key: "strikeouts", label: "SO", align: "right" as const },
  { key: "era", label: "ERA", align: "right" as const },
  { key: "whip", label: "WHIP", align: "right" as const },
  { key: "strikeouts_per_9", label: "K/9", align: "right" as const },
  { key: "walks_per_9", label: "BB/9", align: "right" as const },
  { key: "strikeout_walk_ratio", label: "K/BB", align: "right" as const },
];

const MLB_TEAM_COLS = [
  { key: "team_name", label: "Team", align: "left" as const },
  { key: "conference", label: "League", align: "left" as const },
  { key: "division", label: "Division", align: "left" as const },
  { key: "wins", label: "W", align: "right" as const },
  { key: "losses", label: "L", align: "right" as const },
  { key: "games", label: "G", align: "right" as const },
  { key: "points_for", label: "Runs", align: "right" as const },
  { key: "points_against", label: "RA", align: "right" as const },
];

function MLBStats({ sport }: { sport: string }) {
  const [tab, setTab] = useState<"batting" | "pitching" | "teams">("batting");
  const [year, setYear] = useState(2026);
  const [position, setPosition] = useState("ALL");
  const [sort, setSort] = useState("home_runs");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [batting, setBatting] = useState<MLBBattingRow[]>([]);
  const [pitching, setPitching] = useState<MLBPitchingRow[]>([]);
  const [teamData, setTeamData] = useState<TeamStatRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [minGames, setMinGames] = useState(1);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    try {
      if (tab === "batting") {
        const p = new URLSearchParams({ year: String(year), sort, order, limit: "500", offset: "0", min_games: String(minGames) });
        if (position !== "ALL") p.set("position", position.replace("ALL", ""));
        const res = await fetch(`/api/mlb/stats/batting?${p}`);
        const json: StatsResponse<MLBBattingRow> = await res.json();
        setBatting(json.data); setTotal(json.total);
      } else if (tab === "pitching") {
        const p = new URLSearchParams({ year: String(year), sort, order, limit: "500", offset: "0", min_games: String(minGames) });
        const res = await fetch(`/api/mlb/stats/pitching?${p}`);
        const json: StatsResponse<MLBPitchingRow> = await res.json();
        setPitching(json.data); setTotal(json.total);
      } else {
        const res = await fetch(`/api/mlb/stats/teams?year=${year}&sort=${sort}&order=${order}&limit=30&offset=0`);
        const json: StatsResponse<TeamStatRow> = await res.json();
        setTeamData(json.data); setTotal(30);
      }
    } catch (e) { console.error(e); } finally { setLoading(false); }
  }, [tab, year, position, sort, order, minGames]);

  useEffect(() => { fetchStats(); }, [fetchStats]);

  const handleSort = (col: string) => {
    if (sort === col) setOrder(order === "desc" ? "asc" : "desc");
    else { setSort(col); setOrder("desc"); }
  };

  const cols = tab === "batting" ? MLB_BATTING_COLS : tab === "pitching" ? MLB_PITCHING_COLS : MLB_TEAM_COLS;
  const rows: any[] = tab === "batting" ? batting : tab === "pitching" ? pitching : teamData;

  return (
    <>
      {/* Tabs */}
      <div className="flex gap-2 border-b border-white/10 pb-3">
        {(["batting", "pitching", "teams"] as const).map((t) => (
          <button key={t} onClick={() => { setTab(t); setSort(t === "batting" ? "home_runs" : t === "pitching" ? "era" : "wins"); setOrder(t === "pitching" ? "asc" : "desc"); }}
            className={`px-5 py-2 rounded-t-lg text-sm font-semibold transition capitalize ${tab === t ? "bg-white/10 text-white border-b-2 border-earl-500" : "text-gray-400 hover:text-gray-200"}`}>
            {t === "batting" ? "Batting Stats" : t === "pitching" ? "Pitching Stats" : "Team Standings"}
          </button>
        ))}
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <select value={year} onChange={e => setYear(Number(e.target.value))}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500">
          {MLB_YEARS.map(y => <option key={y} value={y} className="text-black">{y}</option>)}
        </select>
        {tab !== "teams" && (
          <>
            <div className="flex gap-1 flex-wrap">
              {MLB_POSITIONS.map(p => (
                <button key={p} onClick={() => setPosition(p)}
                  className={`px-2.5 py-1.5 rounded-lg text-xs font-semibold transition ${position === p ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400 hover:bg-white/10"}`}>{p}</button>
              ))}
            </div>
            <span className="text-xs text-gray-500 ml-auto">{total} results</span>
          </>
        )}
      </div>

      {/* Data table */}
      <div className="overflow-x-auto rounded-xl border border-white/10">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-white/5 text-gray-400 uppercase text-[10px] tracking-wider">
              {cols.map(c => (
                <th key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"} cursor-pointer hover:text-white select-none ${["player_name","team_abbr"].includes(c.key) ? "sticky left-0 bg-[#0a0a0f] z-10" : ""}`}
                  onClick={() => handleSort(c.key)}>
                  {c.label}{sortIcon(sort, c.key, order)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={cols.length} className="text-center py-12 text-gray-500">Loading...</td></tr>
            ) : rows.length === 0 ? (
              <tr><td colSpan={cols.length} className="text-center py-12 text-gray-500">No data yet — MLB stats ingestion is still running.</td></tr>
            ) : (
              rows.map((r: any, i: number) => (
                <tr key={r.player_id || r.team_id || i} className="border-t border-white/5 hover:bg-white/5 transition">
                  {cols.map(c => {
                    const val = r[c.key];
                    const pk = c.key === "player_name";
                    if (pk) {
                      return <td key={c.key} className="px-3 py-2 sticky left-0 bg-[#0a0a0f]">
                        <Link href={`/${sport}/players/${r.player_id}`} className="font-medium hover:text-earl-400 whitespace-nowrap">{val}</Link>
                      </td>;
                    }
                    if (c.key === "team_abbr" || c.key === "team_name") {
                      const linkTo = r.team_abbr ? `/${sport}/teams/${(r.team_abbr || "").toLowerCase()}` : null;
                      return <td key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"}`}>
                        {linkTo ? <Link href={linkTo} className="hover:text-earl-400">{val}</Link> : <span>{val || "-"}</span>}
                      </td>;
                    }
                    // Rate stats need special formatting
                    if (["avg", "obp", "slg", "ops", "babip"].includes(c.key)) {
                      return <td key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"} font-semibold`}>
                        {val !== null && val !== undefined ? val.toString().replace(/^0\./, ".") : "-"}
                      </td>;
                    }
                    if (["era", "whip"].includes(c.key)) {
                      return <td key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"}`}>{fmt(val, 2)}</td>;
                    }
                    if (["innings_pitched"].includes(c.key)) {
                      return <td key={c.key} className="px-3 py-2 text-right">{val !== null ? val : "-"}</td>;
                    }
                    // HR, RBI, etc.
                    if (c.key === "home_runs" || c.key === "runs_batted_in" || c.key === "wins" || c.key === "saves" || c.key === "strikeouts") {
                      return <td key={c.key} className={`px-3 py-2 text-right ${c.key === "home_runs" ? "font-semibold text-earl-400" : c.key === "wins" ? "text-green-400 font-semibold" : c.key === "saves" ? "text-amber-400 font-semibold" : ""}`}>{val ?? "-"}</td>;
                    }
                    if (c.key === "losses" || c.key === "earned_runs") {
                      return <td key={c.key} className="px-3 py-2 text-right text-red-400">{val ?? "-"}</td>;
                    }
                    return <td key={c.key} className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"} ${["doubles","triples","home_runs","runs_batted_in","stolen_bases","wins","saves","strikeouts","strikeouts_per_9"].includes(c.key) ? "font-semibold" : ""}`}>
                      {val !== null && val !== undefined ? val : "-"}
                    </td>;
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="text-center text-xs text-gray-600 pb-8">
        {loading ? "" : `Showing ${rows.length} of ${total} results`}
      </div>
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// NBA Stats
// ════════════════════════════════════════════════════════════════════

const NBA_YEARS = Array.from({ length: 22 }, (_, i) => 2026 - i);
const NBA_POSITIONS = ["ALL", "PG", "SG", "SF", "PF", "C", "G", "F"];

const NBA_COLS = [
  { key: "player_name", label: "Player", align: "left" as const },
  { key: "team_abbr", label: "Team", align: "left" as const },
  { key: "games_played", label: "GP", align: "right" as const },
  { key: "points_per_game", label: "PPG", align: "right" as const },
  { key: "points", label: "PTS", align: "right" as const },
  { key: "assists_per_game", label: "APG", align: "right" as const },
  { key: "rebounds_per_game", label: "RPG", align: "right" as const },
  { key: "assists", label: "AST", align: "right" as const },
  { key: "rebounds", label: "REB", align: "right" as const },
  { key: "steals", label: "STL", align: "right" as const },
  { key: "blocks", label: "BLK", align: "right" as const },
  { key: "field_goal_pct", label: "FG%", align: "right" as const },
  { key: "three_point_pct", label: "3P%", align: "right" as const },
  { key: "free_throw_pct", label: "FT%", align: "right" as const },
  { key: "turnovers", label: "TOV", align: "right" as const },
  { key: "minutes_played", label: "MIN", align: "right" as const },
  { key: "fantasy_points", label: "FanPt", align: "right" as const },
];

function NBAStats({ sport }: { sport: string }) {
  const [sort, setSort] = useState("points_per_game");
  const [order, setOrder] = useState<"asc" | "desc">("desc");
  const [year, setYear] = useState(2026);
  const [position, setPosition] = useState("ALL");
  const [data, setData] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [minGames, setMinGames] = useState(1);

  const fetchStats = useCallback(async () => {
    setLoading(true);
    try {
      const p = new URLSearchParams({
        year: String(year), sort, order, limit: "500", offset: "0", min_games: String(minGames),
      });
      if (position !== "ALL") p.set("position", position);
      const res = await fetch(`/api/nba/stats/players?${p}`);
      const json = await res.json();
      setData(json.data || []);
      setTotal(json.total || 0);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [year, position, sort, order, minGames]);

  useEffect(() => { fetchStats(); }, [fetchStats]);

  const handleSort = (col: string) => {
    if (sort === col) setOrder(order === "desc" ? "asc" : "desc");
    else { setSort(col); setOrder("desc"); }
  };

  return (
    <>
      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <select value={year} onChange={e => setYear(Number(e.target.value))}
          className="px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500">
          {NBA_YEARS.map(y => <option key={y} value={y} className="text-black">{y}</option>)}
        </select>
        <div className="flex gap-1 flex-wrap">
          {NBA_POSITIONS.map(p => (
            <button key={p} onClick={() => setPosition(p)}
              className={`px-2.5 py-1.5 rounded-lg text-xs font-semibold transition ${position === p ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400 hover:bg-white/10"}`}>{p}</button>
          ))}
        </div>
        <div className="flex items-center gap-2 ml-auto">
          <label className="text-xs text-gray-500">Min GP:</label>
          <select value={minGames} onChange={e => setMinGames(Number(e.target.value))}
            className="px-2 py-1.5 rounded-lg bg-white/5 border border-white/10 text-xs text-white focus:outline-none focus:border-earl-500">
            {[1, 2, 5, 10, 20, 41, 60].map(n => <option key={n} value={n} className="text-black">{n}</option>)}
          </select>
        </div>
        <span className="text-xs text-gray-500 ml-auto">{total} results</span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-xl border border-white/10">
        <table className="w-full text-xs">
          <thead>
            <tr className="bg-white/5 text-gray-400 uppercase text-[10px] tracking-wider">
              {NBA_COLS.map(c => (
                <th key={c.key}
                  className={`px-3 py-2 ${c.align === "right" ? "text-right" : "text-left"} cursor-pointer hover:text-white select-none ${["player_name","team_abbr"].includes(c.key) ? "sticky left-0 bg-[#0a0a0f] z-10" : ""}`}
                  onClick={() => handleSort(c.key)}>
                  {c.label}{sort === c.key ? <span className="text-earl-400 ml-0.5">{order === "desc" ? "↓" : "↑"}</span> : <span className="opacity-20 ml-0.5">↕</span>}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={NBA_COLS.length} className="text-center py-12 text-gray-500">Loading...</td></tr>
            ) : data.length === 0 ? (
              <tr><td colSpan={NBA_COLS.length} className="text-center py-12 text-gray-500">No data yet — NBA ingestion still running.</td></tr>
            ) : (
              data.map((r: any, i: number) => (
                <tr key={r.player_id || i} className="border-t border-white/5 hover:bg-white/5 transition">
                  {NBA_COLS.map(c => {
                    const val = r[c.key];
                    if (c.key === "player_name") {
                      return <td key={c.key} className="px-3 py-2 sticky left-0 bg-[#0a0a0f]">
                        <Link href={`/${sport}/players/${r.player_id}`} className="font-medium hover:text-earl-400 whitespace-nowrap">{val}</Link>
                      </td>;
                    }
                    if (c.key === "team_abbr") {
                      return <td key={c.key} className="px-3 py-2">
                        <Link href={`/${sport}/teams/${(val || "").toLowerCase()}`} className="hover:text-earl-400">{val || "-"}</Link>
                      </td>;
                    }
                    if (["field_goal_pct", "three_point_pct", "free_throw_pct"].includes(c.key)) {
                      return <td key={c.key} className="px-3 py-2 text-right">{val !== null ? (Number(val) * 100).toFixed(1) + "%" : "-"}</td>;
                    }
                    if (["points_per_game", "assists_per_game", "rebounds_per_game"].includes(c.key)) {
                      return <td key={c.key} className="px-3 py-2 text-right font-semibold text-earl-400">{val !== null ? Number(val).toFixed(1) : "-"}</td>;
                    }
                    return <td key={c.key} className={`px-3 py-2 text-right ${["points", "assists", "rebounds", "steals", "blocks", "fantasy_points"].includes(c.key) ? "font-semibold" : ""}`}>{val ?? "-"}</td>;
                  })}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="text-center text-xs text-gray-600 pb-8">
        {loading ? "" : `Showing ${data.length} of ${total} results`}
      </div>
    </>
  );
}

// ════════════════════════════════════════════════════════════════════
// Main Page
// ════════════════════════════════════════════════════════════════════

export default function StatsPage() {
  const params = useParams<{ sport: string }>();
  const sport = params?.sport || "nfl";

  return (
    <div className="space-y-6">
      <h1 className="font-display text-4xl font-bold">
        {sport.toUpperCase()} Stats
      </h1>
      {sport === "mlb" ? <MLBStats sport={sport} /> : sport === "nba" ? <NBAStats sport={sport} /> : <NFLStats sport={sport} />}
    </div>
  );
}
