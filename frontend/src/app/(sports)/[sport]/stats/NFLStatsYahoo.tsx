"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import TeamLogo from "@/components/TeamLogo";

/**
 * Yahoo-style NFL stats hub — mirrors https://sports.yahoo.com/nfl/stats/
 * Tabs: League Leaders (category cards) · Player Stats · Team Stats.
 * Data: GET /api/stats/leaders|players|teams|seasons (REG season only).
 */

type View = "leaders" | "players" | "teams";
type Category = "passing" | "rushing" | "receiving" | "defense";

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
  games: number;
  pass_attempts: number;
  pass_completions: number;
  pass_yards: number;
  pass_tds: number;
  pass_int: number;
  comp_pct: number | null;
  yards_per_att: number | null;
  passer_rating: number | null;
  rush_attempts: number;
  rush_yards: number;
  rush_tds: number;
  yards_per_carry: number | null;
  targets: number;
  receptions: number;
  receiving_yards: number;
  receiving_tds: number;
  yards_per_rec: number | null;
  fumbles: number;
  fumbles_lost: number;
  tackles_combined: number;
  sacks: number;
  tackles_for_loss: number;
  qb_hits: number;
  fumbles_forced: number;
  interceptions: number;
  passes_defended: number;
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
  ties: number;
  points_for: number;
  points_against: number;
  point_diff: number;
  yds_for: number;
  yds_against: number;
  yds_diff: number;
  rush_yds_for: number;
  rush_yds_against: number;
  pass_yds_for: number;
  pass_yds_against: number;
  to_takeaways: number;
  to_giveaways: number;
  to_margin: number;
}

type Col = { key: string; label: string; fmt?: (v: any) => string };

const fmtInt = (v: any) => (v == null ? "—" : Math.round(Number(v)).toLocaleString());
const fmtOne = (v: any) => (v == null ? "—" : Number(v).toFixed(1));

const CATEGORIES: { key: Category; label: string }[] = [
  { key: "passing", label: "Passing" },
  { key: "rushing", label: "Rushing" },
  { key: "receiving", label: "Receiving" },
  { key: "defense", label: "Defense" },
];

const PLAYER_COLS: Record<Category, Col[]> = {
  passing: [
    { key: "games", label: "GP", fmt: fmtInt },
    { key: "pass_completions", label: "CMP", fmt: fmtInt },
    { key: "pass_attempts", label: "ATT", fmt: fmtInt },
    { key: "pass_yards", label: "YDS", fmt: fmtInt },
    { key: "pass_tds", label: "TD", fmt: fmtInt },
    { key: "pass_int", label: "INT", fmt: fmtInt },
    { key: "comp_pct", label: "PCT", fmt: fmtOne },
    { key: "yards_per_att", label: "Y/A", fmt: fmtOne },
    { key: "passer_rating", label: "RATE", fmt: fmtOne },
  ],
  rushing: [
    { key: "games", label: "GP", fmt: fmtInt },
    { key: "rush_attempts", label: "ATT", fmt: fmtInt },
    { key: "rush_yards", label: "YDS", fmt: fmtInt },
    { key: "rush_tds", label: "TD", fmt: fmtInt },
    { key: "yards_per_carry", label: "Y/A", fmt: fmtOne },
  ],
  receiving: [
    { key: "games", label: "GP", fmt: fmtInt },
    { key: "targets", label: "TGT", fmt: fmtInt },
    { key: "receptions", label: "REC", fmt: fmtInt },
    { key: "receiving_yards", label: "YDS", fmt: fmtInt },
    { key: "receiving_tds", label: "TD", fmt: fmtInt },
    { key: "yards_per_rec", label: "Y/R", fmt: fmtOne },
  ],
  defense: [
    { key: "games", label: "GP", fmt: fmtInt },
    { key: "tackles_combined", label: "TCK", fmt: fmtInt },
    { key: "sacks", label: "SACK", fmt: fmtOne },
    { key: "tackles_for_loss", label: "TFL", fmt: fmtInt },
    { key: "qb_hits", label: "QBH", fmt: fmtInt },
    { key: "fumbles_forced", label: "FF", fmt: fmtInt },
    { key: "interceptions", label: "INT", fmt: fmtInt },
    { key: "passes_defended", label: "PD", fmt: fmtInt },
  ],
};

const DEFAULT_SORT: Record<Category, string> = {
  passing: "pass_yards",
  rushing: "rush_yards",
  receiving: "receiving_yards",
  defense: "tackles_combined",
};

const POSITIONS: Record<Category, string[]> = {
  passing: ["ALL", "QB"],
  rushing: ["ALL", "RB", "QB", "WR"],
  receiving: ["ALL", "WR", "TE", "RB"],
  defense: ["ALL", "LB", "DB", "DL", "DE", "DT"],
};

const TEAM_COLS: Col[] = [
  { key: "wins", label: "W", fmt: fmtInt },
  { key: "losses", label: "L", fmt: fmtInt },
  { key: "ties", label: "T", fmt: fmtInt },
  { key: "points_for", label: "PF", fmt: fmtInt },
  { key: "points_against", label: "PA", fmt: fmtInt },
  { key: "point_diff", label: "DIFF", fmt: fmtInt },
  { key: "yds_for", label: "OFF Y/G", fmt: fmtOne },
  { key: "yds_against", label: "DEF Y/G", fmt: fmtOne },
  { key: "rush_yds_for", label: "RUSH/G", fmt: fmtOne },
  { key: "pass_yds_for", label: "PASS/G", fmt: fmtOne },
  { key: "to_margin", label: "TO", fmt: fmtInt },
];

