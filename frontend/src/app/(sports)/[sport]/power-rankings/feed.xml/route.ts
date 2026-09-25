import { backendBaseForPath } from "@/lib/backend-url";
import { buildRss, SITE_URL, type FeedItem } from "@/lib/rss";

export const dynamic = "force-dynamic";

const LABELS: Record<string, string> = { nfl: "NFL", mlb: "MLB", nba: "NBA" };

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ sport: string }> },
) {
  const { sport } = await ctx.params;
  const label = LABELS[sport];
  if (!label) return new Response("Not found", { status: 404 });

  let weeks: { season: number; week: number; as_of_date: string | null }[] = [];
  try {
    const base = backendBaseForPath(`/power-rankings/${sport}/weeks`);
    const res = await fetch(`${base}/power-rankings/${sport}/weeks`, {
      next: { revalidate: 600 },
    });
    if (res.ok) weeks = (await res.json()).weeks ?? [];
  } catch {
    weeks = [];
  }

  const items: FeedItem[] = weeks.slice(0, 30).map((w) => ({
    title: `Earl's ${label} Power Rankings — Week ${w.week}${w.season ? ` (${w.season})` : ""}`,
    link: `/${sport}/power-rankings/week/${w.week}?season=${w.season}`,
    description:
      `Earl's points-denominated ${label} team power ratings for Week ${w.week} — ` +
      `every team rated on a neutral field, with weekly movement and Earl's take.`,
    pubDate: w.as_of_date ?? undefined,
    categories: [`${label} Power Rankings`],
  }));

  const xml = buildRss({
    title: `Earl Knows Ball — ${label} Power Rankings`,
    description: `Weekly ${label} power rankings from Earl Knows Ball, updated every week and backed by hard data.`,
    link: `${SITE_URL}/${sport}/power-rankings`,
    feedUrl: `${SITE_URL}/${sport}/power-rankings/feed.xml`,
    items,
  });

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, s-maxage=600, stale-while-revalidate=1200",
    },
  });
}
