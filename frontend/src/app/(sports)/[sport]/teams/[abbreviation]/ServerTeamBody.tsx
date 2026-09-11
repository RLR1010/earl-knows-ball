import Link from "next/link";
import type { TeamContent } from "@/lib/seo-content";

const SPORT_NAMES: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

function fmtDate(value?: string | null): string {
  if (!value) return "";
  const d = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
}

/**
 * SERVER-RENDERED team header + schedule (SEO only).
 *
 * Publishes NON-PREMIUM team facts — name and season schedule/results — into the
 * initial HTML so crawlers see real per-team content on the first wave.
 *
 * ⚠️ Deliberately EXCLUDES picks / spreads / totals / moneylines / EV: those are
 * premium and must never enter public HTML. Do NOT add them here.
 */
export default function ServerTeamBody({ content }: { content: TeamContent }) {
  const { sport, abbr, name, games } = content;
  const label = SPORT_NAMES[sport.toLowerCase()] ?? sport.toUpperCase();
  if (!name && games.length === 0) return null;

  const wins = games.filter((g) => g.teamScore != null && g.oppScore != null && g.teamScore > g.oppScore).length;
  const losses = games.filter((g) => g.teamScore != null && g.oppScore != null && g.teamScore < g.oppScore).length;
  const hasResults = wins + losses > 0;

  return (
    <section className="max-w-5xl mx-auto px-4 pt-10" aria-label="Team summary">
      <div className="text-sm text-gray-500 mb-4">
        <Link href={`/${sport}`} className="hover:text-earl-400 transition">
          {label}
        </Link>
        <span className="mx-2 text-gray-600">·</span>
        <Link href={`/${sport}/teams`} className="hover:text-earl-400 transition">
          Teams
        </Link>
      </div>

      {/*
        No <h1> here on purpose: the TeamClient component renders the page's
        canonical <h1> (team name). Emitting a second h1 caused DUPLICATE h1s on
        every team page. This block contributes the indexable BODY (record +
        schedule) instead.
      */}
      <h2 className="text-2xl font-bold tracking-tight">
        {name ?? abbr} {hasResults ? <span className="text-gray-400 font-normal">({wins}-{losses})</span> : null}
      </h2>
      <p className="text-sm text-gray-500 mt-2">
        {label} team schedule and results{name ? ` for the ${name}` : ""}.
      </p>

      {games.length > 0 && (
        <div className="mt-6">
          <h2 className="text-xl font-bold text-white mb-3">Schedule &amp; Results</h2>
          <table className="w-full text-sm border border-white/10 rounded-lg overflow-hidden">
            <thead className="bg-white/5 text-gray-400">
              <tr>
                <th className="text-left px-3 py-2 font-medium">Date</th>
                <th className="text-left px-3 py-2 font-medium">Opponent</th>
                <th className="text-center px-3 py-2 font-medium">Result</th>
              </tr>
            </thead>
            <tbody>
              {games.map((g, i) => {
                const played = g.teamScore != null && g.oppScore != null;
                const win = played && (g.teamScore as number) > (g.oppScore as number);
                return (
                  <tr key={`${g.date}-${g.opponent}-${i}`} className="border-t border-white/5">
                    <td className="px-3 py-2 text-gray-400 whitespace-nowrap">{fmtDate(g.date)}</td>
                    <td className="px-3 py-2 text-gray-200">
                      <span className="text-gray-500 mr-1">{g.home ? "vs" : "@"}</span>
                      {g.opponent}
                    </td>
                    <td className="px-3 py-2 text-center">
                      {played ? (
                        <span className={win ? "text-green-400 font-semibold" : "text-red-400"}>
                          {win ? "W" : "L"} {g.teamScore}-{g.oppScore}
                        </span>
                      ) : (
                        <span className="text-gray-500">{g.status ?? "Scheduled"}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
