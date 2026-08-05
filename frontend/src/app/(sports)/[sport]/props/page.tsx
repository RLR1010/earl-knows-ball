"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";

// ── Types ───────────────────────────────────────────────────────────────────

interface TeamProp {
  team_name: string | null;
  abbreviation: string | null;
  win_total: number | null;
  win_total_over_odds: number | null;
  win_total_under_odds: number | null;
  championship_odds: number | null;
  make_playoffs_odds: number | null;
  miss_playoffs_odds: number | null;
}

interface SeasonProp {
  player_name: string;
  prop_type: string;
  odds: number | null;
  implied_probability?: number | null;
  team_name?: string | null;
  abbreviation?: string | null;
}

interface PropsResponse {
  sport: string;
  team_props: TeamProp[];
  player_season_props: SeasonProp[];
}

// ── Helpers ────────────────────────────────────────────────────────────────

const PROP_LABELS: Record<string, string> = {
  championship: "Championship",
  make_playoffs: "Make Playoffs",
  miss_playoffs: "Miss Playoffs",
  "win-total": "Win Total",
  win_total: "Win Total",
  mvp: "MVP",
  mvp_al: "AL MVP",
  mvp_nl: "NL MVP",
  cy_young: "Cy Young",
  cy_young_al: "AL Cy Young",
  cy_young_nl: "NL Cy Young",
  rookie: "Rookie of the Year",
  rookie_of_year: "Rookie of the Year",
  rookie_al: "AL Rookie of the Year",
  rookie_nl: "NL Rookie of the Year",
  dpoy: "Defensive Player of the Year",
  opoy: "Offensive Player of the Year",
  coy: "Coach of the Year",
  comeback_player: "Comeback Player of the Year",
  sixth_man: "Sixth Man of the Year",
  most_improved: "Most Improved Player",
};

