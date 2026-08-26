import { backendBaseForPath } from "@/lib/backend-url";

/**
 * Server-side SEO metadata resolvers.
 *
 * These run inside `generateMetadata()` (server components) so the crawler
 * sees the REAL title/description in the raw HTML — not a title that's only
 * injected client-side after hydration (which non-JS crawlers and Google's
 * initial HTML pass never execute).
 *
 * Every resolver talks to the backend's `/seo/*` endpoints (which own all
 * DB access) and falls back to a sensible generic title on any failure —
 * metadata resolution must NEVER break the page render.
 */

const SITE_URL = "https://earlknowsball.com";

/** Full Metadata-building helper: title/desc + canonical + OG + twitter. */
const OG_IMAGE = `${SITE_URL}/og-image.png`;

function buildMeta(opts: {
  title: string;
  description: string;
  url: string;
}): {
  title: string;
  description: string;
  alternates: { canonical: string };
  openGraph: { title: string; description: string; url: string; images?: string[]; siteName?: string; type?: string };
  twitter: { title: string; description: string; card?: string };
} {
  const esctitle = (t: string) => t;
  return {
    title: opts.title,
    description: opts.description,
    alternates: { canonical: opts.url },
    openGraph: {
      title: esctitle(opts.title),
      description: opts.description,
      url: opts.url,
      images: [OG_IMAGE],
      siteName: "Earl Knows Ball",
      type: "website",
    },
    twitter: {
      title: esctitle(opts.title),
      description: opts.description,
      card: "summary_large_image",
    },
  };
}

/** Consistent per-page URL helper. */
const url = (p: string) => `${SITE_URL}${p}`;

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
    console.error(`[seo-content] fetch failed: ${path}`, err);
    return null;
  }
}

/** Consistent site-level suffix so every title is on-brand. */
const SPORT_LABEL: Record<string, string> = {
  nfl: "NFL",
  nba: "NBA",
  mlb: "MLB",
};

function sportLabel(sport: string): string {
  return SPORT_LABEL[sport?.toLowerCase()] ?? sport?.toUpperCase() ?? "";
}

/** Base title metadata fragments shared across content resolvers. */
const BASE = {
  site: "Earl Knows Ball",
  description: (title: string) =>
    `${title} — AI-powered handicapping, odds, predictions and analysis on Earl Knows Ball.`,
};

/**
 * Metadata for a game pick-card / prediction page.
 * Title: "Chicago Bears vs Tennessee Titans Prediction, Odds & Picks"
 */
export async function gameMetadata(
  sport: string,
  gameId: string
): Promise<{ title: string; description: string; canonical?: string }> {
  const label = sportLabel(sport);
  const meta = await fetchSeoJson<GameMeta>(`/seo/game-meta/${sport}/${encodeURIComponent(gameId)}`);
  const home = meta?.home?.name;
  const away = meta?.away?.name;
  if (home && away) {
    return buildMeta({
      title: `${home} vs ${away} Prediction, Odds & Picks`,
      description: BASE.description(`${home} vs ${away} prediction, odds and AI-powered picks`),
      url: url(`/${sport}/games/${gameId}`),
    });
  }
  return buildMeta({
    title: `Game ${gameId} Prediction, Odds & Picks`,
    description: BASE.description(`Game ${gameId} prediction and odds`),
    url: url(`/${sport}/games/${gameId}`),
  });
}

/**
 * Metadata for a team page.
 * Title: "Chicago Bears — NFL Team: Schedule, Odds & Stats"
 */
export async function teamMetadata(
  sport: string,
  abbr: string
): Promise<{ title: string; description: string; canonical?: string }> {
  const label = sportLabel(sport);
  const meta = await fetchSeoJson<{ name?: string | null }>(
    `/seo/team-meta/${sport}/${encodeURIComponent(abbr)}`
  );
  const name = meta?.name || abbr.toUpperCase();
  return buildMeta({
    title: `${name} — ${label} Team: Schedule, Odds & Stats`,
    description: BASE.description(
      `${name} ${label} schedule, odds, depth chart, roster and stats`
    ),
    url: url(`/${sport}/teams/${abbr.toUpperCase()}`),
  });
}

/**
 * Metadata for a writeup / analysis page.
 * Title: the writeup's own title (e.g. "Seahawks at Titans: Preseason Week 2 Preview").
 */
export async function writeupMetadata(
  sport: string,
  identifier: string
): Promise<{ title: string; description: string; canonical?: string }> {
  const label = sportLabel(sport);
  const meta = await fetchSeoJson<{ title?: string | null }>(
    `/seo/writeup-meta/${sport}/${encodeURIComponent(identifier)}`
  );
  const writeupTitle = meta?.title?.trim();
  if (writeupTitle) {
    return buildMeta({
      title: writeupTitle,
      description: BASE.description(writeupTitle),
      url: url(`/${sport}/analysis/${identifier}`),
    });
  }
  return buildMeta({
    title: `${label} Analysis`,
    description: BASE.description(`${label} game analysis and writeups`),
    url: url(`/${sport}/analysis/${identifier}`),
  });
}

/**
 * Metadata for a static sport hub page (e.g. /nfl/schedule, /mlb/teams).
 * No per-page data fetch needed — just a sport-aware title.
 */
export function hubMetadata(
  sport: string,
  noun: string,
  descriptionTemplate: string,
  path = "/" + sport.toLowerCase()
): {
  title: string;
  description: string;
  alternates: { canonical: string };
  openGraph: { title: string; description: string; url: string; images?: string[]; siteName?: string; type?: string };
  twitter: { title: string; description: string; card?: string };
} {
  const label = sportLabel(sport);
  return buildMeta({
    title: `${label} ${noun}`,
    description: descriptionTemplate.replace("{label}", label),
    url: url(path),
  });
}
