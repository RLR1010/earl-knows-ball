"use client";

import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import GameChatIcon from "@/components/GameChatIcon";

interface ChatCardLinkProps {
  href: string;
  sport: "nfl" | "nba" | "mlb";
  homeTeam: string;
  awayTeam: string;
  date?: string | null;
  /** Pre-built game context (matchup, lines, picks) passed to the chat modal. */
  context?: string;
  /** When true, don't render the Earl chat icon (e.g. on Final games). */
  hideChat?: boolean;
  className?: string;
  children?: ReactNode;
}

/**
 * Clickable game-card container. Serves as the card's own "link" to the game
 * detail page, but with the chat icon integrated so that:
 *  - hovering / clicking the chat icon never lights up the card or navigates,
 *  - while the chat modal is open the whole card is made pointer-events:none
 *    (no hover, no click capture), so interacting with the modal — including
 *    highlighting the text box — can never navigate to the game page.
 */
export default function ChatCardLink({
  href,
  sport,
  homeTeam,
  awayTeam,
  date,
  context,
  hideChat = false,
  className = "",
  children,
}: ChatCardLinkProps) {
  const router = useRouter();
  const [chatOpen, setChatOpen] = useState(false);

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={(e) => {
        if (chatOpen) return;
        const t = e.target as HTMLElement;
        if (t.closest("[data-game-chat-icon]")) return;
        router.push(href);
      }}
      onMouseDown={(e) => {
        if (chatOpen) e.stopPropagation();
      }}
      onKeyDown={(e) => {
        if ((e.key === "Enter" || e.key === " ") && !chatOpen) {
          e.preventDefault();
          router.push(href);
        }
      }}
      className={`relative block text-center transition ${
        chatOpen
          ? "pointer-events-none select-none bg-white/5"
          : "cursor-pointer bg-white/5 hover:bg-white/[0.09]"
      } ${className}`}
    >
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
