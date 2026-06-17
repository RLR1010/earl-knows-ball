"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { api, Player } from "@/lib/api";

interface MLBPlayer {
  id: number;
  name: string;
  position: string;
  team_abbr: string | null;
  team_name: string | null;
  jersey_number: number | null;
  height: number | null;
  weight: number | null;
  college: string | null;
  bats: string | null;
  throws: string | null;
  years_exp: number | null;
  status: string | null;
}

interface NBAPlayer {
  id: number;
  name: string;
  position: string;
  team_abbr: string | null;
  team_name: string | null;
  jersey_number: number | null;
  height: number | null;
  weight: number | null;
  college: string | null;
  years_exp: number | null;
  status: string | null;
}

const NFL_POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "DST"];
const NBA_POSITIONS = ["ALL", "PG", "SG", "SF", "PF", "C"];
const MLB_POSITIONS = ["ALL", "P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "OF", "DH"];

function fmtInches(h: number | null): string {
  if (!h) return "-";
  return `${Math.floor(h / 12)}'${h % 12}"`;
}

export default function PlayersPage() {
  const params = useParams<{ sport: string }>();
  const sport = params?.sport || "nfl";
  const isMLB = sport === "mlb";
  const isNBA = sport === "nba";

  const [players, setPlayers] = useState<(Player | MLBPlayer | NBAPlayer)[]>([]);
  const [position, setPosition] = useState("ALL");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState(0);

  const positions = isMLB ? MLB_POSITIONS : isNBA ? NBA_POSITIONS : NFL_POSITIONS;
  const apiEndpoint = isMLB ? "/api/mlb/players" : isNBA ? "/api/nba/players" : null;

  useEffect(() => {
    setLoading(true);
    if (apiEndpoint) {
      const p = new URLSearchParams({ limit: "500", offset: "0" });
      if (position !== "ALL") p.set("position", position);
      if (search) p.set("search", search);
      fetch(`${apiEndpoint}?${p}`)
        .then(r => r.json())
        .then(data => {
          setPlayers(Array.isArray(data) ? data : data.data || []);
          setTotal(Array.isArray(data) ? data.length : (data.total || 0));
        })
        .catch(() => setPlayers([]))
        .finally(() => setLoading(false));
    } else {
      const p: { position?: string } = {};
      if (position !== "ALL") p.position = position;
      api.players.list(p)
        .then(data => { setPlayers(data); setTotal(data.length); })
        .catch(() => setPlayers([]))
        .finally(() => setLoading(false));
    }
  }, [position, apiEndpoint, search]);

  const filtered = players.filter((p) =>
    p.name.toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div className="space-y-6">
      <h1 className="font-display text-4xl font-bold">
        {sport.toUpperCase()} Players
      </h1>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <input
          type="text"
          placeholder="Search players..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="px-4 py-2 rounded-lg bg-white/5 border border-white/10 text-sm flex-1 min-w-[200px] focus:outline-none focus:border-earl-500"
        />
        <div className="flex gap-1 flex-wrap">
          {positions.map((p) => (
            <button
              key={p}
              onClick={() => setPosition(p)}
              className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition ${
                position === p
                  ? "bg-earl-600 text-white"
                  : "bg-white/5 text-gray-400 hover:bg-white/10"
              }`}
            >
              {p}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-center py-12 text-gray-500">Loading...</div>
      ) : (
        <div className="overflow-x-auto rounded-xl border border-white/10">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-white/5 text-gray-400 uppercase text-xs tracking-wider">
                <th className="px-4 py-3 text-left">Name</th>
                <th className="px-4 py-3 text-left">Pos</th>
                <th className="px-4 py-3 text-left">Team</th>
                {isMLB ? (
                  <><th className="px-4 py-3 text-center">B/T</th><th className="px-4 py-3 text-right">#</th><th className="px-4 py-3 text-right">Wt</th><th className="px-4 py-3 text-right">Ht</th></>
                ) : isNBA ? (
                  <><th className="px-4 py-3 text-right">#</th><th className="px-4 py-3 text-right">Ht</th><th className="px-4 py-3 text-right">Wt</th><th className="px-4 py-3 text-left">College</th></>
                ) : (
                  <><th className="px-4 py-3 text-right">#</th><th className="px-4 py-3 text-right">Wt</th><th className="px-4 py-3 text-left">College</th></>
                )}
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, 200).map((p) => (
                <tr key={p.id} className="border-t border-white/5 hover:bg-white/5 transition">
                  <td className="px-4 py-3">
                    <Link href={`/${sport}/players/${p.id}`} className="font-medium hover:text-earl-400 transition">{p.name}</Link>
                  </td>
                  <td className="px-4 py-3 text-earl-400 font-semibold">{p.position}</td>
                  <td className="px-4 py-3">{p.team_abbr || "FA"}</td>
                  {isMLB ? (
                    <>
                      <td className="px-4 py-3 text-center text-gray-400">{(p as MLBPlayer).bats || "-"}/{(p as MLBPlayer).throws || "-"}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{p.jersey_number || "-"}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{p.weight ? `${p.weight} lbs` : "-"}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{fmtInches((p as MLBPlayer).height)}</td>
                    </>
                  ) : isNBA ? (
                    <>
                      <td className="px-4 py-3 text-right text-gray-500">{p.jersey_number || "-"}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{fmtInches((p as NBAPlayer).height)}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{p.weight ? `${p.weight} lbs` : "-"}</td>
                      <td className="px-4 py-3 text-gray-400">{(p as NBAPlayer).college || "-"}</td>
                    </>
                  ) : (
                    <>
                      <td className="px-4 py-3 text-right text-gray-500">{p.jersey_number || "-"}</td>
                      <td className="px-4 py-3 text-right text-gray-500">{p.weight ? `${p.weight} lbs` : "-"}</td>
                      <td className="px-4 py-3 text-gray-400">{(p as Player).college || "-"}</td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {filtered.length > 200 && (
            <div className="text-center py-4 text-sm text-gray-500">Showing 200 of {filtered.length} players</div>
          )}
          {filtered.length === 0 && (
            <div className="text-center py-12 text-gray-500">No players found.</div>
          )}
        </div>
      )}
    </div>
  );
}
