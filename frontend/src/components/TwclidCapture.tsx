"use client";

/**
 * X (Twitter) click-ID capture.
 *
 * When a visitor lands from an X ad, the destination URL carries a `twclid`
 * query param. We persist the most recent one in localStorage so it can be
 * attached to later conversion events (login / checkout) server-side, which
 * dramatically improves X's ad attribution.
 *
 * Last-touch with a TTL: a newer twclid overwrites an older one. We keep it
 * for up to 30 days (X's default click attribution window).
 *
 * Mounted once in the root layout so it fires on every route.
 */

const TWCLID_KEY = "earl_twclid";
const TWCLID_TS_KEY = "earl_twclid_ts";
const TTL_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

/** Read the stored twclid (client-only), honoring the TTL. Returns null if absent/expired. */
export function getTwclid(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const val = localStorage.getItem(TWCLID_KEY);
    const ts = Number(localStorage.getItem(TWCLID_TS_KEY) || "0");
    if (!val) return null;
    if (ts && Date.now() - ts > TTL_MS) {
      localStorage.removeItem(TWCLID_KEY);
      localStorage.removeItem(TWCLID_TS_KEY);
      return null;
    }
    return val;
  } catch {
    return null;
  }
}

function captureFromLocation() {
  if (typeof window === "undefined") return;
  try {
    const twclid = new URLSearchParams(window.location.search).get("twclid");
    if (twclid && twclid.trim()) {
      localStorage.setItem(TWCLID_KEY, twclid.trim());
      localStorage.setItem(TWCLID_TS_KEY, String(Date.now()));
    }
  } catch {
    // storage unavailable / malformed URL — best-effort only
  }
}

export default function TwclidCapture() {
  if (typeof window !== "undefined") {
    // Runs during render and after every client-side navigation.
    captureFromLocation();
  }
  return null;
}

/**
 * X (Twitter) conversion events. Event IDs are defined in the X Events Manager
 * and MUST match the IDs used by the server-side Conversions API
 * (backend/app/social/x_conversions.py) so browser + server events dedup.
 */
export const X_EVENTS = {
  LOGIN: "tw-rf02z-rf69g",
  PURCHASE: "tw-rf02z-rf6c9",
} as const;

/**
 * Fire an X conversion event from the browser pixel.
 *
 * IMPORTANT: pass the RAW email address (not a hash) — X's JS hashes it
 * client-side. The server-side API, by contrast, sends a SHA-256 hash of the
 * same email; X matches the two because the inputs hash to the same value.
 *
 * Safe to call anywhere: no-ops if the pixel hasn't loaded (e.g. ad blocker).
 */
export function fireXEvent(
  eventId: string,
  params: { email_address?: string | null } = {},
): void {
  if (typeof window === "undefined") return;
  const twq = (window as unknown as { twq?: (...args: unknown[]) => void }).twq;
  if (typeof twq !== "function") return;
  try {
    twq("event", eventId, {
      email_address: params.email_address ?? null,
    });
  } catch {
    // best-effort only — never break the login flow
  }
}
