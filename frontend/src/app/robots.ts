import type { MetadataRoute } from "next";

const SITE_URL = "https://earlknowsball.com";

/**
 * robots.txt — signals what search engines may crawl and index.
 *
 * Content that draws organic search traffic (game pick cards, analysis
 * writeups, team pages, original articles, sport hubs) is left crawlable.
 * We block private / account / payment / admin / interactive surfaces that
 * have no standalone SEO value.
 */
export default function robots(): MetadataRoute.Robots {
  return {
    rules: {
      userAgent: "*",
      allow: "/",
      disallow: [
        // Account & payment surfaces (private / no SEO value)
        "/login",
        "/register",
        "/profile",
        "/checkout",
        "/checkout/*",
        // Admin tooling (never index)
        "/admin",
        "/admin/*",
        // Interactive / chat surfaces (thin, JS-only, no SEO value)
        "/chat",
        "/chat/*",
        "/*/chat",
        "/*/chat/*",
        // Player stat pages are client-rendered and thin; keep them out of
        // the index to avoid near-duplicate low-value URLs.
        "/players/*",
        "/*/players/*",
        // Backend/proxy namespace.
        "/api/*",
      ],
    },
    sitemap: `${SITE_URL}/sitemap.xml`,
  };
}
