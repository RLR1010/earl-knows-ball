"use client";

import { useMemo, useState } from "react";
import Link from "next/link";

/**
 * Sortable Yahoo-style stat table for a team/season page.
 * Columns/rows come straight from the backend (NFL team stat tables).
 *   { key, title, default_sort, columns:[{key,label,fmt}], rows:[{...}] }
 */

export type StatColFmt =
  | "int" | "dec1" | "pct" | "pct1"
  | "rate3" | "rate2" | "ip" | "time";

export interface StatColumn { key: string; label: string; fmt: StatColFmt }
export interface StatTableData {
  key: string;
  title: string;
  default_sort?: string;
  default_dir?: "asc" | "desc";
  columns: StatColumn[];
  rows: Array<Record<string, number | string | null>>;
}

function formatCell(fmt: StatColFmt, v: number | string | null): string {
  if (v == null || v === "") return "—";
  if (typeof v === "string") return v;
  if (fmt === "int") return Math.round(v).toLocaleString();
  if (fmt === "pct") return v.toFixed(1);
  if (fmt === "pct1") return `${v.toFixed(1)}%`;
  if (fmt === "rate3") { const s = v.toFixed(3); return s.startsWith("0.") ? s.slice(1) : s; }
  if (fmt === "rate2") return v.toFixed(2);
  if (fmt === "time") { const s = Math.round(v); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; }
  if (fmt === "ip") return v.toFixed(1);
  return v.toFixed(1);
}

export default function StatTable({
  sport,
  table,
}: {
  sport: string;
  table: StatTableData;
}) {
  const firstNumeric = table.columns[0]?.key ?? "player_name";
  const [sortKey, setSortKey] = useState<string>(table.default_sort || firstNumeric);
  const [dir, setDir] = useState<"asc" | "desc">(table.default_dir || "desc");

  const rows = useMemo(() => {
    const isPlayer = sortKey === "player_name";
    const sorted = [...table.rows].sort((a, b) => {
      if (isPlayer) {
        const sa = String(a.player_name ?? "");
        const sb = String(b.player_name ?? "");
        return dir === "asc" ? sa.localeCompare(sb) : sb.localeCompare(sa);
      }
      const av = a[sortKey];
      const bv = b[sortKey];
      const an = av == null || av === "" ? null : Number(av);
      const bn = bv == null || bv === "" ? null : Number(bv);
      if (an == null && bn == null) return 0;
      if (an == null) return 1; // nulls last regardless of dir
      if (bn == null) return -1;
      return dir === "asc" ? an - bn : bn - an;
    });
    return sorted;
  }, [table.rows, sortKey, dir]);

  function clickSort(key: string) {
    if (key === sortKey) {
      setDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setDir("desc");
    }
  }

  if (!table.rows.length) return null;

  return (
    <section className="rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden">
      <div className="px-4 py-2.5 bg-white/5 border-b border-white/10">
        <span className="text-sm font-semibold">{table.title}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-white/10 text-[11px] uppercase tracking-wider text-gray-500">
              <th
                className="px-4 py-2 text-left font-medium cursor-pointer select-none hover:text-gray-300"
                onClick={() => clickSort("player_name")}
                title="Sort by player"
              >
                Player
                {sortKey === "player_name" && (
                  <span className="ml-1 text-earl-400">{dir === "asc" ? "▲" : "▼"}</span>
                )}
              </th>
              {table.columns.map((c) => (
                <th
                  key={c.key}
                  className="px-3 py-2 text-right font-medium cursor-pointer select-none whitespace-nowrap hover:text-gray-300"
                  onClick={() => clickSort(c.key)}
                  title={`Sort by ${c.label}`}
                >
                  {c.label}
                  {sortKey === c.key && (
                    <span className="ml-1 text-earl-400">{dir === "asc" ? "▲" : "▼"}</span>
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-white/5">
            {rows.map((r, i) => (
              <tr key={`${r.player_id ?? r.player_name ?? i}`} className="hover:bg-white/5">
                <td className="px-4 py-2 whitespace-nowrap">
                  <span className="inline-flex items-center gap-2">
                    {r.position ? (
                      <span className="text-[10px] text-gray-500 w-7 uppercase">{String(r.position)}</span>
                    ) : (
                      <span className="w-7" />
                    )}
                    <span className="font-medium">
                      {r.player_id ? (
                        <Link href={`/${sport}/players/${r.player_id}`} className="hover:text-earl-400">
                          {String(r.player_name)}
                        </Link>
                      ) : (
                        String(r.player_name)
                      )}
                    </span>
                  </span>
                </td>
                {table.columns.map((c) => (
                  <td
                    key={c.key}
                    className={`px-3 py-2 text-right tabular-nums ${
                      sortKey === c.key ? "text-white font-semibold" : "text-gray-200"
                    }`}
                  >
                    {formatCell(c.fmt, r[c.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
