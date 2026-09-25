import Link from "next/link";
import { getCurrent, getLatestArticle } from "@/lib/power-rankings";

const SPORT_LABEL: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

function fmtDelta(v: number | null | undefined): string | null {
  if (v === null || v === undefined) return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return null;
  return `${n > 0 ? "+" : ""}${n.toFixed(2)}`;
}

/**
 * Power Rankings feature for a sport home page.
 *
 * Shows the current top five (rank / team / rating / weekly change) plus the
 * opening of this week's power-rankings column, linking through to the full
 * rankings page and the article. Renders nothing if we have neither yet.
 */
export default async function PowerRankingsFeature({ sport }: { sport: string }) {
  // Fault-tolerant: a backend hiccup must never take down the sport home page.
  const [current, article] = await Promise.all([
    getCurrent(sport).catch(() => null),
    getLatestArticle(sport).catch(() => null),
  ]);

  const teams = (current?.teams ?? [])
    .slice()
    .sort((a, b) => a.rank - b.rank)
    .slice(0, 5);
  if (teams.length === 0 && !article) return null;

  const label = SPORT_LABEL[sport] ?? sport.toUpperCase();
  const lede = (article?.summary ?? "").trim();

  return (
    <section className="overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03]">
      <div className="flex flex-col gap-6 p-5 sm:p-6 lg:flex-row lg:gap-10">
        {/* Left: the top five */}
        <div className="lg:w-[44%]">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-xl font-bold text-white">Earl&apos;s {label} Power Rankings</h2>
            <Link
              href={`/${sport}/power-rankings`}
              className="whitespace-nowrap text-sm font-semibold text-emerald-400 hover:text-emerald-300"
            >
              Full rankings →
            </Link>
          </div>
          {current?.season ? (
            <p className="mt-1 text-xs text-gray-500">
              {label} {current.season} · Week {current.week}
              {current.as_of_date ? ` · as of ${current.as_of_date}` : ""}
            </p>
          ) : null}

          <ol className="mt-4 space-y-1.5">
            {teams.map((t) => {
              const d = fmtDelta(t.rating_delta);
              return (
                <li key={t.team.abbr} className="flex items-center gap-3 text-sm">
                  <span className="w-6 text-right font-bold text-gray-400">{t.rank}</span>
                  <span className="w-11 font-bold text-white">{t.team.abbr}</span>
                  <span className="flex-1 truncate text-gray-300">{t.team.name}</span>
                  <span className="font-mono text-gray-200">{Number(t.rating).toFixed(2)}</span>
                  <span
                    className={`w-14 text-right text-xs ${
                      d && d.startsWith("-") ? "text-red-400" : "text-emerald-400"
                    }`}
                  >
                    {d ?? ""}
                  </span>
                </li>
              );
            })}
          </ol>
        </div>

        {/* Right: the beginning of this week's column */}
        {article ? (
          <div className="lg:flex-1 lg:border-l lg:border-white/10 lg:pl-10">
            <p className="text-xs font-semibold uppercase tracking-wider text-emerald-400">
              This week&apos;s column
            </p>
            <h3 className="mt-2 text-lg font-bold leading-snug text-white">
              <Link href={`/${sport}/articles/${article.slug}`} className="hover:text-emerald-300">
                {article.title}
              </Link>
            </h3>
            {lede ? <p className="mt-3 text-sm leading-relaxed text-gray-300">{lede}</p> : null}
            <Link
              href={`/${sport}/articles/${article.slug}`}
              className="mt-4 inline-block text-sm font-semibold text-emerald-400 hover:text-emerald-300"
            >
              Read the full column →
            </Link>
          </div>
        ) : null}
      </div>
    </section>
  );
}
