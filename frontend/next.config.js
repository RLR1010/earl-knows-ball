/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      // Auth routes (frontend calls /api/auth/send-code, backend serves /auth/send-code)
      { source: "/api/auth/:path*", destination: "http://localhost:8001/auth/:path*" },
      // API routes with /api prefix (admin, articles, subscriptions)
      { source: "/api/admin/:path*", destination: "http://localhost:8001/api/admin/:path*" },
      { source: "/api/articles/:path*", destination: "http://localhost:8001/api/articles/:path*" },
      { source: "/api/subscriptions/:path*", destination: "http://localhost:8001/api/subscriptions/:path*" },
      // MLB stats/roster routes need /api prefix preserved (they're at /api/mlb/* on the backend)
      { source: "/api/mlb/:path*", destination: "http://localhost:8001/api/mlb/:path*" },
      // Writeup routes — bare and /api prefixed, must be before catch-all
      { source: "/writeups/:path*", destination: "http://localhost:8001/writeups/:path*" },
      { source: "/api/writeups/:path*", destination: "http://localhost:8001/writeups/:path*" },
      // All other /api calls get the prefix stripped (teams, players, games, chat, etc.)
      { source: "/api/:path*", destination: "http://localhost:8001/:path*" },
      { source: "/auth/:path*", destination: "http://localhost:8001/auth/:path*" },
      { source: "/health", destination: "http://localhost:8001/health" },
    ];
  },
  // Keep-alive for proxy connections
  httpAgentOptions: {
    keepAlive: true,
  },
  // Increase proxy timeout for long-running requests (e.g., MLB chat tool calls)
  experimental: {
    proxyTimeout: 180_000, // 3 minutes
  },
};

module.exports = nextConfig;
