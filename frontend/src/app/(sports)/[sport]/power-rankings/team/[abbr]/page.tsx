import Link from "next/link";
import type { Metadata } from "next";

import TeamLogo from "@/components/TeamLogo";
import { getTeam } from "@/lib/power-rankings";
import { sportLabel } from "@/lib/seo-content";

type Props = {
  params: Promise<{ sport: string; abbr: string }>;
  searchParams: Promise<{ season?: string }>;
};

export const revalidate = 600;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport, abbr } = await params;
  const label = sportLabel(sport);
  const code = abbr.toUpperCase();
  const title = `${code} ${label} Power Ranking — Earl Knows Ball`;
  const description = `Earl's points-denominated ${label} power rating for the ${code}, with week-by-week movement and the take behind the number.`;
  const url = `/${sport}/power-rankings/team/${code}`;
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: { title, description, url, siteName: "Earl Knows Ball", type: "article" },
  };
}

/** Minimal inline SVG line chart of a rating series. Values already sorted by week. */
function RatingSpark({ points }: { points: { week: number; rating: number }[] }) {
  if (points.length < 2) return null;
  const W = 640;
  const H = 160;
  const pad = 24;
  const rs = points.map((p) => p.rating);
  const min = Math.min(...rs);
  const max = Math.max(...rs);
  const span = max - min || 1;
  const x = (i: number) => pad + (i * (W - 2 * pad)) / (points.length - 1);
  const y = (v: number) => H - pad - ((v - min) * (H - 2 * pad)) / span;
  const line = points.map((p, i) => `${x(i)},${y(p.rating)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-40" role="img" aria-label="Rating over weeks">
      <line x1={pad} y1={H - pad} x2={W - pad} y2={H - pad} stroke="rgba(255,255,255,0.15)" />
      <polyline points={line} fill="none" stroke="#34d399" strokeWidth={2.5} />
      {points.map((p, i) => (
        <circle key={p.week} cx={x(i)} cy={y(p.rating)} r={3} fill="#34d399" />
      ))}
      <text x={pad} y={16} fill="#9ca3af" fontSize="11">
        {max.toFixed(1)} pts
      </text>
      <text x={pad} y={H - 6} fill="#9ca3af" fontSize="11">
        {min.toFixed(1)} pts
      </text>
    </svg>
  );
}

export default async function PowerRankingTeam({ params, searchParams }: Props) {
  const { sport, abbr } = await params;
  const { season } = await searchParams;
  const code = abbr.toUpperCase();
  const data = await getTeam(sport, code);
  const label = sportLabel(sport);
  const sn = season ? parseInt(season, 10) : data?.current?.season;
  const hist = (data?.history ?? []).filter((h) => !sn || h.season === sn);

  if (!data) {
    return (
      <div className="max-w-3xl mx-auto px-4 py-8">
        <h1 className="text-2xl font-bold text-white">
          {code} {label} Power Ranking
        </h1>
        <p className="text-gray-400 mt-2">No power ranking published for {code} yet.</p>
        <Link href={`/${sport}/power-rankings`} className="text-emerald-400 hover:underline">
          ← All {label} power rankings
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-3xl mx-auto px-4 py-8">
      <nav className="text-sm text-gray-500 mb-2">
        <Link href={`/${sport}/power-rankings`} className="hover:text-emerald-400">
          {label} Power Rankings
        </Link>
        <span className="mx-1">/</span>
        <span>{code}</span>
      </nav>

      <header className="flex items-center gap-3 mb-4">
        <TeamLogo abbr={code} sport={sport} name={data.team.name} size={44} />
        <div>
          <h1 className="text-2xl font-bold text-white">{data.team.name}</h1>
          <p className="text-gray-400 text-sm">
            Earl&apos;s {label} power ranking · Week {data.current.week}, {data.current.season}
          </p>
        </div>
      </header>

      <div className="flex items-baseline gap-6 mb-4">
        <div>
          <div className="text-4xl font-bold text-emerald-300">#{data.current.rank}</div>
          <div className="text-xs text-gray-500">rank</div>
        </div>
        <div>
          <div className="text-4xl font-bold text-white">
            {data.current.rating > 0 ? "+" : ""}
            {data.current.rating.toFixed(1)}
          </div>
          <div className="text-xs text-gray-500">rating (points, neutral field)</div>
        </div>
        {hist.length ? (
          <div>
            <div className="text-4xl font-bold text-gray-300">
              {hist[hist.length - 1].record ?? "—"}
            </div>
            <div className="text-xs text-gray-500">record ({sn})</div>
          </div>
        ) : null}
        {data.current.market_delta != null ? (
          <div>
            <div
              className={`text-4xl font-bold ${
                data.current.market_delta > 0
                  ? "text-emerald-300"
                  : data.current.market_delta < 0
                    ? "text-rose-400"
                    : "text-gray-300"
              }`}
            >
              {data.current.market_delta > 0 ? "+" : ""}
              {data.current.market_delta.toFixed(1)}
            </div>
            <div className="text-xs text-gray-500">vs market (Earl \u2212 line)</div>
          </div>
        ) : null}
      </div>

      {data.current.blurb ? (
        <p className="text-gray-200 mb-6 max-w-prose">{data.current.blurb}</p>
      ) : null}

      {data.current.injuries && data.current.injuries.length > 0 ? (
        <section className="mb-8 rounded-lg border border-amber-500/20 bg-amber-500/5 p-4">
          <h2 className="text-sm font-semibold text-amber-300 mb-2">
            Unavailable this week
            {data.current.injury_adj ? (
              <span className="ml-2 font-normal text-amber-200/80">
                (-{data.current.injury_adj.toFixed(1)} {sport === "mlb" ? "runs" : "pts"} of rating)
              </span>
            ) : null}
          </h2>
          <ul className="space-y-1 text-sm text-gray-200">
            {data.current.injuries.map((p) => (
              <li key={p.player_id}>
                <span className="text-white">{p.name}</span>
                <span className="text-gray-500">
                  {" \u00b7 "}
                  {p.position}
                  {" \u00b7 "}
                  {p.report_status || p.practice_status || "?"}
                </span>
                <span className="ml-2 font-mono text-amber-300">-{p.value.toFixed(1)}</span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-gray-500">
            Points are how much Earl docks a team&apos;s rating for each unavailable player.
          </p>
        </section>
      ) : null}

      {hist.length > 1 ? (
        <section className="mb-8">
          <h2 className="text-sm font-semibold text-gray-400 mb-1">
            Rating over the {sn} season
          </h2>
          <RatingSpark points={hist.filter((h) => h.season === sn).map((h) => ({ week: h.week, rating: h.rating }))} />
        </section>
      ) : null}

      <section>
        <h2 className="text-sm font-semibold text-gray-400 mb-2">Week by week</h2>
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-400 border-b border-white/10">
              <th className="py-2">Season</th>
              <th className="py-2">Wk</th>
              <th className="py-2 text-right">Rank</th>
              <th className="py-2 text-right">Rec</th>
              <th className="py-2 text-right">Rating</th>
              <th className="py-2 text-right">Inj</th>
              <th className="py-2 text-right">vs Mkt</th>
              <th className="py-2 text-right">Δ</th>
            </tr>
          </thead>
          <tbody>
            {hist.map((h) => (
              <tr key={`${h.season}-${h.week}`} className="border-b border-white/5">
                <td className="py-2 text-gray-400">{h.season}</td>
                <td className="py-2">
                  <Link
                    href={`/${sport}/power-rankings/week/${h.week}?season=${h.season}`}
                    className="text-emerald-400 hover:underline"
                  >
                    {h.week}
                  </Link>
                </td>
                <td className="py-2 text-right text-gray-300">#{h.rank}</td>
                <td className="py-2 text-right font-mono text-gray-400">{h.record ?? "—"}</td>
                <td className="py-2 text-right font-mono text-gray-300">
                  {h.rating > 0 ? "+" : ""}
                  {h.rating.toFixed(1)}
                </td>
                <td className="py-2 text-right font-mono text-amber-400/80">
                  {h.injury_adj ? `-${h.injury_adj.toFixed(1)}` : "—"}
                </td>
                <td
                  className={`py-2 text-right font-mono ${
                    h.market_delta == null
                      ? "text-gray-600"
                      : h.market_delta > 0
                        ? "text-emerald-400/80"
                        : h.market_delta < 0
                          ? "text-rose-400/80"
                          : "text-gray-500"
                  }`}
                >
                  {h.market_delta == null
                    ? "—"
                    : `${h.market_delta > 0 ? "+" : ""}${h.market_delta.toFixed(1)}`}
                </td>
                <td className="py-2 text-right font-mono text-gray-500">
                  {h.rating_delta === null ? "—" : `${h.rating_delta > 0 ? "+" : ""}${h.rating_delta.toFixed(1)}`}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}
