// backend-url.ts — Single source of truth for routing frontend requests to the
// correct backend machine.
//
// Production runs the Earl backend split across TWO machines:
//   * api     — user-facing routers  (chat, games, players, props, stats, auth,
//              articles, subscriptions, teams, token_usage, v1)
//   * compute — mlb_stats, nba_stats, admin, writeups, original_articles, ingest
//              + the task scheduler
//
// The base URL for each machine is supplied as build/runtime env:
//   EARL_API_URL       (default: http://localhost:8001)
//   EARL_COMPUTE_URL   (default: http://localhost:8002)
//
// This module exposes helpers that decide which host a given backend path/prefix
// belongs to, so next.config.js rewrites, middleware, and page components all
// agree on routing without duplicating the prefix table.

const API_DEFAULT = "http://localhost:8001";
const COMPUTE_DEFAULT = "http://localhost:8002";

export const API_URL =
  process.env.EARL_API_URL || process.env.NEXT_PUBLIC_EARL_API_URL || API_DEFAULT;
export const COMPUTE_URL =
  process.env.EARL_COMPUTE_URL || process.env.NEXT_PUBLIC_EARL_COMPUTE_URL || COMPUTE_DEFAULT;

// Backend path prefixes served by the compute machine. Anything else goes to api.
const COMPUTE_PREFIXES: string[] = [
  "/api/admin",
  "/api/mlb",
  "/api/nba",
  "/api/writeups",
  "/writeups",
  "/original-articles",
  "/ingest",
  "/health", // health is served by either; route to api by default
];

/** True if a backend path (or leading path segment) is served by compute. */
export function isComputePath(path: string): boolean {
  const p = path.startsWith("/") ? path : `/${path}`;
  return COMPUTE_PREFIXES.some((prefix) => p === prefix || p.startsWith(`${prefix}/`));
}

/** Base URL (scheme://host[:port]) for the machine serving the given backend path. */
export function backendBaseForPath(path: string): string {
  return isComputePath(path) ? COMPUTE_URL : API_URL;
}

/** Build a full backend URL for a path/query. */
export function backendUrl(path: string): string {
  const base = backendBaseForPath(path);
  const q = path.includes("?") ? "" : "";
  return `${base}${path}`;
}
