/** @type {import('next').NextConfig} */
const nextConfig = {
  async rewrites() {
    return [
      // Auth routes (frontend calls /auth/login, backend serves /login)
      { source: "/api/auth/:path*", destination: "http://host.docker.internal:8001/:path*" },
      // API routes with /api prefix (admin, articles, subscriptions)
      { source: "/api/admin/:path*", destination: "http://host.docker.internal:8001/api/admin/:path*" },
      { source: "/api/articles/:path*", destination: "http://host.docker.internal:8001/api/articles/:path*" },
      { source: "/api/subscriptions/:path*", destination: "http://host.docker.internal:8001/api/subscriptions/:path*" },
      // MLB stats/roster routes need /api prefix preserved (they're at /api/mlb/* on the backend)
      { source: "/api/mlb/:path*", destination: "http://host.docker.internal:8001/api/mlb/:path*" },
      // All other /api calls get the prefix stripped (teams, players, games, chat, etc.)
      { source: "/api/:path*", destination: "http://host.docker.internal:8001/:path*" },
      { source: "/auth/:path*", destination: "http://host.docker.internal:8001/:path*" },
      { source: "/health", destination: "http://host.docker.internal:8001/health" },
    ];
  },
};

module.exports = nextConfig;
