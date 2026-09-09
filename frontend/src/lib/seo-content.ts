import { backendBaseForPath } from "@/lib/backend-url";
import { gameIdFromSegment } from "@/lib/game-slug";

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

/**
 * Full Metadata-building helper: title/desc + canonical + OG + twitter.
 * When `image` is omitted this falls back to the site-wide generic OG image
 * (og-image.png). Callers that have a real per-page image (e.g. a writeup's
 * social card) should pass it so X/Facebook render the actual card.
 */
function buildMeta(opts: {
  title: string;
  description: string;
  url: string;
  /** Absolute URL of the social card image. Falls back to the site-wide OG image. */
  image?: string;
}): {
  title: string;
  description: string;
  alternates: { canonical: string };
  openGraph: { title: string; description: string; url: string; images?: string[]; siteName?: string; type?: string };
  twitter: { title: string; description: string; card?: string; image?: string };
} {
  const esctitle = (t: string) => t;
  const image = opts.image?.trim() || OG_IMAGE;
  return {
    title: opts.title,
    description: opts.description,
    alternates: { canonical: opts.url },
    openGraph: {
      title: esctitle(opts.title),
      description: opts.description,
      url: opts.url,
      images: [image],
      siteName: "Earl Knows Ball",
      type: "website",
    },
    twitter: {
      title: esctitle(opts.title),
      description: opts.description,
      card: "summary_large_image",
      image,
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
  slug?: string | null;
  description?: string | null;
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
 *
 * The backend /seo/game-meta response carries the canonical slug
 * ({home}-vs-{away}-{date}-{id}) and a rich description. We canonicalize ON
 * the slug URL (the whole point is that canonical = the new readable URL).
 */
export async function gameMetadata(
  sport: string,
  segment: string
): Promise<{ title: string; description: string; canonical?: string }> {
  const label = sportLabel(sport);
  // `segment` is the slug (e.g. chicago-cubs-vs-...-49070) or a legacy numeric
  // id. The backend needs the numeric id, which is always the trailing token.
  const numericId = gameIdFromSegment(segment) ?? segment;
  const meta = await fetchSeoJson<GameMeta>(
    `/seo/game-meta/${sport}/${encodeURIComponent(numericId)}`
  );
  const home = meta?.home?.name;
  const away = meta?.away?.name;
  const slug = meta?.slug;
  // Canonical URL is the slug form (the readable, descriptive URL). Fall back
  // to the numeric id URL if the backend slug is missing.
  const canonicalPath = slug
    ? url(`/${sport}/games/${slug}`)
    : url(`/${sport}/games/${encodeURIComponent(numericId)}`);
  if (home && away) {
    return buildMeta({
      title: `${home} vs ${away} Prediction, Odds & Picks`,
      description: meta?.description
        ? meta.description
        : BASE.description(`${home} vs ${away} prediction, odds and AI-powered picks`),
      url: canonicalPath,
    });
  }
  return buildMeta({
    title: `Game ${numericId} Prediction, Odds & Picks`,
    description: BASE.description(`Game ${numericId} prediction and odds`),
    url: canonicalPath,
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

interface WriteupMeta {
  sport?: string | null;
  identifier?: string | null;
  title?: string | null;
  // Present when `identifier` is an OLD published slug that now maps (via the
  // backend slug_alias table) to a writeup whose canonical slug is canonical_slug.
  canonical_slug?: string | null;
}

/**
 * Resolve whether a writeup identifier is an aliased (old) slug that should be
 * 301 → the live canonical slug. Returns null when no redirect is warranted.
 * Used by the server analysis page so browser links + crawlers converge on the
 * canonical URL instead of endlessly serving the old one.
 */
export async function resolveWriteupRedirect(
  sport: string,
  identifier: string
): Promise<{ canonicalSlug: string } | null> {
  try {
    const meta = await fetchSeoJson<WriteupMeta>(
      `/seo/writeup-meta/${sport}/${encodeURIComponent(identifier)}`
    );
    const canonical = meta?.canonical_slug?.trim();
    if (canonical && canonical !== identifier) {
      return { canonicalSlug: canonical };
    }
  } catch {
    // resolution is best-effort — never break the page
  }
  return null;
}

/**
 * Normalize a card image path from the DB into an absolute crawler-safe URL.
 * The DB stores site-absolute paths like "/writeups/cards/mlb/free-49268.png";
 * X/Facebook/Google need a fully-qualified https URL to render the image.
 */
function absolutizeImage(raw?: string | null): string | undefined {
  const v = raw?.trim();
  if (!v) return undefined;
  if (/^https?:\/\//i.test(v)) return v;
  return `${SITE_URL}${v.startsWith("/") ? v : `/${v}`}`;
}

/**
 * Metadata for a writeup / analysis page.
 * Title: the writeup's own title (e.g. "Seahawks at Titans: Preseason Week 2 Preview").
 * Image: the writeup's social card. When the writeup is the active Free Pick it
 * carries a bespoke `premium_social_card` (promo card meant to be shared);
 * otherwise fall back to its standard `preview_image`. Falls back to the generic
 * OG image only when neither exists.
 */
export async function writeupMetadata(
  sport: string,
  identifier: string
): Promise<{ title: string; description: string; canonical?: string; image?: string }> {
  const label = sportLabel(sport);
  const meta = await fetchSeoJson<{
    title?: string | null;
    preview_image?: string | null;
    premium_social_card?: string | null;
  }>(`/seo/writeup-meta/${sport}/${encodeURIComponent(identifier)}`);
  const writeupTitle = meta?.title?.trim();
  // Premium social card (free-pick promo) wins when present; else the standard
  // per-game card; else undefined so buildMeta falls back to the site OG image.
  const image = absolutizeImage(meta?.premium_social_card) ?? absolutizeImage(meta?.preview_image);
  if (writeupTitle) {
    return buildMeta({
      title: writeupTitle,
      description: BASE.description(writeupTitle),
      url: url(`/${sport}/analysis/${identifier}`),
      image,
    });
  }
  return buildMeta({
    title: `${label} Analysis`,
    description: BASE.description(`${label} game analysis and writeups`),
    url: url(`/${sport}/analysis/${identifier}`),
    image,
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
