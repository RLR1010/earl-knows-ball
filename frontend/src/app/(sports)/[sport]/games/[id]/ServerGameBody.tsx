import Link from "next/link";
import type { GameContent } from "@/lib/seo-content";

const SPORT_NAMES: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

function fmtDate(value?: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    weekday: "long",
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

/**
 * SERVER-RENDERED game header (SEO only).
 *
 * Renders the NON-PREMIUM game facts — matchup, teams, date, venue, and the
 * live/final score — directly into the initial HTML so crawlers see real,
 * per-game content on the first wave.
 *
 * ⚠️ Deliberately EXCLUDES picks, probabilities, EV, and prediction stats:
 * those are premium-gated (`<PremiumGate>` in the client) and must never enter
 * public HTML. Do NOT add them here.
 *
 * ⚠️ Deliberately does NOT render a visual score CARD: the client GameClient
 * renders its own (richer, live-updating) score card below. Emitting a second
 * card here produced TWO score cards on every game page. This block now
 * contributes only the crawlable heading + facts line (with the score woven
 * into the prose), so users see exactly one card (the client's) while the
 * matchup/score text stays in the HTML.
 */
export default function ServerGameBody({ content }: { content: GameContent }) {
  const { sport, home, away, date, status, venue, homeScore, awayScore } = content;
  const label = SPORT_NAMES[sport.toLowerCase()] ?? sport.toUpperCase();

  if (!home || !away) return null;

  const hasScore = homeScore != null && awayScore != null;
  const s = (status || "").toLowerCase();
  const isFinal = s.includes("final") || s.includes("completed");
  const isLive = s.includes("in progress") || s.includes("live") || s.includes("quarter");
  const heading = `${away.name} vs ${home.name}`;

  return (
    <section className="max-w-4xl mx-auto px-4 pt-10" aria-label="Game summary">
      <div className="text-sm text-gray-500 mb-4">
        <Link href={`/${sport}`} className="hover:text-earl-400 transition">
          {label}
        </Link>
        <span className="mx-2 text-gray-600">·</span>
        <Link href={`/${sport}/schedule`} className="hover:text-earl-400 transition">
          Schedule
        </Link>
      </div>

      <h1 className="text-3xl md:text-4xl font-bold tracking-tight">
        {heading}
      </h1>
      <p className="text-sm text-gray-500 mt-2">
        {fmtDate(date)}
        {venue ? (
          <>
            <span className="mx-2 text-gray-600">·</span>
            {venue}
          </>
        ) : null}
        {status ? (
          <>
            <span className="mx-2 text-gray-600">·</span>
            {isFinal ? "Final" : isLive ? "In progress" : status}
          </>
        ) : null}
      </p>

      <p className="mt-4 text-gray-300">
        {heading}
        {hasScore && isFinal ? ` final score: ${away.abbr} ${awayScore}, ${home.abbr} ${homeScore}.` : "."}{" "}
        {label} matchup{venue ? ` at ${venue}` : ""}
        {fmtDate(date) ? ` on ${fmtDate(date)}` : ""}.
      </p>
    </section>
  );
}
