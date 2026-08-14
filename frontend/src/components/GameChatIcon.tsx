"use client";

import { Sparkles } from "lucide-react";
import GameChatModal from "@/components/GameChatModal";

interface GameChatIconProps {
  sport: "nfl" | "nba" | "mlb";
  homeTeam: string;
  awayTeam: string;
  date?: string | null;
  context?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Position the control relative to a card container that already has `relative` set. */
  className?: string;
}

/**
 * Small stars AI icon (top-right) that opens the Earl chat modal for a game.
 *
 * The `open` state is lifted up to the parent card so the card can disable its
 * own interactivity (pointer-events + hover) while the modal is open — that way
 * nothing under the overlay, including the card, can capture clicks/hovers, and
 * highlighting the modal's text box can never trigger card navigation.
 *
 * All pointer events (click, mousedown, mouseup) on the button are stopped from
 * propagating to the card beneath.
 */
export default function GameChatIcon({
  sport,
  homeTeam,
  awayTeam,
  date,
  context,
  open,
  onOpenChange,
  className = "",
}: GameChatIconProps) {
  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <>
      <button
        type="button"
        aria-label={`Chat with Earl about ${awayTeam} at ${homeTeam}`}
        title="Chat with Earl about this game"
        onClick={(e) => {
          stop(e);
          onOpenChange(true);
        }}
        onMouseDown={stop}
        onMouseUp={stop}
        onPointerDown={stop}
        data-game-chat-icon
        className={`absolute top-2 right-2 z-20 flex h-7 w-7 items-center justify-center rounded-full bg-gradient-to-br from-earl-600 to-earl-500/80 border border-earl-500/40 text-white shadow-lg shadow-earl-600/25 hover:from-earl-500 hover:to-earl-400 hover:scale-105 transition ${className}`}
      >
        <Sparkles className="h-3.5 w-3.5" />
      </button>
      <GameChatModal
        open={open}
        onClose={() => onOpenChange(false)}
        sport={sport}
        homeTeam={homeTeam}
        awayTeam={awayTeam}
        date={date ?? undefined}
        context={context}
      />
    </>
  );
}