function propLabel(pt: string): string {
  if (PROP_LABELS[pt]) return PROP_LABELS[pt];
  return pt.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

function formatOdds(o: number | null | undefined): string {
  if (o == null) return "—";
  if (o === 0) return "0";
  return o > 0 ? `+${o}` : `${o}`;
}

function OddsPill({ odds }: { odds?: number | null }) {
  if (odds == null)
    return <span className="text-xs text-gray-500">—</span>;
  const negative = odds < 0;
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-semibold tabular-nums ${
        negative ? "bg-red-500/15 text-red-300" : "bg-green-500/15 text-green-300"
      }`}
    >
      {formatOdds(odds)}
    </span>
  );
}

function TeamCard({ tp }: { tp: TeamProp }) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/5 overflow-hidden">
      <div className="px-4 py-2.5 bg-white/10 flex items-baseline gap-2">
        <span className="font-semibold text-white">{tp.team_name ?? tp.abbreviation ?? "—"}</span>
        {tp.abbreviation && (
          <span className="text-[11px] uppercase tracking-wide text-gray-400">{tp.abbreviation}</span>
        )}
      </div>
      <div className="divide-y divide-white/5">
        {tp.win_total != null && (
          <div className="px-4 py-2 flex items-center justify-between">
            <span className="text-sm text-gray-300">Win Total</span>
            <span className="flex items-center gap-2">
              <span className="text-sm font-semibold text-white tabular-nums">
                O/U {tp.win_total}
              </span>
              <OddsPill odds={tp.win_total_over_odds} />
              <OddsPill odds={tp.win_total_under_odds} />
            </span>
          </div>
        )}
        {tp.make_playoffs_odds != null && (
          <div className="px-4 py-2 flex items-center justify-between">
            <span className="text-sm text-gray-300">Make Playoffs</span>
            <OddsPill odds={tp.make_playoffs_odds} />
          </div>
        )}
        {tp.miss_playoffs_odds != null && (
          <div className="px-4 py-2 flex items-center justify-between">
            <span className="text-sm text-gray-300">Miss Playoffs</span>
            <OddsPill odds={tp.miss_playoffs_odds} />
          </div>
        )}
        {tp.championship_odds != null && (
          <div className="px-4 py-2 flex items-center justify-between">
            <span className="text-sm text-gray-300">Championship</span>
            <OddsPill odds={tp.championship_odds} />
          </div>
        )}
      </div>
    </div>
  );
}

function groupByLabel(rows: SeasonProp[]): Record<string, SeasonProp[]> {
  const grouped: Record<string, SeasonProp[]> = {};
  for (const r of rows) {
    (grouped[r.prop_type] = grouped[r.prop_type] || []).push(r);
  }
  // Ordered keys: non-player awards first, then player awards
  const order = ["championship", "mvp", "mvp_al", "mvp_nl", "cy_young_al", "cy_young_nl",
    "dpoy", "opoy", "sixth_man", "most_improved", "comeback_player", "rookie_of_year",
    "rookie_al", "rookie_nl"];
  const keys = Object.keys(grouped).sort(
    (a, b) => (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b))
  );
  return keys.reduce<Record<string, SeasonProp[]>>((acc, k) => {
    acc[k] = grouped[k];
    return acc;
  }, {});
}

// ── Page ────────────────────────────────────────────────────────────────────

export default function PropsPage() {
  const { sport } = useParams<{ sport: string }>();
  const [data, setData] = useState<PropsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const apiPath =
    sport === "mlb"
      ? `/api/mlb/props`
      : sport === "nba"
      ? `/api/nba/props`
      : `/api/props`;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(apiPath)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((d: PropsResponse) => {
        if (!cancelled) setData(d);
      })
      .catch(e => {
        if (!cancelled) setError(e.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [apiPath]);

  const grouped = useMemo(() => (data ? groupByLabel(data.player_season_props) : {}), [data]);

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      <header className="mb-6">
        <h1 className="text-3xl font-bold text-white">Props</h1>
        <p className="text-sm text-gray-400 mt-1">
          Season-long futures and awards betting odds for {sport?.toUpperCase()}.
        </p>
      </header>

      {loading && (
        <div className="py-20 text-center">
          <div className="animate-pulse text-sm text-gray-400">Loading props…</div>
        </div>
      )}

      {!loading && error && (
        <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-6 text-red-200 text-sm">
          Unable to load props: {error}
        </div>
      )}

      {!loading && !error && data && (
        <>
          {/* Team props */}
          <section className="mb-10">
            <h2 className="text-xl font-semibold text-white mb-4">Team Futures</h2>
            {data.team_props.length === 0 ? (
              <p className="text-sm text-gray-500">No team futures available.</p>
            ) : (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                {data.team_props.map((tp) => (
                  <TeamCard key={tp.team_name ?? tp.abbreviation} tp={tp} />
                ))}
              </div>
            )}
          </section>

          {/* Player season props (awards) */}
          <section>
            <h2 className="text-xl font-semibold text-white mb-4">Awards &amp; Season Props</h2>
            {Object.keys(grouped).length === 0 ? (
              <p className="text-sm text-gray-500">No player season props available.</p>
            ) : (
              <div className="space-y-8">
                {Object.entries(grouped).map(([ptype, rows]) => (
                  <div key={ptype}>
                    <h3 className="text-sm font-medium uppercase tracking-wide text-gray-400 mb-2">
                      {propLabel(ptype)}
                    </h3>
                    <div className="rounded-xl border border-white/10 bg-white/5 overflow-hidden divide-y divide-white/5">
                      {rows.map((r) => {
                        const key = `${r.player_name}-${ptype}`;
                        return (
                          <div key={key} className="px-4 py-2 flex items-center justify-between">
                            <div className="flex items-baseline gap-2">
                              <span className="text-sm font-medium text-white">{r.player_name}</span>
                              {r.team_name && (
                                <span className="text-xs text-gray-400">{r.team_name}</span>
                              )}
                              {r.abbreviation && (
                                <span className="text-[11px] uppercase text-gray-500">{r.abbreviation}</span>
                              )}
                            </div>
                            <OddsPill odds={r.odds} />
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}
