import { backendBaseForPath } from "@/lib/backend-url";

/**
 * Server-side structured-data (JSON-LD) builders.
 *
 * These return plain schema.org objects that a server component renders inside
 * a `<script type="application/ld+json">` tag. They're built during SSR so
 * crawlers see rich results (game matchups, article metadata) without running
 * any client JS.
 *
 * All data comes from the backend `/seo/*` endpoints (which own DB access),
 * mirroring exactly what `generateMetadata` uses — so metadata and structured
 * data always agree.
 */

const SITE_URL = "https://earlknowsball.com";

const SPORT_NAME: Record<string, string> = {
  nfl: "NFL Football",
  nba: "NBA Basketball",
  mlb: "MLB Baseball",
};

interface GameMeta {
  sport: string;
  home?: { name: string; abbr: string } | null;
  away?: { name: string; abbr: string } | null;
  date?: string | null;
  status?: string | null;
}

async function fetchSeoJson<T>(path: string): Promise<T | null> {
  try {
    const base = backendBaseForPath(path);
    const res = await fetch(`${base}${path}`, {
      next: { revalidate: 0 },
      cache: "no-store",
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch (err) {
    console.error(`[structured-data] fetch failed: ${path}`, err);
    return null;
  }
}

function sportName(sport: string): string {
  return SPORT_NAME[sport?.toLowerCase()] ?? sport?.toUpperCase() ?? "";
}

async function fetchWriteupTitle(sport: string, identifier: string): Promise<string | null> {
  const meta = await fetchSeoJson<{ title?: string | null }>(
    `/seo/writeup-meta/${sport}/${encodeURIComponent(identifier)}`
  );
  return meta?.title?.trim() || null;
}

/**
 * SportsEvent schema for a game pick-card page.
 * Gives Google rich results (teams, date, status) and confirms these are
 * real sporting events, not thin doors.
 */
export async function gameStructuredData(
  sport: string,
  gameId: string
): Promise<Record<string, unknown> | null> {
  const meta = await fetchSeoJson<GameMeta>(`/seo/game-meta/${sport}/${encodeURIComponent(gameId)}`);
  if (!meta?.home?.name || !meta?.away?.name) return null;

  return {
    "@context": "https://schema.org",
    "@type": "SportsEvent",
    name: `${meta.away.name} at ${meta.home.name}`,
    sport: sportName(sport),
    eventStatus:
      String(meta.status ?? "").toUpperCase() === "SCHEDULED"
        ? "https://schema.org/EventScheduled"
        : "https://schema.org/EventMovedOnline",
    eventAttendanceMode: "https://schema.org/OfflineEventAttendanceMode",
    startDate: meta.date || undefined,
    location: {
      "@type": "Place",
      name: sportName(sport),
    },
    homeTeam: {
      "@type": "SportsTeam",
      name: meta.home.name,
      identifier: meta.home.abbr,
    },
    awayTeam: {
      "@type": "SportsTeam",
      name: meta.away.name,
      identifier: meta.away.abbr,
    },
    url: `${SITE_URL}/${sport}/games/${gameId}`,
    organizer: {
      "@type": "Organization",
      name: "Earl Knows Ball",
      url: SITE_URL,
    },
  };
}

/**
 * Article schema for an original article or analysis writeup page.
 */
export async function articleStructuredData(
  sport: string,
  slug: string,
  title: string,
  description: string,
  datePublished?: string | null
): Promise<Record<string, unknown>> {
  return {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: title,
    description,
    image: `${SITE_URL}/og-image.png`,
    datePublished: datePublished || new Date().toISOString(),
    dateModified: datePublished || new Date().toISOString(),
    author: {
      "@type": "Organization",
      name: "Earl Knows Ball",
      url: SITE_URL,
    },
    publisher: {
      "@type": "Organization",
      name: "Earl Knows Ball",
      url: SITE_URL,
    },
    mainEntityOfPage: {
      "@type": "WebPage",
      "@id": `${SITE_URL}/${sport}/articles/${slug}`,
    },
  };
}

/**
 * Article schema for a writeup / analysis detail page.
 * Reuses the writeup headline as the Article headline.
 */
export async function writeupStructuredData(
  sport: string,
  identifier: string
): Promise<Record<string, unknown> | null> {
  const title = await fetchWriteupTitle(sport, identifier);
  if (!title) return null;
  return articleStructuredData(sport, identifier, title, `AI handicapping analysis: ${title}`);
}

/** Persist schema.org root-level entity (homepage). */
export function websiteStructuredData(): Record<string, unknown> {
  return {
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: "Earl Knows Ball",
    url: SITE_URL,
    description:
      "AI-powered sports handicapping for NFL, MLB, and NBA — picks with probabilities, betting lines, trends, and a chat handicapper.",
    publisher: {
      "@type": "Organization",
      name: "Earl Knows Ball",
      url: SITE_URL,
    },
  };
}
