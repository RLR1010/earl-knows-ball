"use client";

import type { ReactNode } from "react";
import Link from "next/link";

// ── Shared Game Score Card ──────────────────────────────────────────────
// ONE canonical score-card template for ALL sports (nfl / nba / mlb).
// Every game-details page renders this so the three sports look and behave
// identically (badge, live detail, date, away @ home, meta line).
//
// Canonical layout:
//   [ STATUS BADGE ] [ live detail ] [ subtitle ]
//            AWAY  @  HOME
//         (abbr, record, score)
//   [ venue · weather · attendance · duration ]

export type GameScoreTeam = {
  /** Team abbreviation, e.g. "GB", "ATL". Rendered linkable if `href` set. */
  abbr?: string | null;
  /** Already-formatted score, e.g. 24 or "-". */
  score?: number | string | null;
  /** Already-formatted record, e.g. "3-1" or "12-8-1". */
  record?: string | null;
  /** True when this team won (only used when `final`). */
  won?: boolean;
  /** Link target for the abbreviation (e.g. "/nfl/teams/gb"). */
  href?: string | null;
};

export type GameScoreCardProps = {
  /** Status badge text, e.g. "FINAL", "LIVE", "7:20 PM". */
  badgeLabel: string;
  /** Badge color classes, e.g. "text-green-400". */
  badgeClass?: string;
  /** Optional live detail shown next to the badge (quarter/clock, inning arrow). */
  liveDetail?: ReactNode;
  /** Optional subtitle shown next to the badge (date, season type). */
  subtitle?: ReactNode;
  /** Whether the game is complete (drives winner highlight + dimming). */
  final?: boolean;
  away: GameScoreTeam;
  home: GameScoreTeam;
  /** Optional meta line (venue / weather / attendance / duration). */
  meta?: ReactNode;
};

function TeamColumn({
  side,
  team,
  final,
}: {
  side: "away" | "home";
  team: GameScoreTeam;
  final?: boolean;
}) {
  const abbr = (team.abbr ?? "").trim();
  const label = abbr ? abbr.toUpperCase() : side === "away" ? "AWAY" : "HOME";
  const baseColor = side === "away" ? "text-gray-300" : "text-white";
  const dimmed = final && !team.won;
  const abbrClass = `text-2xl font-bold transition-colors ${dimmed ? "opacity-60 text-gray-400" : `opacity-100 ${baseColor}`}`;
  const scoreClass = `text-5xl font-bold mt-1 ${final && team.won ? "text-earl-400" : "text-gray-400"}`;

  const abbrEl = <span className={abbrClass}>{label}</span>;

  return (
    <div className="flex flex-col items-center gap-1">
      {team.href ? (
        <Link href={team.href} className="hover:underline">
          {abbrEl}
        </Link>
      ) : (
        abbrEl
      )}
      {team.record ? <div className="text-xs text-gray-400">{team.record}</div> : null}
      <span className={scoreClass}>{team.score != null && team.score !== "" ? team.score : "-"}</span>
    </div>
  );
}

export function GameScoreCard({
  badgeLabel,
  badgeClass,
  liveDetail,
  subtitle,
  final,
  away,
  home,
  meta,
}: GameScoreCardProps) {
  return (
    <div className="border border-white/10 rounded-xl p-6 bg-gradient-to-r from-white/5 to-white/0 text-center">
      <span className={`text-sm font-bold ${badgeClass || "text-gray-400"}`}>{badgeLabel}</span>
      {liveDetail ? <span className="text-sm font-semibold text-white ml-3">{liveDetail}</span> : null}
      {subtitle ? <span className="text-xs text-gray-500 ml-3">{subtitle}</span> : null}
      <div className="flex items-center justify-center gap-8 md:gap-16 mt-4">
        <TeamColumn side="away" team={away} final={final} />
        <div className="text-4xl text-gray-600 font-black">@</div>
        <TeamColumn side="home" team={home} final={final} />
      </div>
      {meta ? (
        <div className="flex items-center justify-center flex-wrap gap-x-3 gap-y-1 text-sm text-gray-500 mt-4">
          {meta}
        </div>
      ) : null}
    </div>
  );
}

export default GameScoreCard;
