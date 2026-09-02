import { NextResponse } from "next/server";

/**
 * Instance-local liveness probe used by Caddy's blue/green health checks.
 *
 * IMPORTANT: This must NOT depend on any backend / API / database. It exists so
 * Caddy can tell whether THIS Next.js instance built and booted successfully and
 * is ready to serve, independent of whether the API or compute machines are up.
 *
 * Each blue/green instance identifies itself via the EARL_INSTANCE env var
 * (set in its systemd unit) so you can confirm which instance answered.
 */
export async function GET() {
  return NextResponse.json({
    status: "ok",
    role: "frontend",
    instance: process.env.EARL_INSTANCE ?? "unknown",
    time: new Date().toISOString(),
  });
}
