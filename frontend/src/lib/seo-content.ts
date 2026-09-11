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
  /** When false, emit `robots: noindex,follow` (paywalled / missing content). */
  indexable?: boolean;
}): {
  title: string;
  description: string;
  alternates: { canonical: string };
  robots?: { index: boolean; follow: boolean };
  openGraph: { title: string; description: string; url: string; images?: string[]; siteName?: string; type?: string };
  twitter: { title: string; description: string; card?: string; image?: string };
} {
  const esctitle = (t: string) => t;
  const image = opts.image?.trim() || OG_IMAGE;
  return {
    title: opts.title,
    description: opts.description,
    alternates: { canonical: opts.url },
    // Default to indexable; only explicit false triggers noindex.
    robots: opts.indexable === false ? { index: false, follow: true } : undefined,
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

/**
 * SEO-safe (NON-PREMIUM) game detail for server rendering.
 *
 * Deliberately does NOT include picks / probabilities / EV / prediction-stats —
 * those are premium-gated (see the `<PremiumGate>` on the game page) and MUST
 * NOT enter the public HTML (else the paywalled picks get indexed for free).
 * Only matchup + teams + date/venue + live/final score, all of which the site
 * already shows to anonymous users.
 */
export interface GameContent {
  ok: boolean;
  /** Canonical slug from /seo/game-meta (preferred for links). */
  slug: string | null;
  sport: string;
  home: { name: string; abbr: string } | null;
  away: { name: string; abbr: string } | null;
  date: string | null;
  status: string | null;
  venue: string | null;
  homeScore: number | null;
  awayScore: number | null;
  homeRecord?: string | null;
  awayRecord?: string | null;
}

const EMPTY_GAME: Omit<GameContent, "sport"> = {
  ok: false,
  slug: null,
  home: null,
  away: null,
  date: null,
  status: null,
  venue: null,
  homeScore: null,
  awayScore: null,
  homeRecord: null,
  awayRecord: null,
};

/**
 * Server-fetch the game detail (matchup + score) for SSR of the game page.
 * Combines two PUBLIC sources:
 *   - /seo/game-meta/{sport}/{id}  -> canonical slug + team names/abbrs
 *   - /{sport}/games/{id}          -> venue, status, live/final scores
 * (NFL game detail is unprefixed `/games/{id}`; NBA/MLB are `/nba|mlb/games/{id}`.)
 * Never throws; degrades to EMPTY_GAME so the page still renders.
 */
export async function gameContent(
  sport: string,
  segment: string
): Promise<GameContent> {
  const sp = sport.toLowerCase();
  const numericId = gameIdFromSegment(segment) ?? segment;
  // Detail source differs by sport:
  //   NFL -> /games/{id}          (full game row: venue/status/scores)
  //   NBA -> /nba/games/{id}/boxscore
  //   MLB -> /mlb/games/{id}/boxscore
  // NOTE: the boxscore payloads ALSO carry premium fields (pick_card,
  // betting_lines, splits, lineups). We extract ONLY the non-premium game facts
  // below and never pass the raw payload through.
  const detailPath =
    sp === "nfl"
      ? `/games/${encodeURIComponent(numericId)}`
      : sp === "nba"
        ? `/nba/games/${encodeURIComponent(numericId)}/boxscore`
        : `/mlb/games/${encodeURIComponent(numericId)}/boxscore`;
  try {
    const [meta, detail] = await Promise.all([
      fetchSeoJson<GameMeta>(`/seo/game-meta/${sport}/${encodeURIComponent(numericId)}`),
      fetchSeoJson<Record<string, unknown>>(detailPath),
    ]);
    const raw = (detail ?? {}) as Record<string, unknown>;
    // The game facts live at the top level for NFL, under `.game` for MLB boxscore.
    const d: Record<string, unknown> =
      raw.game && typeof raw.game === "object"
        ? (raw.game as Record<string, unknown>)
        : raw;
    const num = (v: unknown): number | null =>
      typeof v === "number" ? v : v == null ? null : Number.isFinite(Number(v)) ? Number(v) : null;
    const str = (v: unknown): string | null => (typeof v === "string" ? v : null);

    const homeAbbr = str(d.home_team)?.slice(0, 3).toUpperCase() ?? meta?.home?.abbr ?? null;
    const awayAbbr = str(d.away_team)?.slice(0, 3).toUpperCase() ?? meta?.away?.abbr ?? null;
    const rec = (v: unknown): string | null => {
      if (v && typeof v === "object" && "wins" in (v as object)) {
        const o = v as { wins?: unknown; losses?: unknown };
        return o.wins != null && o.losses != null ? `${o.wins}-${o.losses}` : null;
      }
      return null;
    };

    return {
      ok: Boolean(meta || detail),
      slug: meta?.slug ?? str(d.slug) ?? null,
      sport,
      home:
        meta?.home ??
        (str(d.home_team) ? { name: str(d.home_team) as string, abbr: homeAbbr ?? "" } : null),
      away:
        meta?.away ??
        (str(d.away_team) ? { name: str(d.away_team) as string, abbr: awayAbbr ?? "" } : null),
      date: meta?.date ?? str(d.date),
      status: meta?.status ?? str(d.status),
      venue: str(d.venue),
      homeScore: num(d.home_score),
      awayScore: num(d.away_score),
      homeRecord: rec(raw.home_record),
      awayRecord: rec(raw.away_record),
    };
  } catch (err) {
    console.error(`[seo-content] gameContent failed: ${sport}/${segment}`, err);
    return { ...EMPTY_GAME, sport };
  }
}

/**
 * SSR cache window for server-rendered SEO/game facts (seconds).
 *
 * The game-detail page server-renders non-premium game facts into the initial
 * HTML for crawlers. Previously every request re-fetched the backend
 * (`cache: "no-store"`), which is wasteful for a page that can get hit hard.
 * We now cache the SSR payload for a short window (like the schedule page),
 * small enough that live scores stay fresh — the client component additionally
 * polls (see usePollingRefresh) so what the user SEES updates immediately
 * regardless of this server cache.
 */
export const SSR_GAME_REVALIDATE_SECONDS = 60;

async function fetchSeoJson<T>(path: string): Promise<T | null> {
  try {
    const base = backendBaseForPath(path);
    const res = await fetch(`${base}${path}`, {
      next: { revalidate: SSR_GAME_REVALIDATE_SECONDS },
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

/** One row of a team's schedule, reduced to NON-PREMIUM fields only. */
export interface TeamGameRow {
  date: string | null;
  status: string | null;
  home: boolean;
  opponent: string;
  teamScore: number | null;
  oppScore: number | null;
}

/**
 * SEO-safe (NON-PREMIUM) team content for server rendering.
 *
 * Includes team name and the season schedule — all public.
 * ⚠️ Deliberately EXCLUDES picks / spreads / totals / moneylines / EV (the
 * source endpoint returns them, but they are premium and MUST NOT enter public
 * HTML). Filter in `_scheduleRow` if the upstream shape changes.
 */
export interface TeamContent {
  ok: boolean;
  sport: string;
  abbr: string;
  name: string | null;
  record: string | null;
  games: TeamGameRow[];
}

function _scheduleRow(
  g: Record<string, unknown>,
  abbr: string,
  sport: string
): TeamGameRow | null {
  const homeTeam = String(g.home_team ?? "").toUpperCase();
  const awayTeam = String(g.away_team ?? "").toUpperCase();
  const isHome = homeTeam === abbr;
  const opp = isHome ? awayTeam : homeTeam;
  if (!opp) return null;
  const num = (v: unknown): number | null =>
    typeof v === "number" ? v : v == null ? null : Number.isFinite(Number(v)) ? Number(v) : null;
  return {
    date:
      (typeof g.game_date === "string" && g.game_date) ||
      (typeof g.date === "string" ? g.date.slice(0, 10) : null),
    status: typeof g.status === "string" ? g.status : null,
    home: isHome,
    opponent: opp,
    teamScore: num(isHome ? g.home_score : g.away_score),
    oppScore: num(isHome ? g.away_score : g.home_score),
  };
}

/**
 * Server-fetch a team's name and season schedule for SSR.
 * Sources: /seo/team-meta/{sport}/{abbr} (name) and the public schedule list.
 * Never throws; degrades to empty so the page still renders.
 */
export async function teamContent(sport: string, abbrRaw: string): Promise<TeamContent> {
  const sp = sport.toLowerCase();
  const abbr = abbrRaw.toUpperCase();
  const base: TeamContent = { ok: false, sport, abbr, name: null, record: null, games: [] };
  try {
    const year = new Date().getUTCFullYear();
    // NFL schedule list is UNPREFIXED (/games); NBA/MLB are sport-prefixed.
    const listPath =
      sp === "nfl"
        ? `/games?year=${year}&team_abbr=${encodeURIComponent(abbr)}`
        : `/${sp}/games?year=${year}&team_abbr=${encodeURIComponent(abbr)}`;
    const [meta, list] = await Promise.all([
      fetchSeoJson<{ name?: string | null }>(`/seo/team-meta/${sport}/${encodeURIComponent(abbr)}`),
      fetchSeoJson<unknown>(listPath),
    ]);
    const rows0 = Array.isArray(list)
      ? list
      : ((list as { games?: unknown[] } | null)?.games ?? []);
    // Endpoints differ: NBA/MLB honor ?team_abbr, but the NFL list returns ALL
    // seasons and ignores the filter — so filter+sort defensively for every sport.
    const all = (rows0 as Record<string, unknown>[]).filter((g) => {
      const h = String(g.home_team ?? "").toUpperCase();
      const a = String(g.away_team ?? "").toUpperCase();
      return h === abbr || a === abbr;
    });
    const rows = all.filter((g) => {
      const ds = String(g.game_date ?? g.date ?? "");
      return ds.startsWith(String(year));
    });
    const useRows = rows.length > 0 ? rows : all;
    const games = useRows
      .slice()
      .sort((x, y) =>
        String(x.game_date ?? x.date ?? "").localeCompare(String(y.game_date ?? y.date ?? ""))
      )
      .map((g) => _scheduleRow(g, abbr, sp))
      .filter((g): g is TeamGameRow => g !== null)
      .slice(-16); // most recent 16 for the HTML block
    return { ok: Boolean(meta || useRows.length), sport, abbr, name: meta?.name ?? null, record: null, games };
  } catch (err) {
    console.error(`[seo-content] teamContent failed: ${sport}/${abbr}`, err);
    return base;
  }
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
): Promise<{ title: string; description: string; canonical?: string; image?: string; robots?: { index: boolean; follow: boolean } }> {
  const label = sportLabel(sport);
  const [meta, access] = await Promise.all([
    fetchSeoJson<{
      title?: string | null;
      preview_image?: string | null;
      premium_social_card?: string | null;
    }>(`/seo/writeup-meta/${sport}/${encodeURIComponent(identifier)}`),
    // Accessibility gate: only a free-feature writeup returns 200 to an
    // anonymous request. Everything else (paywalled 403 / missing 404) must be
    // noindex so crawlers don't index empty paywall shells or soft-404s.
    writeupContent(sport, identifier),
  ]);
  const indexable = access.ok && (access.data?.is_free_feature ?? true);
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
      indexable,
    });
  }
  return buildMeta({
    title: `${label} Analysis`,
    description: BASE.description(`${label} game analysis and writeups`),
    url: url(`/${sport}/analysis/${identifier}`),
    image,
    indexable,
  });
}

export interface WriteupContent {
  /** True only when the backend served the FULL article body to an anonymous request. */
  ok: boolean;
  /** HTTP status from the backend (200 free-feature, 403 paywalled, 404 missing). */
  status: number;
  /** Raw writeup payload (markdown content + metadata) when ok. */
  data: {
    title?: string | null;
    content?: string | null;
    body?: string | null;
    matchup?: string | null;
    game_date?: string | null;
    published_at?: string | null;
    game_id?: number | null;
    is_free_feature?: boolean | null;
    sport?: string | null;
  } | null;
}

/**
 * Server-side fetch of a writeup's FULL content, gated exactly like the page.
 *
 * Hits the same public endpoint the client uses (`/writeups/{sport}/{id}?
 * tier=premium`): free-feature writeups return 200 + body, paywalled ones
 * return 403 (no content). This lets the SERVER decide, before render, whether
 * the article body belongs in the initial HTML — so Googlebot's first-wave
 * fetch sees real content for the one class of writeup we actually want indexed
 * (free-feature), and a clean paywall page for the rest.
 *
 * Never throws — any failure degrades to `{ ok: false }` so the page still
 * renders (client component takes over).
 */
export async function writeupContent(
  sport: string,
  identifier: string
): Promise<WriteupContent> {
  try {
    const path = `/writeups/${sport}/${encodeURIComponent(identifier)}?tier=premium`;
    const base = backendBaseForPath(path);
    const res = await fetch(`${base}${path}`, {
      next: { revalidate: 0 },
      cache: "no-store",
    });
    if (!res.ok) return { ok: false, status: res.status, data: null };
    const data = (await res.json()) as WriteupContent["data"];
    return { ok: true, status: res.status, data };
  } catch (err) {
    console.error(`[seo-content] writeupContent failed: ${sport}/${identifier}`, err);
    return { ok: false, status: 0, data: null };
  }
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
