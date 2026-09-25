// Per-sport RSS feed: https://earlknowsball.com/{sport}/feed.xml
// Original articles + recaps for one sport, newest first.

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
  const items = await fetchFeedItems(base, { sport, kind: "articles", limit: 50 });

  const xml = buildRss({
    title: `Earl Knows Ball — ${label} Articles & Recaps`,
    description: `Original ${label} articles and post-game recaps from Earl Knows Ball.`,
    link: `${SITE_URL}/${sport}/articles`,
    feedUrl: `${SITE_URL}/${sport}/feed.xml`,
    items: toFeedItems(items),
  });

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=600, s-maxage=600",
    },
  });
}
