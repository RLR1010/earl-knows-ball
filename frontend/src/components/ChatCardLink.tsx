"use client";

import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import GameChatIcon from "@/components/GameChatIcon";
import MatchupButton from "@/components/MatchupButton";

interface ChatCardLinkProps {
  href: string;
  sport: "nfl" | "nba" | "mlb";
  homeTeam: string;
  awayTeam: string;
  gameId?: number;
  homeName?: string;
  awayName?: string;
  date?: string | null;
  /** Pre-built game context (matchup, lines, picks) passed to the chat modal. */
  context?: string;
  /** When true, don't render the Earl chat icon (e.g. on Final games). */
  hideChat?: boolean;
  /** When true, don't render the VS matchup button (e.g. on Final games). */
  hideMatchup?: boolean;
  className?: string;
  children?: ReactNode;
}

/**
 * Clickable game-card container. Serves as the card's own "link" to the game
 * detail page, but with the chat icon (top-right) and matchup/VS button
 * (top-left) integrated so that:
 *  - hovering / clicking either control never lights up the card or navigates,
 *  - while a modal is open the whole card is made pointer-events:none (no
 *    hover, no click capture), so interacting with the modal — including
 *    highlighting text or scrolling — can never navigate to the game page.
 */
export default function ChatCardLink({
  href,
  sport,
  homeTeam,
  awayTeam,
  gameId,
  homeName,
  awayName,
  date,
  context,
  hideChat = false,
  hideMatchup = false,
  className = "",
  children,
}: ChatCardLinkProps) {
  const router = useRouter();
  const [chatOpen, setChatOpen] = useState(false);
  const [matchupOpen, setMatchupOpen] = useState(false);
  const anyModalOpen = chatOpen || matchupOpen;

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={(e) => {
        if (anyModalOpen) return;
        const t = e.target as HTMLElement;
        if (t.closest("[data-game-chat-icon]") || t.closest("[data-matchup-button]")) return;
        router.push(href);
      }}
      onMouseDown={(e) => {
        if (anyModalOpen) e.stopPropagation();
      }}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !anyModalOpen) {
          e.preventDefault();
          router.push(href);
        }
      }}
      className={`relative flex flex-col text-center transition ${
        anyModalOpen
          ? "pointer-events-none select-none bg-white/5"
          : "cursor-pointer bg-white/5 hover:bg-white/[0.09]"
      } ${className}`}
    >
      {!hideMatchup && (
        <MatchupButton
          sport={sport}
          gameId={gameId}
          homeTeam={homeTeam}
          awayTeam={awayTeam}
          homeName={homeName}
          awayName={awayName}
          open={matchupOpen}
          onOpenChange={setMatchupOpen}
        />
      )}
      {!hideChat && (
        <GameChatIcon
          sport={sport}
          homeTeam={homeTeam}
          awayTeam={awayTeam}
          date={date}
          context={context}
          open={chatOpen}
          onOpenChange={setChatOpen}
        />
      )}
      {children}
    </div>
  );
}
