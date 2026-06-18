"use client";

import React, { useEffect, useState } from "react";
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

function StatRow({ label, home, away, highlight }: { label: string; home: number | null | string; away: number | null | string; highlight?: boolean }) {
  return (
    <tr className="border-t border-white/5">
      <td className={`px-3 py-1.5 text-right text-sm w-[40%] ${highlight ? "font-bold text-white" : "text-gray-300"}`}>{home ?? "-"}</td>
      <td className={`px-3 py-1.5 text-xs text-gray-500 text-center w-[20%] ${highlight ? "font-bold" : ""}`}>{label}</td>
      <td className={`px-3 py-1.5 text-sm w-[40%] ${highlight ? "font-bold text-white" : "text-gray-300"}`}>{away ?? "-"}</td>
    </tr>
  );
}

function PCT(v: number | null | undefined): string {
  if (v == null) return "-";
  return v.toFixed(3).slice(1); // e.g., .455
}

function PlayerTable({ players }: { players: NBAPlayerStat[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="bg-white/[0.03] text-gray-500 uppercase tracking-wider">
            <th className="px-2 py-1.5 text-left sticky left-0 bg-[#0a0a1a]">Player</th>
            <th className="px-2 py-1.5 text-center">MIN</th>
            <th className="px-2 py-1.5 text-center">FG</th>
            <th className="px-2 py-1.5 text-center">3PT</th>
            <th className="px-2 py-1.5 text-center">FT</th>
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

function NBABoxScorePage({ gameId }: { gameId: string | undefined }) {
  const [data, setData] = useState<NBABoxScoreData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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

  if (loading) {
    return <div className="text-center py-12 text-gray-500">Loading boxscore...</div>;
  }

  if (error || !data) {
    return (
      <div className="text-center py-12">
        <div className="text-gray-500 mb-4">{error || "Game not found."}</div>
        <Link href="/nba/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">
          ← Back to Schedule
        </Link>
      </div>
    );
  }

  const h = data.home;
  const a = data.away;
  const isFinal = data.status === "FINAL" || data.status === "final";

  return (
    <div className="max-w-3xl mx-auto p-4">
      {/* Back link */}
      <div className="mb-4">
        <Link href="/nba/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">
          ← Back to Schedule
        </Link>
      </div>

      {/* Score header */}
      <div className="border border-white/10 rounded-xl p-6 mb-4">
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

      {/* Boxscore stats table */}
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

      {/* Player stats — split by team */}
      {data.players && data.players.length > 0 && (
        <div className="mt-4 space-y-4">
          {/* Home team */}
          {(() => {
            const homePlayers = data.players.filter(p => p.team_id === data.home.team_id);
            const awayPlayers = data.players.filter(p => p.team_id === data.away.team_id);
            return (
              <>
                {/* Away team */}
                <div className="border border-white/10 rounded-xl overflow-hidden">
                  <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-gray-300">{data.away.team}</div>
                  <PlayerTable players={awayPlayers} />
                </div>
                {/* Home team */}
                <div className="border border-white/10 rounded-xl overflow-hidden">
                  <div className="bg-white/5 px-4 py-2 text-sm font-semibold text-white">{data.home.team}</div>
                  <PlayerTable players={homePlayers} />
                </div>
              </>
            );
          })()}
        </div>
      )}

      <div className="text-center mt-4">
        <Link href="/nba/schedule" className="text-sm text-earl-400 hover:text-earl-300 transition">
          ← Back to Schedule
        </Link>
      </div>
    </div>
  );
}
