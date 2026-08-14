"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Sparkles, X, Send, Loader2 } from "lucide-react";
import { markdownComponents } from "@/components/markdown";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "@/components/LoginModal";
import PricingGateModal from "@/components/PricingGateModal";

type Sport = "nfl" | "nba" | "mlb";

interface Message {
  role: "user" | "assistant";
  content: string;
}

const SPORT_EMOJIS: Record<Sport, string> = {
  nfl: "🏈",
  nba: "🏀",
  mlb: "⚾",
};

const SPORT_CHAT_ENDPOINTS: Record<Sport, string> = {
  nfl: `${process.env.NEXT_PUBLIC_API_HOST || "/api"}/chat`,
  nba: `${process.env.NEXT_PUBLIC_API_HOST || "/api"}/chat/nba`,
  mlb: `${process.env.NEXT_PUBLIC_API_HOST || "/api"}/chat/mlb`,
};

/** Short date, e.g. "Wed, Aug 13" (ET) — matches the game card. */
function formatDate(iso?: string) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    timeZone: "America/New_York",
  });
}

interface GameChatModalProps {
  open: boolean;
  onClose: () => void;
  sport: "nfl" | "nba" | "mlb";
  homeTeam: string;
  awayTeam: string;
  date?: string;
  /** Pre-built game context (matchup, lines, Earl's picks) injected into each
   *  message so the LLM knows exactly which game we're discussing. */
  context?: string;
}

