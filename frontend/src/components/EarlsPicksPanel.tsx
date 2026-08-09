"use client";

import React from "react";
import PremiumGate from "./PremiumGate";

/**
 * Professional inline panel that shows Earl's Picks inside a Betting Lines card.
 *
 * Self-gated: renders the actual picks only for premium members via <PremiumGate>.
 * Designed to plug cleanly into any sport's betting-lines card and to look good
 * on both large displays (3-col grid) and mobile (stacked).
 *
 * Parents map their sport-specific pick_card into this generic shape.
 */

export type EarlsPickItem = {
  label: string;            // e.g. "Run Line", "Over/Under", "Moneyline"
  pick: string;             // the pick value / team shown in the accent color
  subpick?: string | null;  // optional secondary line (e.g. "Over 8.5")
  ev?: number | string | null;    // expected value, shown as "EV: +12.5¢"
  line?: string | null;     // optional reference line text
  result?: string | null;   // "Win"/"Loss" once the game is final
  pickColor?: string;       // accent class for the pick (default cyan)
};

type PredictedScore = {
  awayLabel: string;
  awayScore: number;
  homeScore: number;
  homeLabel: string;
  total?: number;
  margin?: number;
};

type EarlsPicksPanelProps = {
  items: EarlsPickItem[];
  predicted?: PredictedScore | null;
  title?: string; // default "Earl's Picks"
};

function ScoreLine({
  heading,
  score,
}: {
  heading: string;
  score: PredictedScore;
}) {
  return (
    <div className="text-center flex flex-col sm:flex-row sm:items-center sm:justify-center gap-x-3 gap-y-0.5">
      <span className="text-xs uppercase tracking-wider text-gray-500">{heading}</span>
      <div className="text-lg font-bold tracking-tight flex items-center justify-center gap-1.5">
        <span className="text-gray-300">{score.awayLabel}</span>
        <span className="text-white"> {score.awayScore}</span>
        <span className="text-gray-600">@</span>
        <span className="text-white">{score.homeScore} </span>
        <span className="text-gray-300">{score.homeLabel}</span>
      </div>
      {(score.total != null || score.margin != null) && (
        <span className="text-xs text-gray-500">
          {score.total != null && <>Total: {score.total}</>}
          {score.margin != null && (
            <>
              {score.total != null && " · "}
              Margin: {score.margin >= 0 ? "+" : ""}
              {score.margin}
            </>
          )}
        </span>
      )}
    </div>
  );
}

function PickItemCard({ item }: { item: EarlsPickItem }) {
  const pickColor = item.pickColor ?? "text-cyan-400";
  const hasLine = item.line != null || item.subpick != null;
  const isFinal = item.result != null;
  // Normalize result casing across sports (NBA ATS uses lowercase 'win'/'loss'/'push').
  const resultNorm =
    typeof item.result === "string"
      ? item.result.charAt(0).toUpperCase() + item.result.slice(1).toLowerCase()
      : item.result;
  // Completed game: Win (green) / Loss (red) / Push (grey)
  const resultColor =
    resultNorm === "Win"
      ? "text-green-400"
      : resultNorm === "Loss"
      ? "text-red-400"
      : "text-gray-400"; // Push

  return (
    <div className="text-center py-3 md:px-3 flex flex-col">
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{item.label}</div>

      {isFinal ? (
        <div className={`text-lg font-bold mt-2 ${resultColor}`}>{resultNorm}</div>
      ) : (
        <div className={`text-lg font-bold mt-2 ${pickColor} leading-snug break-words`}>
          {item.pick}
        </div>
      )}

      {/* For completed games only the result + EV show; the pick line is redundant. */}
      {!isFinal && item.subpick && (
        <div className="text-sm font-semibold text-gray-200 mt-0.5">{item.subpick}</div>
      )}

      {item.ev != null && (
        <span
          className={`text-xs font-semibold mt-1 ${
            typeof item.ev === "number" && item.ev >= 0 ? "text-green-400" : "text-red-400"
          }`}
        >
          EV: {typeof item.ev === "number" ? (item.ev >= 0 ? "+" : "") + item.ev.toFixed(1) + "¢" : item.ev}
        </span>
      )}

      {!isFinal && hasLine && (
        <div className="text-[11px] text-gray-500 mt-2 leading-snug">{item.line ?? item.subpick}</div>
      )}
    </div>
  );
}

export default function EarlsPicksPanel({
  items,
  predicted,
  title = "Earl's Picks",
}: EarlsPicksPanelProps) {
  return (
    <div className="mt-4 pt-4 border-t border-white/10 space-y-4">
      <div className="flex items-center gap-2">
        <span className="w-1 h-4 rounded-full bg-gradient-to-b from-earl-400 to-amber-500" />
        <h3 className="text-sm font-semibold tracking-tight text-gray-100 uppercase inline-flex items-center gap-1.5">
          {title}
        </h3>
        <span className="text-[10px] font-medium text-amber-400/90 bg-amber-500/10 border border-amber-500/30 rounded-full px-2 py-0.5">
          Premium
        </span>
      </div>

      <PremiumGate title={title}>
        {predicted && <ScoreLine heading="Predicted" score={predicted} />}

        <div className="grid grid-cols-1 md:grid-cols-3 divide-y md:divide-y-0 md:divide-x divide-white/10">
          {items.map((item) => (
            <PickItemCard key={item.label} item={item} />
          ))}
        </div>
      </PremiumGate>
    </div>
  );
}
