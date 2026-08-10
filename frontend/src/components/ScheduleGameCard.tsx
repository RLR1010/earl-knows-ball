"use client";

import Link from "next/link";
import Image from "next/image";
import { formatOverUnder } from "@/lib/api";
import { getTeamLogoUrl } from "@/lib/team_logos";
import EarlsPicksPanel from "@/components/EarlsPicksPanel";

/** Type of sport this card renders for. Determines logo set + MLB innings/duration extras. */
export type CardSport = "nfl" | "nba" | "mlb";

/** Flexible game shape accepted by the shared card. Covers schedule + upcoming home cards. */
export interface ScheduleGameLike {
  id: number;
  away_team?: string | null;
  home_team?: string | null;
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

function favoredSpread(
  spread: number | null | undefined,
  home?: string | null | undefined,
  away?: string | null | undefined,
): string {
  if (spread == null) return "Pick'em";
  if (Math.abs(spread) < 0.05) return "Pick'em";
  const line = spread > 0 ? `-${spread}` : `+${Math.abs(spread)}`;
  const team = spread < 0 ? (home ?? "?") : (away ?? "?");
  return `${team} ${line}`;
}

function formatMoneyline(ml: number | null | undefined): string {
  if (ml == null) return "-";
  return ml > 0 ? `+${ml}` : `${ml}`;
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
function buildPickItems(o: {
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

function hasPicks(o: {
  spread?: string | null;
  ou?: string | null;
  ml?: string | null;
}): boolean {
  return Boolean(o.spread || o.ou || o.ml);
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

  const awayLogo = game.away_team ? getTeamLogoUrl(game.away_team, sport) : null;
  const homeLogo = game.home_team ? getTeamLogoUrl(game.home_team, sport) : null;

  return (
    <Link
      href={href}
      className="block border border-white/10 rounded-xl p-3 bg-white/5 hover:bg-white/10 transition text-center"
    >
      <div className="flex items-center justify-center gap-1.5 text-lg">
        {awayLogo && (
          <Image
            src={awayLogo}
            alt={game.away_team ?? ""}
            width={20}
            height={20}
            className="object-contain shrink-0"
            unoptimized
          />
        )}
        <div className={`font-semibold ${awayWon ? "text-earl-400" : "text-gray-300"}`}>
          {game.away_team ?? ""}
        </div>

        {isFinal && <span className="font-bold text-white">{game.away_score}</span>}
        {isLive && game.away_score != null && (
          <span className="font-bold text-red-400">{game.away_score}</span>
        )}

        <span className="text-gray-500 font-medium">@</span>

        {isFinal && <span className="font-bold text-white">{game.home_score}</span>}
        {isLive && game.home_score != null && (
          <span className="font-bold text-red-400">{game.home_score}</span>
        )}

        <div className={`font-semibold ${homeWon ? "text-earl-400" : "text-gray-300"}`}>
          {game.home_team ?? ""}
        </div>
        {homeLogo && (
          <Image
            src={homeLogo}
            alt={game.home_team ?? ""}
            width={20}
            height={20}
            className="object-contain shrink-0"
            unoptimized
          />
        )}
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
          <div className="text-xs text-gray-500 mt-1">
            {game.date ? formatTime(game.date) : ""}
          </div>
        ) : (
          <div className="h-4 mt-1" aria-hidden="true" />
        )}
      </div>

      {/* Betting lines: spread (favored/Pick'em), moneyline, over/under */}
      {(game.spread != null || game.over_under != null) && (
        <div className="mt-3 pt-3 pb-1 border-t border-white/10 text-xs text-center">
          <div className="text-gray-400">
            <span className="text-earl-300">
              {favoredSpread(game.spread, game.home_team, game.away_team)}
            </span>
            <span className="mx-2 text-gray-700">|</span>
            <span>
              {formatMoneyline(game.home_moneyline)}/{formatMoneyline(game.away_moneyline)}
            </span>
            {game.over_under != null && (
              <>
                <span className="mx-2 text-gray-700">|</span>
                <span className="text-gray-400">{formatOverUnder(game.over_under)}</span>
              </>
            )}
          </div>
        </div>
      )}

      {/* Premium picks (self-gated) */}
      {hasPicks({
        spread: game.pick_spread,
        ou: game.pick_over_under,
        ml: game.pick_moneyline,
      }) && (
        <div className="mt-2">
          <EarlsPicksPanel
            compact
            items={buildPickItems({
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
              spreadLabel: "Spread",
            })}
          />
        </div>
      )}
    </Link>
  );
}
