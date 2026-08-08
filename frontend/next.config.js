/** @type {import('next').NextConfig} */

// ✅ B1: Two-machine routing.
//   EARL_API_URL     -> api     machine (user-facing routers)
//   EARL_COMPUTE_URL -> compute machine (mlb/nba stats, admin, writeups, original_articles, ingest)
//
// Match the prefix table in src/lib/backend-url.ts (isComputePath). We can't
// import the TS lib from next.config.js reliably, so the small prefix list is
// kept in sync here.
const C = {
  api: process.env.EARL_API_URL || process.env.NEXT_PUBLIC_EARL_API_URL || "http://localhost:8001",
  compute: process.env.EARL_COMPUTE_URL || process.env.NEXT_PUBLIC_EARL_COMPUTE_URL || "http://localhost:8002",
};

const isCompute = (p) =>
  [
    "/api/admin",
    "/api/mlb",
    "/api/nba",
    "/api/writeups",
    "/writeups",
    "/original-articles",
    "/ingest",
  ].some((prefix) => p === prefix || p.startsWith(`${prefix}/`));

// Keep source prefix -> (strip?) mapping. Most /api/x routes need the /api
// stripped to reach backend /x; a few keep it (admin, users).
const RULES = [
  // ---- API machine (user-facing) ----
  { src: "/api/auth/:path*", dst: "/auth/:path*", keepApi: false },         // backend /auth/*
  { src: "/api/articles/:path*", dst: "/api/articles/:path*", keepApi: true },
  { src: "/api/subscriptions/:path*", dst: "/api/subscriptions/:path*", keepApi: true },
  { src: "/api/users/:path*", dst: "/api/users/:path*", keepApi: true },
  { src: "/api/:path*", dst: "/:path*", keepApi: false },                     // catch-all -> api
  { src: "/auth/:path*", dst: "/auth/:path*", keepApi: false },
  { src: "/health", dst: "/health", keepApi: false },

  // ---- Compute machine ----
  { src: "/api/admin/:path*", dst: "/api/admin/:path*", keepApi: true, compute: true },
  { src: "/api/mlb/:path*", dst: "/mlb/:path*", keepApi: false, compute: true },
  { src: "/api/nba/:path*", dst: "/nba/:path*", keepApi: false, compute: true },
  { src: "/writeups/:path*", dst: "/writeups/:path*", keepApi: false, compute: true },
  { src: "/api/writeups/:path*", dst: "/writeups/:path*", keepApi: false, compute: true },
];

// Order matters: specific routes must precede the /api catch-all.
const rewrites = RULES.filter((r) => r.compute !== true).map((r) => ({
  source: r.src,
  destination: `${C.api}${r.dst}`,
}));
const computeRewrites = RULES.filter((r) => r.compute === true).map((r) => ({
  source: r.src,
  destination: `${C.compute}${r.dst}`,
}));
// Compute rules must come first (specific mlb/nba/admin/writeups before catch-all).
const allRewrites = [...computeRewrites, ...rewrites];

const nextConfig = {
  allowedDevOrigins: ["earlknowsball.com", "localhost"],

  async redirects() {
    return [
      { source: "/chat", destination: "/nfl/chat", permanent: false },
      { source: "/chat/:path*", destination: "/nfl/chat/:path*", permanent: false },
    ];
  },

  async rewrites() {
    return allRewrites;
  },

  // Keep-alive for proxy connections
  httpAgentOptions: {
    keepAlive: true,
  },

  // Increase proxy timeout for long-running requests (MLB chat tool calls, and
  // writeup generation which can run 7+ minutes with the 2-pass accuracy loop).
  experimental: {
    proxyTimeout: 1800_000, // 30 minutes
  },
};

module.exports = nextConfig;
