"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import TeamLogo from "@/components/TeamLogo";

/**
 * Yahoo-style NBA stats hub — mirrors https://sports.yahoo.com/nba/stats/
 * Tabs: League Leaders (category cards) · Player Stats · Team Stats.
 */

type View = "leaders" | "players" | "teams";
type Category = "scoring" | "rebounds" | "assists" | "defense";

interface LeaderRow {
  rank: number;
  player_id: number;
  player_name: string;
  position: string | null;
  team_abbr: string | null;
  value: number;
}
interface LeaderCard {
  key: string;
  title: string;
  unit: string;
  rows: LeaderRow[];
}
interface LeaderGroup {
  title: string;
  cards: LeaderCard[];
}
interface LeadersResponse {
  year: number;
  limit: number;
  groups: LeaderGroup[];
}

interface PlayerRow {
  player_id: number;
  player_name: string;
  position: string | null;
  team_abbr: string | null;
  games_played: number;
  games_started: number;
  minutes_played: number;
  points: number;
  points_per_game: number | null;
  field_goals_made: number;
  field_goals_attempted: number;
  field_goal_pct: number | null;
  three_points_made: number;
  three_points_attempted: number;
  three_point_pct: number | null;
  free_throws_made: number;
  free_throws_attempted: number;
  free_throw_pct: number | null;
  rebounds: number;
  offensive_rebounds: number;
  defensive_rebounds: number;
  rebounds_per_game: number | null;
  assists: number;
  assists_per_game: number | null;
  turnovers: number;
  steals: number;
  blocks: number;
  personal_fouls: number;
  plus_minus: number;
  efficiency: number;
}

interface TeamRow {
  team_id: number;
  team_name: string;
  team_abbr: string;
  conference: string | null;
  division: string | null;
  games: number;
  wins: number;
  losses: number;
  points_for: number | null;
  points_against: number | null;
}

type Col = { key: string; label: string; fmt?: (v: any) => string };

const fmtInt = (v: any) => (v == null ? "—" : Math.round(Number(v)).toLocaleString());
const fmtOne = (v: any) => (v == null ? "—" : Number(v).toFixed(1));
// Stored as a fraction (0.4756) -> display 47.6
const fmtPct = (v: any) => (v == null ? "—" : (Number(v) * 100).toFixed(1));

const CATEGORIES: { key: Category; label: string }[] = [
  { key: "scoring", label: "Scoring" },
  { key: "rebounds", label: "Rebounds" },
  { key: "assists", label: "Assists" },
  { key: "defense", label: "Defense" },
];

const PLAYER_COLS: Record<Category, Col[]> = {
  scoring: [
    { key: "games_played", label: "GP", fmt: fmtInt },
    { key: "minutes_played", label: "MIN", fmt: fmtInt },
    { key: "points", label: "PTS", fmt: fmtInt },
    { key: "points_per_game", label: "PPG", fmt: fmtOne },
    { key: "field_goals_made", label: "FGM", fmt: fmtInt },
    { key: "field_goals_attempted", label: "FGA", fmt: fmtInt },
    { key: "field_goal_pct", label: "FG%", fmt: fmtPct },
    { key: "three_points_made", label: "3PM", fmt: fmtInt },
    { key: "three_point_pct", label: "3P%", fmt: fmtPct },
    { key: "free_throws_made", label: "FTM", fmt: fmtInt },
    { key: "free_throw_pct", label: "FT%", fmt: fmtPct },
    { key: "efficiency", label: "EFF", fmt: fmtOne },
  ],
  rebounds: [
    { key: "games_played", label: "GP", fmt: fmtInt },
    { key: "rebounds", label: "REB", fmt: fmtInt },
    { key: "rebounds_per_game", label: "RPG", fmt: fmtOne },
    { key: "offensive_rebounds", label: "OREB", fmt: fmtInt },
    { key: "defensive_rebounds", label: "DREB", fmt: fmtInt },
  ],
  assists: [
    { key: "games_played", label: "GP", fmt: fmtInt },
    { key: "assists", label: "AST", fmt: fmtInt },
    { key: "assists_per_game", label: "APG", fmt: fmtOne },
    { key: "turnovers", label: "TO", fmt: fmtInt },
  ],
  defense: [
    { key: "games_played", label: "GP", fmt: fmtInt },
    { key: "steals", label: "STL", fmt: fmtInt },
    { key: "blocks", label: "BLK", fmt: fmtInt },
    { key: "personal_fouls", label: "PF", fmt: fmtInt },
    { key: "plus_minus", label: "+/-", fmt: fmtInt },
  ],
};

