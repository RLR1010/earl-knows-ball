"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import TeamLogo from "@/components/TeamLogo";

/**
 * Yahoo-style MLB stats hub.
 *
 * Three views, mirroring https://sports.yahoo.com/mlb/stats/ :
 *   - League Leaders : top-5 cards by stat category, stacked in
 *                      MLB / American League / National League sections.
 *   - Player Stats   : full sortable stat table (Batting | Pitching)
 *                      with league / position / qualified filters.
 *   - Team Stats     : team season totals (Batting | Pitching).
 */

type View = "leaders" | "players" | "teams";

const MLB_YEARS = Array.from({ length: 21 }, (_, i) => 2026 - i); // 2006–2026
const POSITIONS_BAT = ["ALL", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH", "P"];
const LEAGUES: [string, string][] = [
  ["MLB", "MLB"],
  ["AL", "American League"],
  ["NL", "National League"],
];

// ── formatters ────────────────────────────────────────────────────────
const fmtInt = (v: any) => (v === null || v === undefined || v === "" ? "—" : String(Math.round(Number(v))));
const fmt2 = (v: any) => (v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(2));
const fmt3 = (v: any) => {
  if (v === null || v === undefined || v === "") return "—";
  const s = Number(v).toFixed(3);
  return s.startsWith("0.") ? s.slice(1) : s;
};
const fmtIp = (v: any) => (v === null || v === undefined || v === "" ? "—" : String(Number(v)));

type Col = { key: string; label: string; align?: "left" | "right"; fmt?: (v: any) => string };

const BAT_COLS: Col[] = [
  { key: "player_name", label: "Player", align: "left" },
  { key: "team_abbr", label: "Team", align: "left" },
  { key: "position", label: "Pos", align: "left" },
  { key: "games_played", label: "G", align: "right", fmt: fmtInt },
  { key: "plate_appearances", label: "PA", align: "right", fmt: fmtInt },
  { key: "at_bats", label: "AB", align: "right", fmt: fmtInt },
  { key: "runs", label: "R", align: "right", fmt: fmtInt },
  { key: "hits", label: "H", align: "right", fmt: fmtInt },
  { key: "doubles", label: "2B", align: "right", fmt: fmtInt },
  { key: "triples", label: "3B", align: "right", fmt: fmtInt },
  { key: "home_runs", label: "HR", align: "right", fmt: fmtInt },
  { key: "runs_batted_in", label: "RBI", align: "right", fmt: fmtInt },
  { key: "stolen_bases", label: "SB", align: "right", fmt: fmtInt },
  { key: "base_on_balls", label: "BB", align: "right", fmt: fmtInt },
  { key: "strikeouts", label: "SO", align: "right", fmt: fmtInt },
  { key: "avg", label: "AVG", align: "right", fmt: fmt3 },
  { key: "obp", label: "OBP", align: "right", fmt: fmt3 },
  { key: "slg", label: "SLG", align: "right", fmt: fmt3 },
  { key: "ops", label: "OPS", align: "right", fmt: fmt3 },
];

const PIT_COLS: Col[] = [
  { key: "player_name", label: "Player", align: "left" },
  { key: "team_abbr", label: "Team", align: "left" },
  { key: "games_played", label: "G", align: "right", fmt: fmtInt },
  { key: "games_started", label: "GS", align: "right", fmt: fmtInt },
  { key: "wins", label: "W", align: "right", fmt: fmtInt },
  { key: "losses", label: "L", align: "right", fmt: fmtInt },
  { key: "saves", label: "SV", align: "right", fmt: fmtInt },
  { key: "innings_pitched", label: "IP", align: "right", fmt: fmtIp },
  { key: "hits", label: "H", align: "right", fmt: fmtInt },
  { key: "earned_runs", label: "ER", align: "right", fmt: fmtInt },
  { key: "base_on_balls", label: "BB", align: "right", fmt: fmtInt },
  { key: "strikeouts", label: "SO", align: "right", fmt: fmtInt },
  { key: "home_runs", label: "HR", align: "right", fmt: fmtInt },
  { key: "era", label: "ERA", align: "right", fmt: fmt2 },
  { key: "whip", label: "WHIP", align: "right", fmt: fmt2 },
  { key: "strikeouts_per_9", label: "K/9", align: "right", fmt: fmt2 },
  { key: "avg", label: "AVG", align: "right", fmt: fmt3 },
];

const TEAM_BAT_COLS: Col[] = [
  { key: "team_abbr", label: "Team", align: "left" },
  { key: "games", label: "G", align: "right", fmt: fmtInt },
  { key: "runs", label: "R", align: "right", fmt: fmtInt },
  { key: "hits", label: "H", align: "right", fmt: fmtInt },
  { key: "doubles", label: "2B", align: "right", fmt: fmtInt },
  { key: "triples", label: "3B", align: "right", fmt: fmtInt },
  { key: "home_runs", label: "HR", align: "right", fmt: fmtInt },
  { key: "rbi", label: "RBI", align: "right", fmt: fmtInt },
  { key: "stolen_bases", label: "SB", align: "right", fmt: fmtInt },
  { key: "base_on_balls", label: "BB", align: "right", fmt: fmtInt },
  { key: "strikeouts", label: "SO", align: "right", fmt: fmtInt },
  { key: "avg", label: "AVG", align: "right", fmt: fmt3 },
  { key: "obp", label: "OBP", align: "right", fmt: fmt3 },
  { key: "slg", label: "SLG", align: "right", fmt: fmt3 },
  { key: "ops", label: "OPS", align: "right", fmt: fmt3 },
];

const TEAM_PIT_COLS: Col[] = [
  { key: "team_abbr", label: "Team", align: "left" },
  { key: "games", label: "G", align: "right", fmt: fmtInt },
  { key: "wins", label: "W", align: "right", fmt: fmtInt },
  { key: "losses", label: "L", align: "right", fmt: fmtInt },
  { key: "saves", label: "SV", align: "right", fmt: fmtInt },
  { key: "innings_pitched", label: "IP", align: "right", fmt: fmtIp },
  { key: "hits", label: "H", align: "right", fmt: fmtInt },
  { key: "earned_runs", label: "ER", align: "right", fmt: fmtInt },
  { key: "base_on_balls", label: "BB", align: "right", fmt: fmtInt },
  { key: "strikeouts", label: "SO", align: "right", fmt: fmtInt },
  { key: "home_runs", label: "HR", align: "right", fmt: fmtInt },
  { key: "era", label: "ERA", align: "right", fmt: fmt2 },
  { key: "whip", label: "WHIP", align: "right", fmt: fmt2 },
  { key: "strikeouts_per_9", label: "K/9", align: "right", fmt: fmt2 },
];

const ASC_DEFAULT = new Set(["era", "whip", "avg"]);

function sortIcon(active: boolean, order: string) {
  if (!active) return <span className="text-gray-600 ml-0.5">↕</span>;
  return <span className="text-earl-400 ml-0.5">{order === "asc" ? "↑" : "↓"}</span>;
}

export default function MLBStatsYahoo({ sport }: { sport: string }) {
  const [view, setView] = useState<View>("leaders");
  const [year, setYear] = useState(2026);
  const [league, setLeague] = useState("MLB");

  // leaders
  const [leaders, setLeaders] = useState<any | null>(null);
  const [leadersLoading, setLeadersLoading] = useState(false);

  // players
  const [statType, setStatType] = useState<"batting" | "pitching">("batting");
  const [position, setPosition] = useState("ALL");
  const [qualify, setQualify] = useState(false);
  const [sort, setSort] = useState("home_runs");
  const [order, setOrder] = useState("desc");
  const [players, setPlayers] = useState<any[]>([]);
  const [total, setTotal] = useState(0);
  const [playersLoading, setPlayersLoading] = useState(false);

  // teams
  const [teamType, setTeamType] = useState<"batting" | "pitching">("batting");
  const [teamSort, setTeamSort] = useState("home_runs");
  const [teamOrder, setTeamOrder] = useState("desc");
  const [teams, setTeams] = useState<any[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);

  const loadLeaders = useCallback(async () => {
    setLeadersLoading(true);
    try {
      const res = await fetch(`/api/mlb/stats/leaders?year=${year}&limit=5`);
      setLeaders(res.ok ? await res.json() : null);
    } catch {
      setLeaders(null);
    } finally {
      setLeadersLoading(false);
    }
  }, [year]);

  const loadPlayers = useCallback(async () => {
    setPlayersLoading(true);
    try {
      const p = new URLSearchParams({
        year: String(year),
        league,
        position: statType === "batting" ? position : "ALL",
        qualify: String(qualify),
        sort,
        order,
        limit: "100",
        offset: "0",
      });
      const res = await fetch(`/api/mlb/stats/${statType}?${p}`);
      const j = res.ok ? await res.json() : { data: [], total: 0 };
      setPlayers(j.data || []);
      setTotal(j.total || 0);
    } catch {
      setPlayers([]);
      setTotal(0);
    } finally {
      setPlayersLoading(false);
    }
  }, [year, league, position, qualify, sort, order, statType]);

  const loadTeams = useCallback(async () => {
    setTeamsLoading(true);
    try {
      const res = await fetch(
        `/api/mlb/stats/team-${teamType}?year=${year}&sort=${teamSort}&order=${teamOrder}`
      );
      const j = res.ok ? await res.json() : { data: [] };
      setTeams(j.data || []);
    } catch {
      setTeams([]);
    } finally {
      setTeamsLoading(false);
    }
  }, [year, teamType, teamSort, teamOrder]);

  useEffect(() => {
    if (view === "leaders") loadLeaders();
  }, [view, loadLeaders]);
  useEffect(() => {
    if (view === "players") loadPlayers();
  }, [view, loadPlayers]);
  useEffect(() => {
    if (view === "teams") loadTeams();
  }, [view, loadTeams]);

  // when switching batting/pitching, reset the default sort column
  useEffect(() => {
    if (statType === "batting") {
      setSort("home_runs");
      setPosition("ALL");
    } else {
      setSort("era");
    }
    setOrder(statType === "batting" ? "desc" : "asc");
  }, [statType]);

  useEffect(() => {
    setTeamSort(teamType === "batting" ? "home_runs" : "era");
    setTeamOrder(teamType === "batting" ? "desc" : "asc");
  }, [teamType]);

  const cols = statType === "batting" ? BAT_COLS : PIT_COLS;
  const teamCols = teamType === "batting" ? TEAM_BAT_COLS : TEAM_PIT_COLS;

  const handleSort = (key: string) => {
    if (sort === key) setOrder(order === "desc" ? "asc" : "desc");
    else {
      setSort(key);
      setOrder(ASC_DEFAULT.has(key) ? "asc" : "desc");
    }
  };
  const handleTeamSort = (key: string) => {
    if (teamSort === key) setTeamOrder(teamOrder === "desc" ? "asc" : "desc");
    else {
      setTeamSort(key);
      setTeamOrder(ASC_DEFAULT.has(key) ? "asc" : "desc");
    }
  };

  const seeAll = (kind: "batting" | "pitching", statId: string, dir: string) => {
    setStatType(kind);
    setSort(statId);
    setOrder(dir);
    setView("players");
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const sectionBadge = (key: string) =>
    key === "mlb" ? "MLB" : key === "al" ? "AL" : key === "nl" ? "NL" : key.toUpperCase();

  return (
    <div className="space-y-6">
      {/* ── Top bar: view tabs + season ───────────────────────────── */}
      <div className="flex flex-wrap items-center gap-2 border-b border-white/10 pb-3">
        {([
          ["leaders", "League Leaders"],
          ["players", "Player Stats"],
          ["teams", "Team Stats"],
        ] as [View, string][]).map(([v, label]) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`px-4 py-2 rounded-t-lg text-sm font-semibold transition border-b-2 ${
              view === v
                ? "bg-white/10 text-white border-earl-500"
                : "text-gray-400 hover:text-gray-200 border-transparent"
            }`}
          >
            {label}
          </button>
        ))}
        <select
          value={year}
          onChange={(e) => setYear(Number(e.target.value))}
          className="ml-auto px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white focus:outline-none focus:border-earl-500"
        >
          {MLB_YEARS.map((y) => (
            <option key={y} value={y} className="text-black">
              {y}
            </option>
          ))}
        </select>
      </div>

      {/* ══ LEAGUE LEADERS ══════════════════════════════════════════ */}
      {view === "leaders" && (
        <div className="space-y-8">
          {leadersLoading && !leaders && (
            <div className="text-center py-20 text-gray-500">Loading leaders…</div>
          )}
          {leaders?.sections?.map((section: any) => (
            <section key={section.key} className="space-y-4">
              <div className="flex items-center gap-3">
                <span className="inline-flex items-center justify-center min-w-[38px] h-7 px-2 rounded-md bg-earl-600 text-white text-xs font-bold">
                  {sectionBadge(section.key)}
                </span>
                <h2 className="font-display text-2xl font-bold">{section.label}</h2>
              </div>
              {section.groups.map((group: any) => (
                <div key={group.key}>
                  <h3 className="text-xs uppercase tracking-widest text-gray-500 mb-2">
                    {group.label}
                  </h3>
                  <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                    {group.categories.map((cat: any) => (
                      <LeaderCard
                        key={cat.stat_id}
                        cat={cat}
                        sport={sport}
                        onSeeAll={() =>
                          seeAll(group.key, cat.stat_id, cat.format === "rate2" ? "asc" : "desc")
                        }
                      />
                    ))}
                  </div>
                </div>
              ))}
            </section>
          ))}
          {!leadersLoading && !leaders && (
            <div className="text-center py-20 text-gray-500">No leaderboard data.</div>
          )}
        </div>
      )}

      {/* ══ PLAYER STATS ════════════════════════════════════════════ */}
      {view === "players" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <Segmented
              options={[
                ["batting", "Batting"],
                ["pitching", "Pitching"],
              ]}
              value={statType}
              onChange={(v) => setStatType(v as "batting" | "pitching")}
            />
            <Segmented
              options={LEAGUES}
              value={league}
              onChange={setLeague}
            />
            <label className="flex items-center gap-2 text-xs text-gray-400 select-none cursor-pointer">
              <input
                type="checkbox"
                checked={qualify}
                onChange={(e) => setQualify(e.target.checked)}
                className="accent-earl-500"
              />
              Qualified only
            </label>
            <span className="text-xs text-gray-500 ml-auto">{total} players</span>
          </div>

          {statType === "batting" && (
            <div className="flex flex-wrap gap-1">
              {POSITIONS_BAT.map((p) => (
                <button
                  key={p}
                  onClick={() => setPosition(p)}
                  className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                    position === p
                      ? "bg-earl-600 text-white"
                      : "bg-white/5 text-gray-400 hover:bg-white/10"
                  }`}
                >
                  {p === "ALL" ? "All" : p}
                </button>
              ))}
            </div>
          )}

          <StatTable
            cols={cols}
            rows={players}
            loading={playersLoading}
            sport={sport}
            sort={sort}
            order={order}
            onSort={handleSort}
            linkPlayers
          />
        </div>
      )}

      {/* ══ TEAM STATS ══════════════════════════════════════════════ */}
      {view === "teams" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3">
            <Segmented
              options={[
                ["batting", "Batting"],
                ["pitching", "Pitching"],
              ]}
              value={teamType}
              onChange={(v) => setTeamType(v as "batting" | "pitching")}
            />
            <span className="text-xs text-gray-500 ml-auto">{teams.length} teams</span>
          </div>
          <StatTable
            cols={teamCols}
            rows={teams}
            loading={teamsLoading}
            sport={sport}
            sort={teamSort}
            order={teamOrder}
            onSort={handleTeamSort}
            teamRows
          />
        </div>
      )}
    </div>
  );
}

// ── sub-components ────────────────────────────────────────────────────

function LeaderCard({
  cat,
  sport,
  onSeeAll,
}: {
  cat: any;
  sport: string;
  onSeeAll: () => void;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-white/5 border-b border-white/10">
        <span className="text-sm font-semibold">{cat.label}</span>
        <span className="text-[10px] font-bold tracking-wider text-earl-400 px-1.5 py-0.5 rounded bg-earl-500/10">
          {cat.abbreviation}
        </span>
      </div>
      <ul className="divide-y divide-white/5">
        {(cat.leaders || []).map((l: any) => (
          <li key={`${cat.stat_id}-${l.rank}`} className="flex items-center gap-3 px-4 py-2 hover:bg-white/5">
            <span className="w-4 text-right text-xs text-gray-500">{l.rank}</span>
            <TeamLogo abbr={l.team_abbr || "FA"} sport={sport} size={20} />
            <Link
              href={`/${sport}/players/${l.player_id}`}
              className="flex-1 truncate text-sm hover:text-earl-400"
            >
              {l.player_name}
            </Link>
            <span className="text-xs text-gray-500 w-8">{l.team_abbr || "FA"}</span>
            <span className="text-sm font-semibold tabular-nums w-14 text-right">{l.display}</span>
          </li>
        ))}
        {(!cat.leaders || cat.leaders.length === 0) && (
          <li className="px-4 py-3 text-xs text-gray-500">No data</li>
        )}
      </ul>
      <button
        onClick={onSeeAll}
        className="w-full px-4 py-2 text-xs font-semibold text-earl-400 hover:text-earl-300 hover:bg-white/5 text-left border-t border-white/5"
      >
        See all {cat.label} →
      </button>
    </div>
  );
}

function Segmented({
  options,
  value,
  onChange,
}: {
  options: [string, string][];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div className="inline-flex rounded-lg overflow-hidden border border-white/10">
      {options.map(([v, label], i) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={`px-3.5 py-1.5 text-xs font-semibold transition ${
            value === v ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400 hover:bg-white/10"
          } ${i > 0 ? "border-l border-white/10" : ""}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function StatTable({
  cols,
  rows,
  loading,
  sport,
  sort,
  order,
  onSort,
  linkPlayers = false,
  teamRows = false,
}: {
  cols: Col[];
  rows: any[];
  loading: boolean;
  sport: string;
  sort: string;
  order: string;
  onSort: (k: string) => void;
  linkPlayers?: boolean;
  teamRows?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-white/10">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-white/5 text-gray-400 uppercase text-[10px] tracking-wider">
            {cols.map((c) => (
              <th
                key={c.key}
                onClick={() => onSort(c.key)}
                className={`px-3 py-2.5 cursor-pointer hover:text-white select-none whitespace-nowrap ${
                  c.align === "right" ? "text-right" : "text-left"
                } ${c.key === "player_name" || c.key === "team_abbr" ? "sticky left-0 bg-[#0a0a0f] z-10" : ""}`}
              >
                {c.label}
                {sortIcon(sort === c.key, order)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {loading ? (
            <tr>
              <td colSpan={cols.length} className="text-center py-14 text-gray-500">
                Loading…
              </td>
            </tr>
          ) : rows.length === 0 ? (
            <tr>
              <td colSpan={cols.length} className="text-center py-14 text-gray-500">
                No stats found.
              </td>
            </tr>
          ) : (
            rows.map((r, i) => (
              <tr
                key={teamRows ? r.team_id ?? i : r.player_id ?? i}
                className="border-t border-white/5 hover:bg-white/5"
              >
                {cols.map((c) => {
                  const val = r[c.key];
                  if (c.key === "player_name") {
                    return (
                      <td key={c.key} className="px-3 py-2 sticky left-0 bg-[#0a0a0f] z-10">
                        {linkPlayers ? (
                          <Link
                            href={`/${sport}/players/${r.player_id}`}
                            className="font-medium hover:text-earl-400 whitespace-nowrap"
                          >
                            {r.player_name}
                          </Link>
                        ) : (
                          <span className="font-medium whitespace-nowrap">{r.player_name}</span>
                        )}
                      </td>
                    );
                  }
                  if (c.key === "team_abbr") {
                    return (
                      <td
                        key={c.key}
                        className={`px-3 py-2 whitespace-nowrap ${teamRows ? "sticky left-0 bg-[#0a0a0f] z-10" : ""}`}
                      >
                        <span className="inline-flex items-center gap-2">
                          <TeamLogo abbr={r.team_abbr || "FA"} sport={sport} size={18} />
                          <span className="font-medium">{teamRows ? r.team_name : r.team_abbr || "FA"}</span>
                        </span>
                      </td>
                    );
                  }
                  if (c.key === "position") {
                    return (
                      <td key={c.key} className="px-3 py-2 text-earl-400 font-semibold">
                        {val || "—"}
                      </td>
                    );
                  }
                  const text = c.fmt ? c.fmt(val) : val === null || val === undefined ? "—" : String(val);
                  return (
                    <td
                      key={c.key}
                      className={`px-3 py-2 whitespace-nowrap ${c.align === "right" ? "text-right tabular-nums" : ""}`}
                    >
                      {text}
                    </td>
                  );
                })}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
