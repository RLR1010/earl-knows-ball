"use client";

import Link from "next/link";
import TeamLogo from "@/components/TeamLogo";
import SchedulePicksFooter from "@/components/SchedulePicksFooter";
import ChatCardLink from "@/components/ChatCardLink";

/** Type of sport this card renders for. Determines logo set + MLB innings/duration extras. */
export type CardSport = "nfl" | "nba" | "mlb";

/** Flexible game shape accepted by the shared card. Covers schedule + upcoming home cards. */
export interface ScheduleGameLike {
  id: number;
  away_team?: string | null;
  home_team?: string | null;
  away_record?: string | null;
  home_record?: string | null;
  away_score?: number | null;
  home_score?: number | null;
  status?: string | null;
  date?: string | null;
  spread?: number | null;
  over_under?: number | null;
  home_moneyline?: number | null;
  away_moneyline?: number | null;
  // Premium picks
  pick_spread?: string | null;
  pick_over_under?: string | null;
  pick_moneyline?: string | null;
  pick_ats_ev?: number | null;
  pick_ou_ev?: number | null;
  pick_ml_ev?: number | null;
  result_spread?: string | null;
  result_over_under?: string | null;
  result_moneyline?: string | null;
  // MLB extras
  actual_innings?: number | null;
  duration_minutes?: number | null;
}

// ── Time / status helpers (kept identical to schedule page) ─────────
function formatTime(iso: string) {
  const d = new Date(iso);
  return (
    d.toLocaleTimeString("en-US", {
      hour: "numeric",
      minute: "2-digit",
      timeZone: "America/New_York",
    }) + " ET"
  );
}

/** Short date, e.g. \"Wed, Aug 13\" (not year — upcoming games are current season). */
function formatDate(iso: string) {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "America/New_York",
  });
}

function statusBadge(status: string): { label: string; cls: string } {
  switch (status.toLowerCase()) {
    case "final":
      return { label: "FINAL", cls: "text-green-400" };
    case "in_progress":
      return { label: "LIVE", cls: "text-red-400 animate-pulse" };
    case "postponed":
      return { label: "PPD", cls: "text-yellow-400" };
    case "cancelled":
      return { label: "CANC", cls: "text-gray-500" };
    default:
      return { label: (status || "SCHEDULED").toUpperCase(), cls: "text-earl-400" };
  }
}

function resolvePickTeam(
  pick: string | null | undefined,
  home: string | null | undefined,
  away: string | null | undefined,
): string | null {
  if (!pick) return null;
  const p = String(pick).trim();
  if (p.toLowerCase() === "home") return home ?? null;
  if (p.toLowerCase() === "away") return away ?? null;
  return p;
}

interface EarlsPickItem {
  label: string;
  pick: string;
  ev?: number | null;
  result?: string | null;
}

/** Build the three premium pick items (Spread / Over-Under / Moneyline) for a game. */
export function buildPickItems(o: {
  spreadPick?: string | null;
  overUnder?: string | null;
  mlPick?: string | null;
  atsEv?: number | null;
  ouEv?: number | null;
  mlEv?: number | null;
  spreadResult?: string | null;
  ouResult?: string | null;
  mlResult?: string | null;
  home?: string | null;
  away?: string | null;
  spreadLabel?: string;
}): EarlsPickItem[] {
  const items: EarlsPickItem[] = [];
  const spreadTeam = resolvePickTeam(o.spreadPick, o.home, o.away);
  if (spreadTeam)
    items.push({
      label: o.spreadLabel ?? "Spread",
      pick: spreadTeam,
      ev: o.atsEv,
      result: o.spreadResult,
    });
  const ou = o.overUnder == null ? null : `${o.overUnder}`;
  if (ou)
    items.push({ label: "Total", pick: ou, ev: o.ouEv, result: o.ouResult });
  const ml = resolvePickTeam(o.mlPick, o.home, o.away);
  if (ml)
    items.push({ label: "Moneyline", pick: ml, ev: o.mlEv, result: o.mlResult });
  return items;
}

export function hasPicks(o: {
  spread?: string | null;
  ou?: string | null;
  ml?: string | null;
}): boolean {
  return Boolean(o.spread || o.ou || o.ml);
}

/**
 * Build a compact, self-contained "game brief" that is injected into every chat
 * message from a card so Earl always knows exactly which game is being discussed
 * (and stays consistent with our lines + picks). Used by the shared card and the
 * schedule page cards.
 */
