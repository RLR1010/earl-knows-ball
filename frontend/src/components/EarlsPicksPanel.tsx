"use client";

import React, { type ReactNode } from "react";
import PremiumGate from "./PremiumGate";
import { useAuth } from "@/lib/auth-context";

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
  ev?: number | string | null;    // expected value, shown as "EV: +12.5"
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
  /** Compact schedule-card variant: no header icon, no Premium badge, text-only gate. */
  compact?: boolean;
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
          {score.total != null && <>Total: {Number.isInteger(score.total) ? score.total : score.total.toFixed(1)}</>}
          {score.margin != null && (
            <>
              {score.total != null && " · "}
              Margin: {score.margin >= 0 ? "+" : ""}
              {score.margin.toFixed(1)}
            </>
          )}
        </span>
      )}
    </div>
  );
}

function PickItemCard({ item, compact = false }: { item: EarlsPickItem; compact?: boolean }) {
  const pickColor = item.pickColor ?? "text-cyan-400";
  const isFinal = item.result != null;
  // Normalize result casing across sports (NBA ATS uses lowercase 'win'/'loss'/'push').
  const resultNorm =
    typeof item.result === "string"
      ? item.result.charAt(0).toUpperCase() + item.result.slice(1).toLowerCase()
      : item.result;
  // Completed game: color the PICK by result — Win (green) / Loss (red) / Push (grey). Non-final uses the usual pick color.
  const resultColor =
    resultNorm === "Win"
      ? "text-green-400"
      : resultNorm === "Loss"
      ? "text-red-400"
      : "text-gray-400"; // Push
  const pickDisplayColor = isFinal ? resultColor : pickColor;

  return (
    <div className={`text-center flex flex-col ${compact ? "px-1 py-1.5" : "py-3 md:px-3"}`}>
      <div className="text-[10px] uppercase tracking-wider text-gray-500">{item.label}</div>

      <div className={`${compact ? "text-xs " : "text-lg "}font-bold mt-0.5 ${pickDisplayColor} leading-snug break-words`}>
        {item.pick}
      </div>

      {item.subpick && (
        <div className="text-sm font-semibold text-gray-200 mt-0.5">{item.subpick}</div>
      )}

      {item.ev != null && (
        <span
          className={`text-xs font-semibold ${compact ? "" : "mt-1 "}${
            typeof item.ev === "number" && item.ev >= 0 ? "text-green-400" : "text-red-400"
          }`}
        >
          EV: {typeof item.ev === "number" ? (item.ev >= 0 ? "+" : "") + item.ev.toFixed(1) : item.ev}
        </span>
      )}
    </div>
  );
}

/** Text-only premium gate for compact schedule cards. No card, no button — just the message. */
function CompactGate({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex items-center justify-center py-4">
        <div className="w-5 h-5 border-2 border-gray-500 border-t-white rounded-full animate-spin" />
      </div>
    );
  }

  const isPremium =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";

  if (!user || !isPremium) {
    return (
      <p className="py-3 text-xs text-gray-400 text-center">
        {user
          ? "Upgrade to Premium to access Earl's Picks."
          : "Sign in and upgrade to Premium to access Earl's Picks."}
      </p>
    );
  }

  return <>{children}</>;
}

export default function EarlsPicksPanel({
  items,
  predicted,
  title = "Earl's Picks",
  compact = false,
}: EarlsPicksPanelProps) {
  return (
    <div className={`${compact ? "mt-2" : "mt-4 pt-4 border-t border-white/10 space-y-4"} ${compact ? "space-y-2" : ""}`}>
      {compact ? (
        <h3 className="text-sm font-semibold tracking-tight text-gray-100">{title}</h3>
      ) : (
        <div className="flex items-center gap-2">
          <span className="w-1 h-4 rounded-full bg-gradient-to-b from-earl-400 to-amber-500" />
          <h3 className="text-sm font-semibold tracking-tight text-gray-100 uppercase inline-flex items-center gap-1.5">
            {title}
          </h3>
          <span className="text-[10px] font-medium text-amber-400/90 bg-amber-500/10 border border-amber-500/30 rounded-full px-2 py-0.5">
            Premium
          </span>
        </div>
      )}

      {compact ? (
        <CompactGate>
          {predicted && <ScoreLine heading="Predicted" score={predicted} />}
          <div className="grid grid-cols-3 divide-x divide-white/10">
            {items.map((item) => (
              <PickItemCard key={item.label} item={item} compact />
            ))}
          </div>
        </CompactGate>
      ) : (
        <PremiumGate title={title}>
          {predicted && <ScoreLine heading="Predicted" score={predicted} />}
          <div className="grid grid-cols-3 divide-x divide-white/10">
            {items.map((item) => (
              <PickItemCard key={item.label} item={item} compact={false} />
            ))}
          </div>
        </PremiumGate>
      )}
    </div>
  );
}
