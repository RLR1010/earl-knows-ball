// Per-sport game-preview RSS feed:
//   https://earlknowsball.com/{sport}/articles/previews/feed.xml

import { backendBaseForPath } from "@/lib/backend-url";
import { buildRss, fetchFeedItems, toFeedItems, SITE_URL } from "@/lib/rss";

export const dynamic = "force-dynamic";

const SPORTS: Record<string, string> = { nfl: "NFL", mlb: "MLB", nba: "NBA" };

export async function GET(
  _req: Request,
  ctx: { params: Promise<{ sport: string }> },
) {
  const { sport } = await ctx.params;
  const label = SPORTS[sport];
  if (!label) return new Response("Not found", { status: 404 });

  const base = backendBaseForPath("/seo/feed-data");
  const items = await fetchFeedItems(base, { sport, kind: "previews", limit: 50 });

  const xml = buildRss({
    title: `Earl Knows Ball — ${label} Game Previews`,
    description: `Pre-game ${label} previews and model picks from Earl Knows Ball.`,
    link: `${SITE_URL}/${sport}/articles/previews`,
    feedUrl: `${SITE_URL}/${sport}/articles/previews/feed.xml`,
    items: toFeedItems(items),
  });

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=600, s-maxage=600",
    },
  });
}
