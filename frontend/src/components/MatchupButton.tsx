"use client";

import MatchupModal from "@/components/MatchupModal";

interface MatchupButtonProps {
  sport: "nfl" | "nba" | "mlb";
  gameId?: number;
  homeTeam: string;
  awayTeam: string;
  homeName?: string;
  awayName?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Position the control relative to a card container that already has `relative` set. */
  className?: string;
}

/**
 * Small "VS" button (top-left) that opens the Matchup modal — trends +
 * side-by-side comparison for both teams. Mirrors the style/size of the
 * chat button (top-right) so the two flank the card's top corners.
 *
 * All pointer events are stopped from propagating so opening the modal never
 * triggers navigation on the game card beneath.
 */
export default function MatchupButton({
  sport,
  gameId,
  homeTeam,
  awayTeam,
  homeName,
  awayName,
  open,
  onOpenChange,
  className = "",
}: MatchupButtonProps) {
  const stop = (e: React.SyntheticEvent) => {
    e.preventDefault();
    e.stopPropagation();
  };

  return (
    <>
      <button
        type="button"
        aria-label={`Matchup: ${awayTeam} at ${homeTeam}`}
        title="Compare trends & matchup"
        onClick={(e) => {
          stop(e);
          onOpenChange(true);
        }}
        onMouseDown={stop}
        onMouseUp={stop}
        onPointerDown={stop}
        data-matchup-button
        className={`absolute top-2 left-2 z-20 flex h-7 w-7 items-center justify-center rounded-full border border-indigo-500/40 bg-gradient-to-br from-indigo-600 to-violet-500/80 text-[9px] font-black uppercase tracking-tight text-white shadow-lg shadow-indigo-600/25 transition hover:from-indigo-500 hover:to-violet-400 hover:scale-105 ${className}`}
      >
        VS
      </button>
      <MatchupModal
        open={open}
        onClose={() => onOpenChange(false)}
        sport={sport}
        gameId={gameId}
        homeAbbr={homeTeam}
        awayAbbr={awayTeam}
        homeName={homeName}
        awayName={awayName}
      />
    </>
  );
}
