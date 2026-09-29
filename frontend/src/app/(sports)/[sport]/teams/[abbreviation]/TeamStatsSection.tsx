"use client";

import { useEffect, useState } from "react";
import TeamLogo from "@/components/TeamLogo";

/**
 * Yahoo-style "Stats" section for a team/season page.
 * Consumes a uniform shape from each sport's backend:
 *   { found, team:{abbr}, record:{wins,losses,ties,games},
 *     sections:[{title, rows:[{label,value,rank,unit}]}],
 *     leader_groups:[{title, cards:[{key,title,unit,rows:[{rank,player_name,team_abbr,value}]}]}] }
 */

type Unit = "int" | "one" | "pct" | "rate3" | "rate2";

interface StatRow { label: string; value: number | null; rank: number | null; unit: Unit }
interface Section { title: string; rows: StatRow[] }
interface LeaderRow { rank: number; player_name: string; team_abbr: string | null; position?: string | null; value: number }
interface LeaderCard { key: string; title: string; unit: Unit; rows: LeaderRow[] }
interface LeaderGroup { title: string; cards: LeaderCard[] }
interface TeamStats {
  found: boolean;
  year?: number;
  team: { abbr: string };
  record?: { wins: number; losses: number; ties: number; games: number };
  sections?: Section[];
  leader_groups?: LeaderGroup[];
}

function fmt(unit: Unit, v: number | null) {
  if (v == null) return "—";
  if (unit === "pct") return (Number(v) * 100).toFixed(1);
  if (unit === "rate3") return Number(v).toFixed(3).replace(/^0\./, ".");
  if (unit === "rate2") return Number(v).toFixed(2);
  if (unit === "int") return Math.round(Number(v)).toLocaleString();
  return Number(v).toFixed(1);
}

function endpointFor(sport: string, abbr: string, year: number) {
  if (sport === "nba") return `/api/nba/stats/team/${abbr}?year=${year}`;
  if (sport === "mlb") return `/api/mlb/stats/team/${abbr}?year=${year}`;
  return `/api/stats/team/${abbr}?year=${year}`;
}

export default function TeamStatsSection({
  sport,
  abbr,
  year,
}: {
  sport: string;
  abbr: string;
  year: number;
}) {
  const [data, setData] = useState<TeamStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!abbr || !year) return;
    let cancelled = false;
    setLoading(true);
    setErr(null);
    (async () => {
      // Walk back a few seasons: some sports' newest season (e.g. NBA 2026) is
      // schedule-only with no stats yet, so fall back to the latest that has data.
      for (let y = year; y >= year - 3; y--) {
        try {
          const r = await fetch(endpointFor(sport, abbr.toUpperCase(), y));
          if (!r.ok) break;
          const d = await r.json();
          if (cancelled) return;
          if (d && d.found) {
            setData(d);
            setErr(null);
            setLoading(false);
            return;
          }
        } catch {
          break;
        }
      }
      if (!cancelled) {
        setErr("No stats available for this season yet.");
        setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [sport, abbr, year]);

  if (loading) return <div className="text-center py-16 text-gray-500">Loading stats…</div>;
  if (err) return <div className="text-center py-16 text-gray-500">{err}</div>;
  if (!data) return null;

  const rec = data.record;
  const shownYear = data.year ?? year;

  return (
    <div className="space-y-8">
      {rec && (
        <div className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-1.5">
            <TeamLogo abbr={abbr.toUpperCase()} sport={sport} size={22} />
            <span className="font-semibold">{shownYear}</span>
          </span>
          <span className="text-sm text-gray-400">
            Record{" "}
            <span className="text-white font-semibold">
              {rec.wins}-{rec.losses}
              {rec.ties ? `-${rec.ties}` : ""}
            </span>
            {rec.games > 0 && (
              <span className="text-gray-500"> · {((rec.wins / Math.max(rec.wins + rec.losses + rec.ties, 1)) * 100).toFixed(1)}%</span>
            )}
          </span>
        </div>
      )}

      {data.sections?.length ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {data.sections.map((s) => (
            <div key={s.title} className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
              <div className="px-4 py-2.5 bg-white/5 border-b border-white/10">
                <span className="text-sm font-semibold">{s.title}</span>
              </div>
              <table className="w-full text-sm">
                <tbody className="divide-y divide-white/5">
                  {s.rows.map((r) => (
                    <tr key={r.label} className="hover:bg-white/5">
                      <td className="px-4 py-2 text-gray-300">{r.label}</td>
                      <td className="px-2 py-2 text-right tabular-nums font-medium">{fmt(r.unit, r.value)}</td>
                      <td className="px-4 py-2 text-right tabular-nums text-xs text-gray-500 w-16">
                        {r.rank ? `#${r.rank}` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ) : null}

      {data.leader_groups?.some((g) => g.cards.some((c) => c.rows.length)) && (
        <div className="space-y-6">
          <h3 className="text-xs uppercase tracking-widest text-gray-500">Team Leaders</h3>
          {data.leader_groups.map((g) => (
            <section key={g.title} className="space-y-3">
              <h4 className="text-[11px] uppercase tracking-wider text-gray-600">{g.title}</h4>
              <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-4">
                {g.cards.filter((c) => c.rows.length).map((c) => (
                  <div key={c.key} className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
                    <div className="px-4 py-2.5 bg-white/5 border-b border-white/10">
                      <span className="text-sm font-semibold">{c.title}</span>
                    </div>
                    <div className="divide-y divide-white/5">
                      {c.rows.map((r) => {
                        const a = (r.team_abbr || "").split("/")[0];
                        return (
                          <div key={r.player_name} className="flex items-center gap-3 px-4 py-2 hover:bg-white/5">
                            <span className="text-xs text-gray-500 w-4 text-right">{r.rank}</span>
                            {a ? <TeamLogo abbr={a} sport={sport} size={20} /> : <span className="w-5 h-5 inline-block" />}
                            <span className="flex-1 truncate text-sm font-medium">{r.player_name}</span>
                            <span className="text-sm font-semibold tabular-nums">{fmt(c.unit, r.value)}</span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            </section>
          ))}
        </div>
      )}
    </div>
  );
}