export default function GameChatModal({
  open,
  onClose,
  sport,
  homeTeam,
  awayTeam,
  date,
  context,
}: GameChatModalProps) {
  const { user, loading: authLoading } = useAuth();
  const [token, setToken] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [loginOpen, setLoginOpen] = useState(false);
  const [pricingOpen, setPricingOpen] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Load token from localStorage once.
  useEffect(() => {
    const saved = localStorage.getItem("earl_token");
    if (saved) setToken(saved);
  }, []);

  // Reset state + seed the Earl greeting bubble every time the modal opens.
  const greeting =
    `Do you want me to tell you more about this game — **${awayTeam} @ ${homeTeam}` +
    (date ? ` on ${formatDate(date)}` : "") +
    `** — or is there anything in particular about it you want to discuss?`;

  const isPremium =
    user?.subscription_tier === "premium" || user?.subscription_tier === "ultimate";

  // Auth gate: when the modal opens (and auth has loaded), route by status.
  //  - not logged in        -> LoginModal (no chat)
  //  - logged in, no prem   -> PricingGateModal (pricing content)
  //  - logged in, premium   -> chat

  // Reset state + open the right panel when the modal opens.
  useEffect(() => {
    if (!open) return;
    setMessages([{ role: "assistant", content: greeting }]);
    setInput("");
    setLoading(false);
    setStatusText(null);
    setConversationId(null);

    // Gates decided once per open (auth already loaded / or will load shortly).
    setLoginOpen(false);
    setPricingOpen(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Whenever the chat modal is open and auth finishes loading, apply the gate.
  useEffect(() => {
    if (!open || authLoading) return;
    if (!user) {
      setLoginOpen(true);
      setPricingOpen(false);
    } else if (!(isPremium)) {
      setLoginOpen(false);
      setPricingOpen(true);
    } else {
      setLoginOpen(false);
      setPricingOpen(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, authLoading, user]);

  // Keep the thread scrolled to the bottom.
  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, statusText, open]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // While the modal is open, swallow ANY mouse/pointer event whose target is
  // OUTSIDE the modal panel (capture phase, before React handlers). This makes it
  // impossible for the game card underneath to receive clicks / mousedowns / text
  // -selection highlights or hover-capturing events, so highlighting the chat
  // input can never navigate to the game detail page.
  useEffect(() => {
    if (!open) return;
    const swallow = (e: Event) => {
      const t = e.target as HTMLElement | null;
      if (t && t.closest("[data-game-chat-modal]")) return;
      e.stopPropagation();
      if (e.type === "mousedown") e.preventDefault();
    };
    for (const evt of ["pointerdown", "mousedown", "click"]) {
      document.addEventListener(evt, swallow, true);
    }
    return () => {
      for (const evt of ["pointerdown", "mousedown", "click"]) {
        document.removeEventListener(evt, swallow, true);
      }
    };
  }, [open]);

  const sendMessage = useCallback(
    async (raw?: string) => {
      const text = (raw ?? input).trim();
      if (!text || loading) return;

      const resolvedToken = token || localStorage.getItem("earl_token");
      if (!resolvedToken) {
        setLoginOpen(true);
        return;
      }

      setLoading(true);
      setStatusText("Asking Earl...");
      // Keep the user bubble clean; send context to the LLM so it knows the game.
      const cleanText = text;
      const systemContext = context || undefined;
      const userMsg: Message = { role: "user", content: cleanText };
      setMessages((prev) => [...prev, userMsg]);
      setInput("");

      const requestId =
        crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;

      // Research/status updates don't stream through Caddy (gzip buffers SSE), so
      // we poll the dedicated status endpoint — same as the full /chat page.
      // NOTE: the status endpoint is ALWAYS /api/chat/status/{request_id} for all
      // sports (the nba/mlb routers register it at the root, not per-sport), so
      // we must NOT use SPORT_CHAT_ENDPOINTS[sport] + "/status/" (that 404s for
      // NBA/MLB).
      const STATUS_BASE = `${process.env.NEXT_PUBLIC_API_HOST || "/api"}/chat/status`;
      const pollStatus = () => {
        fetch(`${STATUS_BASE}/${requestId}`, {
          headers: { Authorization: `Bearer ${resolvedToken}` },
        })
          .then((r) => {
            if (r.status === 204) {
              // Status cleared -> finished; let the answer itself show.
              setStatusText(null);
              return null;
            }
            return r.ok ? r.json() : null;
          })
          .then((d) => {
            if (d && typeof d.status === "string" && d.status) {
              setStatusText(d.status);
            }
          })
          .catch(() => {});
      };
      const statusTimer = window.setInterval(pollStatus, 700);

      const gotAnswer = { value: false };
      let newConvId: string | null = null;

      try {
        const res = await fetch(SPORT_CHAT_ENDPOINTS[sport], {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${resolvedToken}`,
          },
          body: JSON.stringify({
            message: cleanText,
            // Game context (matchup/lines/picks) is sent SEPARATELY so it reaches
            // Earl as a system instruction but is NOT stored in chat history —
            // keeps the history/list titles clean instead of showing [GAME CONTEXT].
            ...(systemContext ? { system_context: systemContext } : {}),
            request_id: requestId,
            // Reuse the persisted conversation when the user keeps asking —
            // this links turns so Earl has context and the chat is saved.
            ...(conversationId ? { conversation_id: conversationId } : {}),
          }),
        });

        if (!res.ok || !res.body) {
          if (res.status === 401 || res.status === 403) {
            setMessages((prev) => [
              ...prev,
              { role: "assistant", content: "⚠️ Please log in to continue chatting with Earl." },
            ]);
          } else {
            setMessages((prev) => [
              ...prev,
              { role: "assistant", content: "Sorry, Earl couldn't reach the game data. Try again." },
            ]);
          }
          return;
        }

        const reader = res.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";

        // eslint-disable-next-line no-constant-condition
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

          const parts = buffer.split("\n\n");
          buffer = parts.pop() || "";

          for (const part of parts) {
            if (!part.startsWith("data: ")) continue;
            let data: Record<string, unknown>;
            try {
              data = JSON.parse(part.slice(6));
            } catch {
              continue;
            }

            if (data.type === "conv_id") {
              newConvId = (data.conversation_id as string) || (data.id as string);
              gotAnswer.value = true;
            } else if (data.type === "status") {
              // Live research updates (same as the /chat page).
              setStatusText((data.message as string) || null);
            } else if (data.type === "token") {
              gotAnswer.value = true;
              setStatusText(null);
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = {
                    ...last,
                    content: last.content + ((data.token as string) || ""),
                  };
                } else {
                  updated.push({ role: "assistant", content: (data.token as string) || "" });
                }
                return updated;
              });
            } else if (data.type === "answer") {
              gotAnswer.value = true;
              setStatusText(null);
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = { ...last, content: (data.content as string) || "" };
                } else {
                  updated.push({ role: "assistant", content: (data.content as string) || "" });
                }
                return updated;
              });
            }
          }
        }
      } catch {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "Sorry, something went wrong. Try again." },
        ]);
      } finally {
        window.clearInterval(statusTimer);
        if (newConvId) setConversationId(newConvId);
        setLoading(false);
        setStatusText(null);
      }
    },
    [sport, input, loading, token, conversationId, context]
  );

  if (!open) return null;

  const modal = (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-[90] bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      {/* Panel */}
      <div
        className="fixed inset-x-0 bottom-0 sm:inset-0 sm:flex sm:items-center sm:justify-center sm:p-6 z-[95] pointer-events-none"
        role="dialog"
        aria-modal="true"
      >
        <div
          data-game-chat-modal
          className="pointer-events-auto w-full sm:max-w-2xl h-[80vh] sm:h-[min(80vh,720px)] flex flex-col bg-[#0b1220] border border-white/10 rounded-t-2xl sm:rounded-2xl shadow-2xl shadow-black/60 overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-white/10 bg-gradient-to-r from-earl-600/30 to-earl-500/10">
            <div className="flex items-center gap-2.5 min-w-0">
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-earl-500 to-earl-700 text-white shadow-lg shadow-earl-600/30">
                <Sparkles className="h-4 w-4" />
              </span>
              <div className="min-w-0">
                <div className="text-sm font-bold text-white truncate">
                  {awayTeam} <span className="text-gray-400">@</span> {homeTeam}
                </div>
                <div className="text-[11px] text-earl-300/80 font-medium">
                  {SPORT_EMOJIS[sport]} {sport.toUpperCase()} · {date ? formatDate(date) : "Upcoming"} · Ask Earl about this game
                </div>
              </div>
            </div>
            <button
              onClick={onClose}
              aria-label="Close chat"
              className="shrink-0 flex h-8 w-8 items-center justify-center rounded-full text-gray-400 hover:text-white hover:bg-white/10 transition"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
            {authLoading && messages.length === 0 ? (
              <div className="text-sm text-gray-400 flex items-center gap-2 pt-2">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading Earl...
              </div>
            ) : (
              messages.map((m, i) => (
                <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                  <div
                    className={`max-w-[85%] rounded-2xl px-3.5 py-2.5 text-sm leading-relaxed whitespace-pre-wrap ${
                      m.role === "user"
                        ? "bg-earl-600 text-white rounded-br-sm"
                        : "bg-white/5 text-gray-100 border border-white/10 rounded-bl-sm"
                    }`}
                  >
                    {m.role === "assistant" ? (
                      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                        {m.content || "…"}
                      </ReactMarkdown>
                    ) : (
                      m.content
                    )}
                  </div>
                </div>
              ))
            )}

            {/* Live research status (same as /chat page) */}
            {loading && statusText && messages.length > 0 && (
              <div className="flex justify-start">
                <div className="max-w-[85%] rounded-2xl px-3.5 py-2.5 text-xs text-gray-300 bg-white/[0.03] border border-white/5 rounded-bl-sm flex items-start gap-2">
                  <Loader2 className="h-3.5 w-3.5 mt-0.5 animate-spin text-earl-400 shrink-0" />
                  <span>{statusText}</span>
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Footer / input */}
          <div className="border-t border-white/10 bg-[#0d1526] px-3 py-3">
            <div className="flex items-center gap-2">
              <input
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendMessage();
                  }
                }}
                placeholder="Ask Earl about this game..."
                disabled={loading}
                className="flex-1 rounded-xl bg-white/5 border border-white/10 px-3.5 py-2.5 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:ring-2 focus:ring-earl-500/50 disabled:opacity-60"
              />
              <button
                onClick={() => sendMessage()}
                disabled={loading || !input.trim()}
                aria-label="Send"
                className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-gradient-to-r from-earl-600 to-earl-500 text-white shadow-lg shadow-earl-600/25 hover:from-earl-500 hover:to-earl-400 disabled:opacity-40 disabled:cursor-not-allowed transition"
              >
                {loading ? <Loader2 className="h-5 w-5 animate-spin" /> : <Send className="h-5 w-5" />}
              </button>
            </div>
            <p className="mt-2 text-[10px] text-gray-500">
              Earl is AI-generated handicapping insight powered by live team and player data. Not financial advice — bet responsibly.
            </p>
          </div>
        </div>
      </div>
    </>
  );

  if (typeof document === "undefined") return null;

  // If we're gated (login or pricing), show only that gate modal — the chat
  // panel is hidden. The gate modals render their own portal + backdrop.
  // Closing a gate modal closes the WHOLE thing (never fall back to the chat
  // for a non-premium user).
  if (loginOpen || pricingOpen) {
    return createPortal(
      <>
        <LoginModal
          open={loginOpen}
          onSuccess={() => setLoginOpen(false)} // after login, gate effect re-routes
          onClose={() => {
            setLoginOpen(false);
            onClose();
          }}
        />
        <PricingGateModal
          open={pricingOpen}
          onClose={() => {
            setPricingOpen(false);
            onClose();
          }}
        />
      </>,
      document.body
    );
  }

  return createPortal(modal, document.body);
}
