// Master RSS feed: https://earlknowsball.com/feed.xml
// All sports — original articles + recaps + game previews, newest first.

import { backendBaseForPath } from "@/lib/backend-url";
import { buildRss, fetchFeedItems, toFeedItems, SITE_URL } from "@/lib/rss";

export const dynamic = "force-dynamic";

export async function GET() {
  const base = backendBaseForPath("/seo/feed-data");
  const items = await fetchFeedItems(base, { sport: "all", kind: "both", limit: 60 });

  const xml = buildRss({
    title: "Earl Knows Ball — NFL, MLB & NBA Articles, Recaps & Previews",
    description:
      "Model-driven NFL, MLB and NBA analysis from Earl Knows Ball: original articles, post-game recaps and game previews.",
    link: SITE_URL,
    feedUrl: `${SITE_URL}/feed.xml`,
    items: toFeedItems(items),
  });

  return new Response(xml, {
    headers: {
      "Content-Type": "application/rss+xml; charset=utf-8",
      "Cache-Control": "public, max-age=600, s-maxage=600",
    },
  });
}
