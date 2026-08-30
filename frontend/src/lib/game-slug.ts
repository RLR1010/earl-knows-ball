/**
 * Slug helpers for game-page SEO URLs.
 *
 * Canonical game URL shape (single path segment, trailing numeric id is the
 * authority):
 *   /{sport}/games/{home-full}-vs-{away-full}-{YYYY-MM-DD}-{gameId}
 *   e.g. /mlb/games/chicago-cubs-vs-st-louis-cardinals-2026-08-26-49070
 *
 * The numeric game id is always the LAST dash-delimited token. A legacy URL
 * that is purely numeric (/games/49070) is also accepted and redirects to the
 * canonical slug form server-side.
 */

/** Extract the trailing numeric game id from a slug segment (or numeric id). */
export function gameIdFromSegment(segment: string): string | null {
  if (!segment) return null;
  const trimmed = segment.replace(/\/+$/, "");
  const lastDash = trimmed.lastIndexOf("-");
  const tail = lastDash === -1 ? trimmed : trimmed.slice(lastDash + 1);
  if (/^\d+$/.test(tail)) return tail;
  return null;
}

/** True when the segment is game-id only (legacy form) or empty. */
export function isNumericOnly(segment: string): boolean {
  return /^\d+$/.test(segment.replace(/\/+$/, ""));
}