const NFL_GROUP_CAT: Record<string, Category> = {
  Passing: "passing",
  Rushing: "rushing",
  Receiving: "receiving",
  Defense: "defense",
};

function fmtLeader(unit: string, v: any) {
  if (v == null) return "—";
  if (unit === "int") return Math.round(Number(v)).toLocaleString();
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

function LeaderCardView({ card, sport, onSeeAll }: { card: LeaderCard; sport: string; onSeeAll?: () => void }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2.5 bg-white/5 border-b border-white/10">
        <span className="text-sm font-semibold">{card.title}</span>
        <span className="text-[10px] font-bold tracking-wider text-earl-400 px-1.5 py-0.5 rounded bg-earl-500/10">
          TOP {card.rows.length}
        </span>
      </div>
      <div className="divide-y divide-white/5">
        {card.rows.map((r) => (
          <div key={r.player_id} className="flex items-center gap-3 px-4 py-2 hover:bg-white/5">
            <span className="text-xs text-gray-500 w-4 text-right">{r.rank}</span>
            {((r.team_abbr || "").split("/")[0]) ? (
              <TeamLogo abbr={(r.team_abbr || "").split("/")[0]} sport={sport} size={20} />
            ) : (
              <span className="w-5 h-5 inline-block" />
            )}
            <div className="min-w-0 flex-1">
              <Link href={`/${sport}/players/${r.player_id}`} className="block flex-1 truncate text-sm hover:text-earl-400">
                <span className="font-medium">{r.player_name}</span>
                {r.position ? <span className="text-gray-600 ml-1.5 text-xs">{r.position}</span> : null}
              </Link>
            </div>
            <span className="text-sm font-semibold tabular-nums">{fmtLeader(card.unit, r.value)}</span>
          </div>
        ))}
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
  linkTo,
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
  linkTo?: (r: T) => string;
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
                    {linkTo ? (
                      <Link href={linkTo(r)} className="font-medium hover:text-earl-400 whitespace-nowrap">
                        {name}
                      </Link>
                    ) : (
                      <span className="font-medium whitespace-nowrap">{name}</span>
                    )}
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

export default function NFLStatsYahoo({ sport = "nfl" }: { sport?: string }) {
  const [view, setView] = useState<View>("leaders");
  const [category, setCategory] = useState<Category>("passing");
  const [years, setYears] = useState<number[]>([]);
  const [year, setYear] = useState<number>(2026);
  const [position, setPosition] = useState("ALL");
  const [sort, setSort] = useState<string>("pass_yards");
  const [order, setOrder] = useState<"desc" | "asc">("desc");

  const [leaders, setLeaders] = useState<LeadersResponse | null>(null);
  const [players, setPlayers] = useState<PlayerRow[]>([]);
  const [teams, setTeams] = useState<TeamRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // seasons
  useEffect(() => {
    fetch("/api/stats/seasons")
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => {
        const ys: number[] = d.years || [];
        setYears(ys);
        if (ys.length) setYear(ys[0]);
      })
      .catch(() => setYears([2026, 2025, 2024]));
  }, []);

  // leaders
  useEffect(() => {
    if (view !== "leaders") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/stats/leaders?year=${year}&limit=5`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => setLeaders(d))
      .catch(() => setErr("Could not load leaders."))
      .finally(() => setLoading(false));
  }, [view, year]);

  // players
  useEffect(() => {
    if (view !== "players") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/stats/players?year=${year}&position=${position}&limit=500`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => setPlayers(d.data || []))
      .catch(() => setErr("Could not load player stats."))
      .finally(() => setLoading(false));
  }, [view, year, position]);

  // teams
  useEffect(() => {
    if (view !== "teams") return;
    setLoading(true);
    setErr(null);
    fetch(`/api/stats/teams?year=${year}&limit=32`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => setTeams(d.data || []))
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

  const sortedTeams = useMemo(() => {
    const arr = [...teams];
    arr.sort((a, b) => {
      const av = Number((a as any)[sort] ?? -1e9);
      const bv = Number((b as any)[sort] ?? -1e9);
      return order === "desc" ? bv - av : av - bv;
    });
    return arr;
  }, [teams, sort, order]);

  const onSort = useCallback(
    (k: string) => {
      setSort((prev) => {
        if (prev === k) {
          setOrder((o) => (o === "desc" ? "asc" : "desc"));
          return prev;
        }
        setOrder("desc");
        return k;
      });
    },
    []
  );

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
        <div className="flex flex-wrap items-center gap-2">
          <Segmented
            options={[
              { key: "leaders" as View, label: "League Leaders" },
              { key: "players" as View, label: "Player Stats" },
              { key: "teams" as View, label: "Team Stats" },
            ]}
            value={view}
            onChange={(v) => setView(v)}
          />
        </div>
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

      {/* ── LEAGUE LEADERS ── */}
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
                    onSeeAll={() => seeAll(NFL_GROUP_CAT[g.title], c.key)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      )}

      {/* ── PLAYER STATS ── */}
      {view === "players" && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-3 border-b border-white/10 pb-3">
            <Segmented
              size="sm"
              options={CATEGORIES}
              value={category}
              onChange={(c) => pickCategory(c)}
            />
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

      {/* ── TEAM STATS ── */}
      {view === "teams" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 border-b border-white/10 pb-3">
            <span className="text-xs uppercase tracking-widest text-gray-500">All Teams</span>
            <span className="text-xs text-gray-500 ml-auto">{sortedTeams.length} teams</span>
          </div>
          {loading ? (
            <div className="text-center py-14 text-gray-500">Loading…</div>
          ) : (
            <StatTable
              cols={TEAM_COLS}
              rows={sortedTeams}
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
