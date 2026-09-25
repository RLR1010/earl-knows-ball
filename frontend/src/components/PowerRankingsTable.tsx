import Link from "next/link";

import TeamLogo from "@/components/TeamLogo";
import type { PRTeam } from "@/lib/power-rankings";

function fmtPts(v: number | null, showPlus = true): string {
  if (v === null || v === undefined) return "—";
  const s = v.toFixed(1);
  return showPlus && v > 0 ? `+${s}` : s;
}

function Movement({ delta }: { delta: number | null }) {
  if (delta === null || delta === undefined) return <span className="text-gray-500">—</span>;
  if (delta > 0) return <span className="text-emerald-400">▲{delta}</span>;
  if (delta < 0) return <span className="text-red-400">▼{Math.abs(delta)}</span>;
  return <span className="text-gray-500">–</span>;
}

export default function PowerRankingsTable({
  sport,
  teams,
  season,
  linkTeams = true,
  showBlurbs = true,
}: {
  sport: string;
  teams: PRTeam[];
  season: number;
  linkTeams?: boolean;
  showBlurbs?: boolean;
}) {
  if (!teams?.length) {
    return (
      <p className="text-gray-400 py-8">
        No power rankings published yet. Check back after the season starts.
      </p>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="text-left text-gray-400 border-b border-white/10">
            <th className="py-2 pr-4 font-semibold">#</th>
            <th className="py-2 pr-4 font-semibold">Team</th>
            <th className="py-2 pr-4 font-semibold text-right">Rec</th>
            <th className="py-2 pr-4 font-semibold text-right">Rating</th>
            <th className="py-2 pr-4 font-semibold text-right" title="Rating points docked for key players ruled out / doubtful / questionable for this week's game.">Inj</th>
            <th className="py-2 pr-4 font-semibold text-right hidden sm:table-cell">Δ Wk</th>
            <th className="py-2 pr-4 font-semibold text-right hidden md:table-cell" title="Average rating of opponents faced, in points vs the league average. Positive = harder schedule; negative = easier.">SOS</th>
            <th className="py-2 pr-4 font-semibold text-right hidden lg:table-cell" title="Earl's rating minus the market's (from closing lines), on Earl's points scale. Positive = Earl rates this team higher than the market.">vs Mkt</th>
            <th className="py-2 pl-4 font-semibold">Earl&apos;s Take</th>
          </tr>
        </thead>
        <tbody>
          {teams.map((t) => (
            <tr key={t.team.abbr} className="border-b border-white/5 align-top">
              <td className="py-3 pr-4 font-bold text-gray-300">{t.rank}</td>
              <td className="py-3 pr-4">
                <div className="flex items-center gap-2 min-w-[9rem]">
                  <TeamLogo abbr={t.team.abbr} sport={sport} name={t.team.name} size={26} />
                  <div>
                    {linkTeams ? (
                      <Link
                        href={`/${sport}/power-rankings/team/${t.team.abbr}?season=${season}`}
                        className="font-semibold text-white hover:text-emerald-400"
                      >
                        {t.team.name}
                      </Link>
                    ) : (
                      <span className="font-semibold text-white">{t.team.name}</span>
                    )}
                  </div>
                </div>
                <div className="text-xs text-gray-500 mt-0.5 sm:hidden">
                  Δ <Movement delta={t.rank_movement} />
                </div>
              </td>
              <td className="py-3 pr-4 text-right font-mono text-gray-300 whitespace-nowrap">
                {t.record ?? "—"}
              </td>
              <td className="py-3 pr-4 text-right font-mono text-emerald-300 whitespace-nowrap">
                {fmtPts(t.rating)}
              </td>
              <td
                className="py-3 pr-4 text-right font-mono whitespace-nowrap"
                title={(t.injuries ?? [])
                  .map(
                    (p) =>
                      `${p.name} (${p.position}, ${
                        p.report_status || p.practice_status || "?"
                      })  -${p.value}`,
                  )
                  .join("\n")}
              >
                {t.injury_adj && t.injury_adj > 0 ? (
                  <span className="text-amber-400">-{t.injury_adj.toFixed(1)}</span>
                ) : (
                  <span className="text-gray-600">—</span>
                )}
              </td>
              <td className="py-3 pr-4 text-right hidden sm:table-cell whitespace-nowrap">
                <Movement delta={t.rank_movement} />
                <span className="text-xs text-gray-500 ml-2">{fmtPts(t.rating_delta)}</span>
              </td>
              <td className="py-3 pr-4 text-right hidden md:table-cell font-mono text-gray-400">
                {fmtPts(t.sos)}
              </td>
              <td
                className="py-3 pr-4 text-right hidden lg:table-cell font-mono whitespace-nowrap"
                title="Earl's rating minus the market-implied rating (closing lines), both on Earl's points scale."
              >
                {t.market_delta == null ? (
                  <span className="text-gray-600">—</span>
                ) : (
                  <span
                    className={
                      t.market_delta > 0
                        ? "text-emerald-400"
                        : t.market_delta < 0
                          ? "text-rose-400"
                          : "text-gray-400"
                    }
                  >
                    {t.market_delta > 0 ? "+" : ""}
                    {t.market_delta.toFixed(1)}
                  </span>
                )}
              </td>
              <td className="py-3 pl-4 text-gray-300 max-w-prose">
                {showBlurbs && t.blurb ? t.blurb : <span className="text-gray-600">—</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="text-xs text-gray-500 mt-3">
        Rating is in points on a neutral field: a team&apos;s rating minus its opponent&apos;s
        approximates the spread. Rec is the team&apos;s W-L-T through this week. Δ Wk is
        movement vs last week. Inj is the rating points docked for key players ruled
        out/doubtful/questionable this week. SOS is the average rating of opponents faced, in
        points vs league average (negative = easier schedule). vs Mkt is Earl&apos;s rating minus
        the market&apos;s (from closing lines), placed on Earl&apos;s points scale — positive means
        Earl is higher on the team than the market.{" "}
        <Link href={`/${sport}/power-rankings/methodology`} className="text-emerald-400 hover:underline">
          How Earl rates teams →
        </Link>
      </p>
    </div>
  );
}