const DEFAULT_SORT: Record<Category, string> = {
  scoring: "points_per_game",
  rebounds: "rebounds_per_game",
  assists: "assists_per_game",
  defense: "steals",
};

const POSITIONS: Record<Category, string[]> = {
  scoring: ["ALL", "G", "F", "C"],
  rebounds: ["ALL", "C", "F"],
  assists: ["ALL", "G", "F"],
  defense: ["ALL", "C", "F", "G"],
};

const TEAM_COLS: Col[] = [
  { key: "wins", label: "W", fmt: fmtInt },
  { key: "losses", label: "L", fmt: fmtInt },
  { key: "pct", label: "PCT", fmt: (v) => (v == null ? "—" : Number(v).toFixed(3).replace(/^0/, "")) },
  { key: "pfpg", label: "PF/G", fmt: fmtOne },
  { key: "papg", label: "PA/G", fmt: fmtOne },
  { key: "diffpg", label: "DIFF", fmt: (v) => (v == null ? "—" : (Number(v) >= 0 ? "+" : "") + Number(v).toFixed(1)) },
];

const NBA_SEEALL: Record<string, { cat: Category; key: string }> = {
  points_per_game: { cat: "scoring", key: "points_per_game" },
  points: { cat: "scoring", key: "points" },
  field_goal_pct: { cat: "scoring", key: "field_goal_pct" },
  rebounds_per_game: { cat: "rebounds", key: "rebounds_per_game" },
  rebounds: { cat: "rebounds", key: "rebounds" },
  steals_per_game: { cat: "defense", key: "steals" },
  blocks_per_game: { cat: "defense", key: "blocks" },
  assists_per_game: { cat: "assists", key: "assists_per_game" },
  turnovers_per_game: { cat: "assists", key: "turnovers" },
  efficiency: { cat: "scoring", key: "efficiency" },
};

function fmtLeader(unit: string, v: any) {
  if (v == null) return "—";
  if (unit === "int") return Math.round(Number(v)).toLocaleString();
  if (unit === "pct") return (Number(v) * 100).toFixed(1);
  return Number(v).toFixed(1);
}

// ── Segmented control ─────────────────────────────────────────────────

