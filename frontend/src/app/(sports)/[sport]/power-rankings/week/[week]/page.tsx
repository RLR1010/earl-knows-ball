import Link from "next/link";
import type { Metadata } from "next";

import PowerRankingsTable from "@/components/PowerRankingsTable";
import PowerRankingsWeekPicker from "@/components/PowerRankingsWeekPicker";
import { getWeek, getWeeks } from "@/lib/power-rankings";
import { sportLabel } from "@/lib/seo-content";

type Props = {
  params: Promise<{ sport: string; week: string }>;
  searchParams: Promise<{ season?: string }>;
};

export const revalidate = 600;

export async function generateMetadata({ params, searchParams }: Props): Promise<Metadata> {
  const { sport, week } = await params;
  const { season } = await searchParams;
  const label = sportLabel(sport);
  const title = `${label} Power Rankings — Week ${week}${season ? ` (${season})` : ""}`;
  const description = `Earl's ${label} power rankings for Week ${week}: all teams rated in points on a neutral field, with weekly movement and Earl's take on each team.`;
  const url = `/${sport}/power-rankings/week/${week}${season ? `?season=${season}` : ""}`;
  return {
    title,
    description,
    alternates: { canonical: url, types: { "application/rss+xml": `/${sport}/power-rankings/feed.xml` } },
    openGraph: { title, description, url, siteName: "Earl Knows Ball", type: "article" },
    twitter: { title, description, card: "summary_large_image" },
  };
}

export default async function PowerRankingsWeek({ params, searchParams }: Props) {
  const { sport, week } = await params;
  const { season } = await searchParams;
  const wk = parseInt(week, 10);
  const sn = season ? parseInt(season, 10) : undefined;
  const data = Number.isFinite(wk) ? await getWeek(sport, wk, sn) : null;
  const weeksResp = await getWeeks(sport);
  const label = sportLabel(sport);
  const prev = wk > 1 ? wk - 1 : null;
  const next = wk < 22 ? wk + 1 : null;

  return (
    <div className="max-w-5xl mx-auto px-4 py-8">
      <header className="mb-6">
        <nav className="text-sm text-gray-500 mb-2">
          <Link href={`/${sport}/power-rankings`} className="hover:text-emerald-400">
            {label} Power Rankings
          </Link>
          <span className="mx-1">/</span>
          <span>Week {wk}</span>
        </nav>
        <h1 className="text-3xl font-bold text-white">
          {label} Power Rankings — Week {wk}
          {data?.season ? `, ${data.season}` : ""}
        </h1>
        {data?.as_of_date ? (
          <p className="text-gray-400 mt-1">As of {data.as_of_date}</p>
        ) : null}
      </header>

      {data && (weeksResp?.weeks?.length ?? 0) > 1 ? (
        <PowerRankingsWeekPicker
          sport={sport}
          weeks={weeksResp!.weeks}
          season={data.season}
          week={data.week}
        />
      ) : null}

      <PowerRankingsTable sport={sport} teams={data?.teams ?? []} season={data?.season ?? sn ?? 0} />

      <nav className="mt-8 flex justify-between border-t border-white/10 pt-4 text-sm">
        {prev ? (
          <Link
            href={`/${sport}/power-rankings/week/${prev}${sn ? `?season=${sn}` : ""}`}
            className="text-emerald-400 hover:underline"
          >
            ← Week {prev}
          </Link>
        ) : (
          <span />
        )}
        {next ? (
          <Link
            href={`/${sport}/power-rankings/week/${next}${sn ? `?season=${sn}` : ""}`}
            className="text-emerald-400 hover:underline"
          >
            Week {next} →
          </Link>
        ) : (
          <span />
        )}
      </nav>
    </div>
  );
}
