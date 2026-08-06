import { NextRequest, NextResponse } from "next/server";

// Sport list mirroring the backend's valid sports.
const SPORTS = new Set(["mlb", "nfl", "nba"]);

/**
 * SEO redirects:
 *  /{sport}/articles/{numericId}  ->  301 to /{sport}/articles/{slug}
 *
 * Old links (and anything still pointing at a numeric article id) get a
 * permanent 301 to the SEO-friendly date+title slug so search engines and
 * users consolidate on a single canonical URL.
 */
export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  const m = pathname.match(/^\/(mlb|nfl|nba)\/articles\/(\d+)$/);
  if (!m) return NextResponse.next();

  const [, sport, id] = m;

  try {
    const res = await fetch(`http://localhost:8001/original-articles/${sport}/${id}`, {
      headers: { authorization: req.headers.get("authorization") || "" },
      next: { revalidate: 0 },
    });
    if (res.ok) {
      const data = await res.json();
      const slug = data?.slug;
      if (slug) {
        const url = new URL(pathname, req.url);
        url.pathname = `/${sport}/articles/${slug}`;
        // Permanent redirect (301) so search engines consolidate on the slug URL.
        return NextResponse.redirect(url, 301);
      }
    }
  } catch {
    /* fall through to the page on lookup errors */
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/mlb/articles/:path*", "/nfl/articles/:path*", "/nba/articles/:path*"],
};
