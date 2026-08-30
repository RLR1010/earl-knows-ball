"use client";

import { formatOverUnder } from "@/lib/api";
import EarlsPicksPanel from "@/components/EarlsPicksPanel";
import { buildPickItems, hasPicks } from "@/components/ScheduleGameCard";

// Current/Earl-favored spread (mirrors the regular schedule game card).
// `spread` is the SIGNED spread for the HOME team: negative = home favored (giving
// runs, e.g. MIL -1.5), positive = away favored (receiving runs). We display the
// FAVORED team with the negative run line.
function favoredSpread(spread: number | null | undefined, home?: string | null | undefined, away?: string | null | undefined): string {
  if (spread == null) return "Pick'em";
  if (Math.abs(spread) < 0.05) return "Pick'em";
  if (spread < 0) return `${home ?? "?"} ${spread}`;          // home favored: MIL -1.5
  return `${away ?? "?"} ${-Math.abs(spread)}`;               // away favored: TB -1.5
}

// Moneyline: prefer the provided value; fall back to "-" placeholder.
function formatMoneyline(ml: number | null | undefined): string {
  if (ml == null) return "-";
  return ml > 0 ? `+${ml}` : `${ml}`;
}

interface Props {
  game: {
    sport?: string;
    sport_key?: string;
    spread?: number | null;
    home_moneyline?: number | null;
    away_moneyline?: number | null;
    over_under?: number | null;
    home_team?: string | null;
    away_team?: string | null;
    pick_spread?: string | null;
    pick_over_under?: string | null;
    pick_moneyline?: string | null;
    pick_ats_ev?: number | null;
    pick_ou_ev?: number | null;
    pick_ml_ev?: number | null;
    result_spread?: string | null;
    result_over_under?: string | null;
    result_moneyline?: string | null;
  };
  spreadLabel?: string;
}

/**
 * The "odds + picks" footer shared between the regular schedule game card and the
 * sport team pages, so every schedule-style card renders identically.
 */
export default function SchedulePicksFooter({ game, spreadLabel = "Spread" }: Props) {
  return (
    <>
      {/* Betting lines: spread (favored/Pick'em), moneyline, over/under */}
      {(game.spread != null || game.over_under != null) && (
        <div className="mt-3 pt-3 pb-1 border-t border-white/10 text-xs text-center">
          <div className="text-gray-400">
            <span className="text-earl-300">
              {favoredSpread(game.spread, game.home_team, game.away_team)}
            </span>
            <span className="mx-2 text-gray-700">|</span>
            <span>
              {formatMoneyline(game.away_moneyline)}/{formatMoneyline(game.home_moneyline)}
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
        <div className="mt-2" data-testid="schedule-picks">
          <EarlsPicksPanel
            compact
            items={buildPickItems({
              spreadPick: game.pick_spread,
              spread: game.spread,
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
              spreadLabel,
            })}
          />
        </div>
      )}
    </>
  );
}