export function buildGameContext(
  sport: CardSport,
  game: ScheduleGameLike
): string {
  const lines: string[] = [];
  if (game.spread != null) lines.push(`spread ${game.spread > 0 ? "+" : ""}${game.spread}`);
  if (game.over_under != null) lines.push(`total ${game.over_under}`);
  if (game.home_moneyline != null && game.away_moneyline != null)
    lines.push(`${game.away_moneyline} / ${game.home_moneyline} ML`);

  const picks: string[] = [];
  const pickItems = buildPickItems({
    spreadPick: game.pick_spread,
    overUnder: game.pick_over_under,
    mlPick: game.pick_moneyline,
    atsEv: game.pick_ats_ev,
    ouEv: game.pick_ou_ev,
    mlEv: game.pick_ml_ev,
    spreadResult: game.result_spread,
    ouResult: game.result_over_under,
    mlResult: game.result_moneyline,
    home: game.home_team,
    away: game.away_team,
  });
  for (const p of pickItems) {
    const ev = typeof p.ev === "number" ? ` (EV ${p.ev.toFixed(3)})` : "";
    picks.push(`${p.label}: ${p.pick}${ev}`);
  }

  const parts = [`Game being discussed: ${game.away_team ?? "Away"} @ ${game.home_team ?? "Home"} (${sport.toUpperCase()})`];
  if (game.date) parts.push(`Scheduled: ${game.date} (ET)`);
  if (lines.length) parts.push(`Current betting lines: ${lines.join(", ")}.`);
  if (picks.length)
    parts.push(`Earl's picks for THIS game (keep your answers consistent with these): ${picks.join("; ")}.`);
  return `[GAME CONTEXT] ${parts.join(" ")}`;
}

export default function ScheduleGameCard({
  game,
  sport,
  href,
}: {
  game: ScheduleGameLike;
  sport: CardSport;
  href: string;
}) {
  const badge = statusBadge(game.status ?? "");
  const isFinal = (game.status ?? "").toLowerCase() === "final";
  const isLive = (game.status ?? "").toLowerCase() === "in_progress";
  const homeWon = isFinal && (game.home_score ?? 0) > (game.away_score ?? 0);
  const awayWon = isFinal && (game.away_score ?? 0) > (game.home_score ?? 0);
  const isMlb = sport === "mlb";

  return (
    <ChatCardLink
      href={href}
      sport={sport}
      homeTeam={game.home_team ?? ""}
      awayTeam={game.away_team ?? ""}
      date={game.date}
      context={buildGameContext(sport, game)}
      hideChat={isFinal}
      className="border border-white/10 rounded-xl p-3 h-full"
    >
      <div className="flex items-center justify-center gap-4 text-lg">
        {/* Away team: logo inline with abbreviation; record centered UNDER the abbreviation */}
        <div className="grid grid-cols-[20px_auto] gap-1.5 items-start">
          {game.away_team && (
            <TeamLogo abbr={game.away_team} sport={sport} size={20} />
          )}
          <div className="flex flex-col items-center gap-0.5 min-w-[52px]">
            <span className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>
              {game.away_team ?? ""}
            </span>
            {game.away_record ? (
              <span className="text-[10px] text-gray-500 leading-tight" title="Record at game time">
                {game.away_record}
              </span>
            ) : null}
          </div>
        </div>

        <div className="flex items-center gap-2 mx-1">
          {isFinal && <span className="font-bold text-white">{game.away_score}</span>}
          {isLive && game.away_score != null && (
            <span className="font-bold text-red-400">{game.away_score}</span>
          )}
          <span className="text-gray-500 font-medium">@</span>
          {isFinal && <span className="font-bold text-white">{game.home_score}</span>}
          {isLive && game.home_score != null && (
            <span className="font-bold text-red-400">{game.home_score}</span>
          )}
        </div>

        {/* Home team: logo inline with abbreviation; record centered UNDER the abbreviation */}
        <div className="grid grid-cols-[auto_20px] gap-1.5 items-start">
          <div className="flex flex-col items-center gap-0.5 min-w-[52px]">
            <span className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>
              {game.home_team ?? ""}
            </span>
            {game.home_record ? (
              <span className="text-[10px] text-gray-500 leading-tight" title="Record at game time">
                {game.home_record}
              </span>
            ) : null}
          </div>
          {game.home_team && (
            <TeamLogo abbr={game.home_team} sport={sport} size={20} />
          )}
        </div>
      </div>

      {/* Status/time */}
      <div className="mt-1.5">
        <span className={`text-[10px] font-bold uppercase tracking-wider ${badge.cls}`}>
          {badge.label}
        </span>
        {isMlb && isFinal && game.actual_innings != null && game.actual_innings > 9 && (
          <span className="ml-2 text-[10px] text-gray-500">{game.actual_innings} inn</span>
        )}
        {isMlb && isFinal && game.duration_minutes != null && (
          <span className="ml-2 text-[10px] text-gray-600">
            {Math.floor(game.duration_minutes / 60)}:
            {String(game.duration_minutes % 60).padStart(2, "0")}
          </span>
        )}
        {!isFinal && !isLive ? (
          <div className="text-xs text-gray-500 mt-1 flex items-center justify-center gap-1.5">
            {game.date ? (
              <>
                <span className="text-gray-400">{formatDate(game.date)}</span>
                <span className="text-gray-600">•</span>
                <span>{formatTime(game.date)}</span>
              </>
            ) : (
              ""
            )}
          </div>
        ) : (
          <div className="h-4 mt-1" aria-hidden="true" />
        )}
      </div>

      {/* Betting lines + premium picks (shared footer, identical across schedule cards) */}
      <div className="mt-auto">
        <SchedulePicksFooter game={game} />
      </div>
    </ChatCardLink>
  );
}