function Segmented<T extends string>({
  options,
  value,
  onChange,
  size = "md",
}: {
  options: { key: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  size?: "sm" | "md";
}) {
  return (
    <div className="inline-flex rounded-lg overflow-hidden border border-white/10">
      {options.map((o) => (
        <button
          key={o.key}
          onClick={() => onChange(o.key)}
          className={`${
            size === "sm" ? "px-2.5 py-1 text-[11px]" : "px-3.5 py-1.5 text-xs"
          } font-semibold transition-colors ${
            value === o.key
              ? "bg-earl-600 text-white"
              : "bg-white/[0.02] text-gray-400 hover:text-white hover:bg-white/5"
          }`}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// ── Leader card ───────────────────────────────────────────────────────

function LeaderCardView({
  card,
  sport,
  onSeeAll,
}: {
  card: LeaderCard;
  sport: string;
  onSeeAll?: () => void;
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-white/5 border-b border-white/10">
        <span className="text-sm font-semibold">{card.title}</span>
        <span className="text-[10px] font-bold tracking-wider text-earl-400 px-1.5 py-0.5 rounded bg-earl-500/10">
          TOP {card.rows.length}
        </span>
      </div>
      <div className="divide-y divide-white/5">
        {card.rows.map((r) => {
          const abbr = (r.team_abbr || "").split("/")[0];
          return (
            <div key={r.player_id} className="flex items-center gap-3 px-4 py-2 hover:bg-white/5">
              <span className="text-xs text-gray-500 w-4 text-right">{r.rank}</span>
              {abbr ? <TeamLogo abbr={abbr} sport={sport} size={20} /> : <span className="w-5 h-5 inline-block" />}
              <div className="min-w-0 flex-1">
                <div className="flex-1 truncate text-sm">
                  <span className="font-medium">{r.player_name}</span>
                  {r.position ? <span className="text-gray-600 ml-1.5 text-xs">{r.position}</span> : null}
                </div>
              </div>
              <span className="text-sm font-semibold tabular-nums">{fmtLeader(card.unit, r.value)}</span>
            </div>
          );
        })}
        {card.rows.length === 0 && <div className="px-4 py-3 text-xs text-gray-500">No data</div>}
      </div>
      {onSeeAll && (
        <button
          onClick={onSeeAll}
          className="w-full px-4 py-2 text-xs font-semibold text-earl-400 hover:text-earl-300 hover:bg-white/5 text-left border-t border-white/5"
        >
          See all {card.title} →
        </button>
      )}
    </div>
  );
}

// ── Sortable stat table ───────────────────────────────────────────────

function StatTable<T extends Record<string, any>>({
  cols,
  rows,
  nameKey,
  nameLabel = "Player",
  sport,
  sort,
  order,
  onSort,
  badgeKey,
}: {
  cols: Col[];
  rows: T[];
  nameKey: string;
  nameLabel?: string;
  sport: string;
  sort: string;
  order: "desc" | "asc";
  onSort: (k: string) => void;
  badgeKey?: string;
}) {
  return (
    <div className="overflow-x-auto rounded-xl border border-white/10">
      <table className="w-full text-sm">
        <thead>
          <tr className="bg-white/5 text-gray-400 uppercase text-[10px] tracking-wider">
            <th className="px-3 py-2 text-right w-8">#</th>
            <th className="px-3 py-2 text-left">{nameLabel}</th>
            {cols.map((c) => (
              <th
                key={c.key}
                onClick={() => onSort(c.key)}
                className={`px-3 py-2 text-right cursor-pointer select-none whitespace-nowrap hover:text-white ${
                  sort === c.key ? "text-earl-400" : ""
                }`}
              >
                {c.label}
                {sort === c.key && <span className="text-earl-400 ml-0.5">{order === "desc" ? "▾" : "▴"}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-white/5">
          {rows.map((r, i) => {
            const name = r[nameKey] as string;
            const abbr = ((r.team_abbr as string) || "").split("/")[0];
            return (
              <tr key={(r.player_id ?? r.team_id ?? i) as any} className="hover:bg-white/5">
                <td className="px-3 py-2 text-right text-xs text-gray-500">{i + 1}</td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    {abbr ? <TeamLogo abbr={abbr} sport={sport} size={20} /> : null}
                    <span className="font-medium whitespace-nowrap">{name}</span>
                    {badgeKey && r[badgeKey] ? (
                      <span className="text-[10px] text-gray-500 border border-white/10 rounded px-1">{r[badgeKey]}</span>
                    ) : null}
                  </div>
                </td>
                {cols.map((c) => (
                  <td key={c.key} className="px-3 py-2 text-right tabular-nums whitespace-nowrap">
                    {c.fmt ? c.fmt(r[c.key]) : r[c.key]}
                  </td>
                ))}
              </tr>
            );
          })}
          {rows.length === 0 && (
            <tr>
              <td colSpan={cols.length + 2} className="px-4 py-10 text-center text-gray-500">
                No stats found.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────

export default function NBAStatsYahoo({ sport = "nba" }: { sport?: string }) {
  const [view, setView] = useState<View>("leaders");
  const [category, setCategory] = useState<Category>("scoring");
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number>(2025);
  const [position, setPosition] = useState("ALL");
  const [sort, setSort] = useState<string>("points_per_game");
  const [order, setOrder] = useState<"desc" | "asc">("desc");

  const [leaders, setLeaders] = useState<LeadersResponse | null>(null);
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/nba/stats/seasons")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        const ys: number[] = d.years || [];
        setYears(ys);
        if (ys.length) setYear(ys[0]);
      })
      .catch(() => setYears([2025, 2024, 2023]));
  }, []);

  useEffect(() => {
    if (view !== "leaders") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/nba/stats/leaders?year=${year}&limit=5`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => setLeaders(d))
      .catch(() => setErr("Could not load leaders."))
      .finally(() => setLoading(false));
  }, [view, year]);

  useEffect(() => {
    if (view !== "players") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/nba/stats/players?year=${year}&position=${position}&limit=500`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => setPlayers(d.data || []))
      .catch(() => setErr("Could not load player stats."))
      .finally(() => setLoading(false));
  }, [view, year, position]);

  useEffect(() => {
    if (view !== "teams") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/nba/stats/teams?year=${year}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        const rows: TeamRow[] = (d.data || []).filter((t: TeamRow) => (t.games || 0) > 0);
        setTeams(rows);
      })
      .catch(() => setErr("Could not load team stats."))
      .finally(() => setLoading(false));
  }, [view, year]);

  const cols = PLAYER_COLS[category];

  const sortedPlayers = useMemo(() => {
    const arr = [...players];
    arr.sort((a, b) => {
      const av = Number((a as any)[sort] ?? -1e9);
      const bv = Number((b as any)[sort] ?? -1e9);
      return order === "desc" ? bv - av : av - bv;
    });
    return arr;
  }, [players, sort, order]);

  const teamRows = useMemo(() => {
    const arr = teams.map((t) => {
      const g = t.games || 0;
      const pf = Number(t.points_for ?? 0);
      const pa = Number(t.points_against ?? 0);
      const w = t.wins || 0;
      const l = t.losses || 0;
      return {
        ...t,
        pct: w + l > 0 ? w / (w + l) : 0,
        pfpg: g ? pf / g : 0,
        papg: g ? pa / g : 0,
        diffpg: g ? (pf - pa) / g : 0,
      } as TeamRow & { pct: number; pfpg: number; papg: number; diffpg: number };
    });
    arr.sort((a, b) => {
      const av = Number((a as any)[sort] ?? -1e9);
      const bv = Number((b as any)[sort] ?? -1e9);
      return order === "desc" ? bv - av : av - bv;
    });
    return arr;
  }, [teams, sort, order]);

  const onSort = useCallback((k: string) => {
    setSort((prev) => {
      if (prev === k) {
        setOrder((o) => (o === "desc" ? "asc" : "desc"));
        return prev;
      }
      setOrder("desc");
      return k;
    });
  }, []);

  function pickCategory(c: Category) {
    setCategory(c);
    setSort(DEFAULT_SORT[c]);
    setOrder("desc");
    setPosition("ALL");
  }

  function seeAll(cat: Category, key: string) {
    setCategory(cat);
    setSort(key);
    setOrder("desc");
    setPosition("ALL");
    setView("players");
    if (typeof window !== "undefined") window.scrollTo({ top: 0, behavior: "smooth" });
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Segmented
          options={[
            { key: "leaders" as View, label: "League Leaders" },
            { key: "players" as View, label: "Player Stats" },
            { key: "teams" as View, label: "Team Stats" },
          ]}
          value={view}
          onChange={(v) => setView(v)}
        />
        <label className="flex items-center gap-2 text-xs text-gray-400">
          Season
          <select
            value={year}
            onChange={(e) => setYear(Number(e.target.value))}
            className="bg-white/[0.03] border border-white/10 rounded-md px-2 py-1 text-xs text-white"
          >
            {years.map((y) => (
              <option key={y} value={y} className="bg-[#0a0a0f]">
                {y}
              </option>
            ))}
          </select>
        </label>
      </div>

      {err && <div className="text-center py-6 text-red-400 text-sm">{err}</div>}

      {view === "leaders" && (
        <div className="space-y-8">
          {loading && !leaders && <div className="text-center py-20 text-gray-500">Loading…</div>}
          {leaders?.groups.map((g) => (
            <section key={g.title} className="space-y-4">
              <h2 className="text-xs uppercase tracking-widest text-gray-500 mb-2">{g.title}</h2>
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                {g.cards.map((c) => (
                  <LeaderCardView
                    key={c.key}
                    card={c}
                    sport={sport}
                    onSeeAll={() => {
                      const t = NBA_SEEALL[c.key];
                      if (t) seeAll(t.cat, t.key);
                    }}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {view === "players" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 border-b border-white/10 pb-3">
            <Segmented size="sm" options={CATEGORIES} value={category} onChange={(c) => pickCategory(c)} />
            <div className="flex flex-wrap gap-1">
              {POSITIONS[category].map((p) => (
                <button
                  key={p}
                  onClick={() => setPosition(p)}
                  className={`px-2 py-0.5 rounded text-[11px] border ${
                    position === p
                      ? "border-earl-500 text-earl-400 bg-earl-500/10"
                      : "border-white/10 text-gray-400 hover:text-white"
                  }`}
                >
                  {p === "ALL" ? "All" : p}
                </button>
              ))}
            </div>
            <span className="text-xs text-gray-500 ml-auto">{sortedPlayers.length} players</span>
          </div>
          {loading ? (
            <div className="text-center py-14 text-gray-500">Loading…</div>
          ) : (
            <StatTable
              cols={cols}
              rows={sortedPlayers}
              nameKey="player_name"
              sport={sport}
              sort={sort}
              order={order}
              onSort={onSort}
              badgeKey="position"
            />
          )}
        </div>
      )}

      {view === "teams" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 border-b border-white/10 pb-3">
            <span className="text-xs uppercase tracking-widest text-gray-500">All Teams</span>
            <span className="text-xs text-gray-500 ml-auto">{teamRows.length} teams</span>
          </div>
          {loading ? (
            <div className="text-center py-14 text-gray-500">Loading…</div>
          ) : (
            <StatTable
              cols={TEAM_COLS}
              rows={teamRows}
              nameKey="team_name"
              nameLabel="Team"
              sport={sport}
              sort={sort}
              order={order}
              onSort={onSort}
            />
          )}
        </div>
      )}
    </div>
  );
}
