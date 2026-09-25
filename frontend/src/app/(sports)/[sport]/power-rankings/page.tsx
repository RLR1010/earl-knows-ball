import Link from "next/link";
import type { Metadata } from "next";

import PowerRankingsTable from "@/components/PowerRankingsTable";
import PowerRankingsWeekPicker from "@/components/PowerRankingsWeekPicker";
import JsonLd from "@/components/JsonLd";
import { getCurrent, getWeeks } from "@/lib/power-rankings";
import { hubMetadata, sportLabel } from "@/lib/seo-content";
import { SITE_URL } from "@/lib/rss";

type Props = { params: Promise<{ sport: string }> };

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  const label = sportLabel(sport);
  const base = hubMetadata(
    sport,
    "Power Rankings",
    `Earl's weekly ${label} power rankings — every team rated in points, backed by hard data and updated each week.`,
    `/${sport}/power-rankings`,
    `/${sport}/power-rankings/feed.xml`
  );
  // Attach the weekly rankings card (rendered by the weekly job) as the share image.
  const current = await getCurrent(sport);
  if (current?.season && current?.week) {
    const img = `${SITE_URL}/writeups/cards/${sport}/power-rankings-${current.season}-wk${current.week}.png`;
    return {
      ...base,
      openGraph: { ...(base.openGraph ?? {}), images: [{ url: img, width: 1600, height: 900 }] },
      twitter: { ...(base.twitter ?? {}), images: [img] },
    } as Metadata;
  }
  return base;
}

export const revalidate = 600;

export default async function PowerRankingsHub({ params }: Props) {
  const { sport } = await params;
  const [current, weeksResp] = await Promise.all([getCurrent(sport), getWeeks(sport)]);
  const label = sportLabel(sport);
  const weeks = (weeksResp?.weeks ?? []).filter(
    (w) =>
      w.season >= 2022 &&
      (!current || !(w.season === current.season && w.week === current.week)),
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      {current?.teams?.length ? (
        <JsonLd
          data={{
            "@context": "https://schema.org",
            "@type": "ItemList",
            name: `Earl's ${label} Power Rankings`,
            url: `${SITE_URL}/${sport}/power-rankings`,
            numberOfItems: current.teams.length,
            itemListOrder: "https://schema.org/ItemListOrderDescending",
            itemListElement: current.teams.map((t) => ({
              "@type": "ListItem",
              position: t.rank,
              name: t.team.name,
              url: `${SITE_URL}/${sport}/power-rankings/team/${t.team.abbr}`,
            })),
          }}
        />
      ) : null}
      <header className="mb-6">
        <h1 className="text-3xl font-bold text-white">{label} Power Rankings</h1>
        {current?.season && current?.week ? (
          <p className="text-gray-400 mt-1">
            Week {current.week}, {current.season}
            {current.as_of_date ? ` · as of ${current.as_of_date}` : ""} · all {current.teams.length} teams rated
          </p>
        ) : (
          <p className="text-gray-400 mt-1">Updated weekly by Earl.</p>
        )}
        <p className="text-gray-300 mt-3 max-w-3xl">
          Every team rated in <strong>points on a neutral field</strong> — a rating gap is the
          spread. Movement, strength of schedule, and Earl&apos;s take, re-cut each week.{" "}
          <Link href={`/${sport}/power-rankings/methodology`} className="text-emerald-400 hover:underline">
            How it works
          </Link>
          {" · "}
          <a href={`/${sport}/power-rankings/feed.xml`} className="text-emerald-400 hover:underline">
            RSS
          </a>
        </p>
      </header>

      {current && current.teams.length > 0 && (weeksResp?.weeks?.length ?? 0) > 1 ? (
        <PowerRankingsWeekPicker
          sport={sport}
          weeks={weeksResp!.weeks}
          season={current.season}
          week={current.week}
        />
      ) : null}

      <PowerRankingsTable sport={sport} teams={current?.teams ?? []} season={current?.season ?? 0} />

      {weeks.length > 0 && (
        <nav className="mt-8 border-t border-white/10 pt-4">
          <h2 className="text-sm font-semibold text-gray-400 mb-2">Previous weeks</h2>
          <div className="flex flex-wrap gap-2">
            {weeks.slice(0, 24).map((w) => (
              <Link
                key={`${w.season}-${w.week}`}
                href={`/${sport}/power-rankings/week/${w.week}?season=${w.season}`}
                className="text-xs px-2 py-1 rounded bg-white/5 hover:bg-white/10 text-gray-300"
              >
                {w.season} Wk {w.week}
              </Link>
            ))}
          </div>
        </nav>
      )}
    </div>
  );
}
