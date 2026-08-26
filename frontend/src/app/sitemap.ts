import type { MetadataRoute } from "next";

import { backendBaseForPath } from "@/lib/backend-url";

const SITE_URL = "https://earlknowsball.com";

/**
 * sitemap.xml — the crawlable URL set.
 *
 * Built from the backend /seo/sitemap-data endpoint (which owns the DB
 * queries across nfl/nba/mlb). Static marketing pages + sport hubs are
 * declared here; dynamic content (team pages, game pages, analysis
 * writeups, original articles) comes from the API so the sitemap stays in
 * sync with what's actually published without a rebuild.
 *
 * NOTE: this route runs at request time on the Next server and calls the
 * internal API. It is intentionally not generated at build (SSG) so it
 * never goes stale.
 */
export const dynamic = "force-dynamic";

async function getSitemapData() {
  const base = backendBaseForPath("/seo/sitemap-data");
  try {
    const res = await fetch(`${base}/seo/sitemap-data`, {
      next: { revalidate: 0 },
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`sitemap-data ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error("[sitemap] failed to fetch sitemap-data:", err);
    return null;
  }
}

export default async function sitemap(): Promise<MetadataRoute.Sitemap> {
  const entries: MetadataRoute.Sitemap = [];

  // ── Static marketing pages ──────────────────────────────────────────
  const staticPages: Array<{ path: string; priority: number; changeFrequency: string }> = [
    { path: "", priority: 1.0, changeFrequency: "daily" },
    { path: "faq", priority: 0.6, changeFrequency: "monthly" },
    { path: "pricing", priority: 0.7, changeFrequency: "monthly" },
    { path: "privacy", priority: 0.3, changeFrequency: "yearly" },
    { path: "terms", priority: 0.3, changeFrequency: "yearly" },
    { path: "support", priority: 0.4, changeFrequency: "monthly" },
  ];
  for (const p of staticPages) {
    entries.push({
      url: `${SITE_URL}/${p.path}`,
      lastModified: new Date(),
      changeFrequency: p.changeFrequency as MetadataRoute.Sitemap[number]["changeFrequency"],
      priority: p.priority,
    });
  }

  const data = await getSitemapData();
  if (!data) {
    // If the API is momentarily down, still emit the static map rather
    // than failing the whole sitemap (crawlers prefer a partial map to none).
    return entries;
  }

  const sports = data.sports || {};
  const sportEntries: MetadataRoute.Sitemap = [];

  for (const [sport, s] of Object.entries<Record<string, unknown>>(sports)) {
    const d = s as {
      static_routes?: string[];
      teams?: string[];
      game_ids?: number[];
      writeup_slugs?: string[];
      article_slugs?: string[];
    };
    const prefix = `/${sport}`;

    // Sport hub routes
    for (const route of d.static_routes ?? []) {
      sportEntries.push({
        url: `${SITE_URL}${prefix}/${route}`.replace(/\/$/, ""),
        changeFrequency: "daily",
        priority: route === "" ? 0.9 : 0.7,
      });
    }

    // Team pages
    for (const abbr of d.teams ?? []) {
      sportEntries.push({
        url: `${SITE_URL}${prefix}/teams/${abbr}`,
        changeFrequency: "daily",
        priority: 0.6,
      });
    }

    // Game pick-card pages (current season)
    for (const gid of d.game_ids ?? []) {
      sportEntries.push({
        url: `${SITE_URL}${prefix}/games/${gid}`,
        changeFrequency: "daily",
        priority: 0.5,
      });
    }

    // Published analysis writeups
    for (const slug of d.writeup_slugs ?? []) {
      sportEntries.push({
        url: `${SITE_URL}${prefix}/analysis/${slug}`,
        changeFrequency: "weekly",
        priority: 0.8,
      });
    }

    // Published original articles
    for (const slug of d.article_slugs ?? []) {
      sportEntries.push({
        url: `${SITE_URL}${prefix}/articles/${slug}`,
        changeFrequency: "weekly",
        priority: 0.8,
      });
    }
  }

  return [...entries, ...sportEntries];
}
