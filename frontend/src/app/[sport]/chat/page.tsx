"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { markdownComponents } from "@/components/markdown";
import ChatSidebar from "@/components/ChatSidebar";
import LoginModal from "@/components/LoginModal";
import { useAuth } from "@/lib/auth-context";
import { useSeo } from "@/components/Seo";

type Sport = "nfl" | "nba" | "mlb";

interface Message {
  role: "user" | "assistant";
  content: string;
}

const SPORT_NAMES: Record<Sport, string> = {
  nfl: "NFL",
  nba: "NBA",
  mlb: "MLB",
};

const SPORT_EMOJIS: Record<Sport, string> = {
  nfl: "🏈",
  nba: "🏀",
  mlb: "⚾",
};

const API_HOST = process.env.NEXT_PUBLIC_API_HOST || "/api";
const SPORT_CHAT_ENDPOINTS: Record<Sport, string> = {
  nfl: `${API_HOST}/chat`,
  nba: `${API_HOST}/chat/nba`,
  mlb: `${API_HOST}/chat/mlb`,
};

const SPORT_WELCOME: Record<Sport, string> = {
  nfl: "I'm Earl. I know ball. Ask me about spreads, player props, or matchups — I've got the data to back it up.",
  nba: "I'm Earl. I know NBA. Ask me about spreads, player props, or matchups for basketball.",
  mlb: "I'm Earl. I know MLB. Ask me about moneyline bets, pitcher props, or matchups for baseball.",
};

const SPORT_PLACEHOLDERS: Record<Sport, string> = {
  nfl: "Ask about spreads, DFS lineups, props, or matchups...",
  nba: "Ask about NBA spreads, DFS lineups, or matchups...",
  mlb: "Ask about MLB bets, DFS stacks, or matchups...",
};

export default function ChatPage() {
  const params = useParams();
  const rawSport = params.sport as string;
  const sport: Sport = rawSport === "nba" || rawSport === "mlb" ? rawSport : "nfl";

  const sportName = SPORT_NAMES[sport];
  useSeo({
    title: `${sportName} AI Handicapping Chat — Earl Knows Ball`,
    description: `Chat with Earl about ${sportName} — get AI handicapping insight on spreads, player props, matchups, and more.`,
    keywords: `${sportName} chat, AI handicapping, Earl Knows Ball, sports betting, ${sportName.toLowerCase()} picks`,
  });

  const [messages, setMessages] = useState<Message[]>([
    { role: "assistant", content: SPORT_WELCOME[sport] },
  ]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const { user, loading: authLoading } = useAuth();
  const [loginModalOpen, setLoginModalOpen] = useState(false);
  const [sidebarRefresh, setSidebarRefresh] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Default sidebar collapsed on mobile (small screens)
  useEffect(() => {
    setSidebarOpen(window.innerWidth >= 768);
  }, []);
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  // Tracks whether the user is currently reading near the bottom of the thread.
  // If the user scrolls up (e.g. to read the TOP of a long answer on mobile),
  // this flips false so we don't keep dragging them (see scroll effect below).
  const stickToBottom = useRef(true);
  // Element of the newest assistant message. Used as the scroll anchor so a
  // long response is revealed from its TOP (in the window) instead of stranding
  // the reader at the very bottom, which forces a scroll-back-up on mobile.
  const latestAssistantRef = useRef<HTMLDivElement | null>(null);
  const statusRef = useRef<HTMLSpanElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const [focusPending, setFocusPending] = useState(false);

  // Focus the chat input after selecting a conversation (waits for loading to finish)
  useEffect(() => {
    if (focusPending && !loading) {
      inputRef.current?.focus();
      setFocusPending(false);
    }
  }, [focusPending, loading]);

  useEffect(() => {
    /*
     * Scroll management.
     * Goal: when a (potentially long) answer arrives, the TOP of the response
     * should stay visible so you don't have to scroll back up to start reading
     * on mobile.
     *
     * Rules:
     *  - If the user is NOT near the bottom of the thread (they scrolled up to
     *    re-read earlier messages), do nothing — never fight their scroll.
     *  - Otherwise, anchor to the newest assistant message:
     *      * short message (fits in the viewport)  -> sit at the bottom (snug
     *        against the input, normal chat look).
     *      * tall message (longer than the viewport)-> align its TOP into view
     *        so the reader starts at the response's beginning and reads down.
     */
    if (!stickToBottom.current) return;

    const scroller = scrollRef.current;
    const anchor = latestAssistantRef.current;

    if (anchor && scroller) {
      const scrollRect = scroller.getBoundingClientRect();
      const anchorRect = anchor.getBoundingClientRect();
      const anchorTopInScroller =
        anchorRect.top - scrollRect.top + scroller.scrollTop;
      const anchorHeight = anchorRect.height;
      const viewport = scroller.clientHeight;

      if (anchorHeight > viewport * 0.6) {
        // Long answer: reveal its top (leave ~12px breathing room) instead of
        // jumping to the very bottom. Parks the start in view without smooth
        // so fast streams don't scroll-past.
        const target = Math.max(0, anchorTopInScroller - 12);
        if (Math.abs(scroller.scrollTop - target) > 4) {
          scroller.scrollTo({ top: target, behavior: "auto" });
        }
      } else {
        // Short/conversational answer: normal bottom-aligned placement.
        bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
      }
    } else {
      // No assistant message yet (initializing / new chat): sit at the bottom.
      bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    }
  }, [messages, loading]);

  useEffect(() => {
    const saved = localStorage.getItem("earl_token");
    if (saved) setToken(saved);
  }, []);

  // Live premium verification from the stored token, so the gate matches what
  // the premium article endpoints enforce. This is immune to a stale
  // useAuth().user (which can be resolved from an outdated earl_token cookie
  // instead of the localStorage token).
  const [livePremium, setLivePremium] = useState<boolean | null>(null);
  useEffect(() => {
    let cancelled = false;
    const tok = token || (typeof window !== "undefined" ? localStorage.getItem("earl_token") : null);
    if (!tok) {
      setLivePremium(false);
      return;
    }
    fetch(`/auth/me`, {
      headers: { Authorization: `Bearer ${tok}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((me) => {
        if (cancelled) return;
        const tier = me?.subscription_tier;
        setLivePremium(tier === "premium" || tier === "premium_yearly");
      })
      .catch(() => !cancelled && setLivePremium(false));
    return () => {
      cancelled = true;
    };
  }, [token]);

  // Reset when navigating between sports
  useEffect(() => {
    setMessages([{ role: "assistant", content: SPORT_WELCOME[sport] }]);
    setConversationId(null);
    setLoading(false);
    setStatusText(null);
  }, [sport]);

  const startNewChat = useCallback(() => {
    setMessages([{ role: "assistant", content: SPORT_WELCOME[sport] }]);
    setConversationId(null);
  }, [sport]);

  const loadConversation = useCallback(async (convId: string | null) => {
    // Collapse the sidebar on mobile after selecting New Chat or a historical chat.
    // On desktop (md+) the `md:flex` class keeps it visible regardless.
    setSidebarOpen(false);

    if (!convId) {
      startNewChat();
      setFocusPending(true);
      return;
    }

    const authToken = token || localStorage.getItem("earl_token");
    if (!authToken) return;

    try {
      setLoading(true);
      setStatusText("Loading conversation...");
      const res = await fetch(`${API_HOST}/chat/conversations/${sport}/${convId}`, {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (res.ok) {
        const data = await res.json();
        setMessages(data.messages.map((m: { role: string; content: string }) => ({
          role: m.role as "user" | "assistant",
          content: m.content,
        })));
        setConversationId(convId);
      }
    } catch {
      // silently fail
    } finally {
      setLoading(false);
      setStatusText(null);
      setFocusPending(true);
    }
  }, [sport, token, startNewChat, setSidebarOpen]);



  async function handleSend() {
    if (!input.trim() || loading || !token) return;
    const userMsg = input.trim();

    setInput("");
    // Park the user at the bottom for their new turn so the incoming response
    // starts in view (only relevant for long threads where they may have
    // scrolled back up to re-read earlier messages).
    stickToBottom.current = true;
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    setLoading(true);
    setStatusText("Asking Earl...");
    await new Promise((r) => setTimeout(r, 0));

    const gotAnswer = { value: false };
    // Unique id for this request so we can poll live status (Caddy gzip-buffers SSE,
    // so statuses don't stream live to the browser — we poll instead).
    const requestId =
      (typeof crypto !== "undefined" && crypto.randomUUID)
        ? crypto.randomUUID()
        : `req_${Date.now()}_${Math.random().toString(36).slice(2)}`;

    // Poll live status from the backend every ~650ms while we wait for the answer.
    const pollTimer = setInterval(async () => {
      try {
        const sr = await fetch(`${API_HOST}/chat/status/${requestId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (sr.status === 204) {
          // Status cleared -> finished. Let the SSE answer take over.
          return;
        }
        const sj = await sr.json();
        if (sj.status) setStatusText(sj.status);
      } catch {
        // ignore transient poll errors
      }
    }, 650);

    try {
      const endpoint = SPORT_CHAT_ENDPOINTS[sport];
      const res = await fetch(endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          message: userMsg,
          conversation_id: conversationId,
          request_id: requestId,
        }),
      });

      if (res.status === 401) {
        localStorage.removeItem("earl_token");
        setToken(null);
        setStatusText(null);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "🔑 Session expired. Please log in again." },
        ]);
        return;
      }

      if (res.status === 403) {
        setStatusText(null);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "⚠️ Premium subscription required. Upgrade to chat with Earl." },
        ]);
        return;
      }

      if (!res.ok) {
        setStatusText(null);
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: "Sorry, I hit an error. Try again." },
        ]);
        return;
      }

      // --- SSE streaming ---
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";
      let done = false;
      let newConvId: string | null = null;

      // NOTE: No empty assistant message is pushed here. We only render the
      // separate "Earl is researching" status box while loading, so the user
      // sees a single Earl box during research. The assistant message box is
      // created lazily when the first token/answer event actually arrives.

      while (!done) {
        const result = await reader.read();
        done = result.done;
        if (done) break;

        buffer += decoder.decode(result.value, { stream: true }).replace(/\r\n/g, "\n");
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;

          try {
            const data = JSON.parse(part.slice(6));

            if (data.type === "conv_id") {
              newConvId = data.conversation_id || data.id;
            } else if (data.type === "status") {
              // Update React state (not just the DOM ref) so the status text
              // survives re-renders. Directly mutating statusRef.textContent
              // was being overwritten by React's reconciliation whenever
              // any other state (messages, loading) changed, which is why
              // live status updates appeared to "stop working" even though
              // the backend was still sending them.
              setStatusText(data.message);
              if (statusRef.current) {
                statusRef.current.textContent = data.message;
              }
            } else if (data.type === "token") {
              gotAnswer.value = true;
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = {
                    ...last,
                    content: last.content + (data.token || ""),
                  };
                } else {
                  // Lazily create the assistant message on the first token
                  updated.push({ role: "assistant", content: data.token || "" });
                }
                return updated;
              });
            } else if (data.type === "answer") {
              // Backend emits a single "answer" event (no token streaming)
              gotAnswer.value = true;
              setStatusText(null);
              if (statusRef.current) statusRef.current.textContent = "";
              setMessages((prev) => {
                const updated = [...prev];
                const last = updated[updated.length - 1];
                if (last?.role === "assistant") {
                  updated[updated.length - 1] = { ...last, content: data.content || "" };
                } else {
                  updated.push({ role: "assistant", content: data.content || "" });
                }
                return updated;
              });
              setLoading(false);
            }
          } catch {
            // skip malformed events
          }
        }
      }

      // flush remaining buffer after stream ends
      if (buffer.startsWith("data: ")) {
        try {
          const data = JSON.parse(buffer.slice(6));
          if (data.type === "token") {
            gotAnswer.value = true;
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === "assistant") {
                updated[updated.length - 1] = {
                  ...last,
                  content: last.content + (data.token || ""),
                };
              } else {
                updated.push({ role: "assistant", content: data.token || "" });
              }
              return updated;
            });
          } else if (data.type === "answer") {
            gotAnswer.value = true;
            setStatusText(null);
            if (statusRef.current) statusRef.current.textContent = "";
            setMessages((prev) => {
              const updated = [...prev];
              const last = updated[updated.length - 1];
              if (last?.role === "assistant") {
                updated[updated.length - 1] = { ...last, content: data.content || "" };
              } else {
                updated.push({ role: "assistant", content: data.content || "" });
              }
              return updated;
            });
            setLoading(false);
          } else if (data.type === "conv_id" || data.id) {
            newConvId = data.conversation_id || data.id;
          }
        } catch {
          // ignore
        }
      }

      if (newConvId) {
        setConversationId(newConvId);
        setSidebarRefresh(true);
      }

      if (!gotAnswer.value) {
        setMessages((prev) => {
          const updated = [...prev];
          if (updated[updated.length - 1]?.role === "assistant" && !updated[updated.length - 1].content) {
            updated.pop();
          }
          return [
            ...updated,
            {
              role: "assistant",
              content: "Earl didn't have anything to say. Try rephrasing your question.",
            },
          ];
        });
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Sorry, something went wrong. Try again." },
      ]);
    } finally {
      clearInterval(pollTimer);
      setLoading(false);
      setStatusText(null);
      if (conversationId || true) {
        setSidebarRefresh(true);
      }
    }
  }

  // Render LoginModal at component level so it mounts regardless of which return fires
  const loginModal = <LoginModal open={loginModalOpen} onClose={() => setLoginModalOpen(false)} />;

  // --- Premium gate: shown to non-logged-in or non-premium users ---
  // isPremium derives from the LIVE token verify (livePremium) when available,
  // falling back to the auth context's user object.
  const contextPremium =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";
  const isPremium = livePremium ?? contextPremium;
  const showGate = !token || !isPremium;

  if (showGate) {
    return (
      <div className="min-h-screen flex items-center justify-center p-6">
        <div className="w-full max-w-md bg-white/5 rounded-2xl p-8 border border-white/10 text-center">
          
          <h1 className="text-xl font-bold text-gray-100 mb-2">
            {user ? `Earl Knows ${SPORT_NAMES[sport]}` : "AI Chat"}
          </h1>
          <div className="w-12 h-0.5 bg-earl-600 mx-auto my-4 rounded-full" />
          <p className="text-gray-300 text-sm mb-6">
            {user
              ? "Upgrade to Premium to chat with Earl about spreads, props, and matchups."
              : "Sign in and upgrade to Premium to chat with Earl about spreads, props, and matchups."}
          </p>

          {user ? (
            <a
              href="/pricing"
              className="inline-block w-full py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
            >
              Upgrade to Premium
            </a>
          ) : (
            <button
              onClick={() => setLoginModalOpen(true)}
              className="w-full py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
            >
              Sign In to Get Started
            </button>
          )}
        </div>
        {loginModal}
      </div>
    );
  }

  // --- Main chat UI ---
  // Index of the newest assistant message, used as the scroll anchor so a long
  // answer gets revealed from its TOP (stays in the window on mobile).
  let lastAssistantIndex = -1;
  for (let j = messages.length - 1; j >= 0; j--) {
    if (messages[j].role === "assistant") {
      lastAssistantIndex = j;
      break;
    }
  }
  return (
    <div className="max-w-[1280px] mx-auto w-full">
      <div className="relative flex h-[calc(100dvh-8rem)] overflow-hidden">
        {/* Mobile open button - only visible when sidebar is closed */}
        {!sidebarOpen && (
          <button
            onClick={() => setSidebarOpen(true)}
            className="fixed top-20 left-4 z-10 md:hidden text-gray-400 hover:text-white p-1 rounded-md hover:bg-white/10"
            aria-label="Open sidebar"
          >
            <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13 5l7 7-7 7M5 5l7 7-7 7" />
            </svg>
          </button>
        )}

        {/* Sidebar */}
        <div className={`${sidebarOpen ? "flex" : "hidden"} md:flex w-64 shrink-0`}>
          <ChatSidebar
            sport={sport}
            activeConversationId={conversationId}
            onSelectConversation={loadConversation}
            onRefreshNeeded={sidebarRefresh}
            onRefreshed={() => setSidebarRefresh(false)}
            onClose={() => setSidebarOpen(false)}
          />
        </div>

        {/* Chat area */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Messages */}
          <div
            ref={scrollRef}
            onScroll={() => {
              const el = scrollRef.current;
              if (!el) return;
              // Roughly "near the bottom": within 120px of the bottom edge.
              const nearBottom =
                el.scrollHeight - el.scrollTop - el.clientHeight < 120;
              stickToBottom.current = nearBottom;
            }}
            className="flex-1 overflow-y-auto px-4 py-8 space-y-4"
          >
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  ref={
                    msg.role === "assistant" && i === lastAssistantIndex
                      ? (el: HTMLDivElement | null) => {
                          latestAssistantRef.current = el;
                        }
                      : undefined
                  }
                  className={`max-w-[85%] rounded-2xl px-4 py-3 break-words ${
                    msg.role === "user"
                      ? "bg-earl-600/20 border border-earl-600/30 text-gray-200"
                      : "bg-white/5 border border-white/10 text-gray-300"
                  }`}
                >
                  <div className="text-xs font-semibold text-gray-500 mb-1 uppercase tracking-wide">
                    {msg.role === "user" ? "You" : `${SPORT_EMOJIS[sport]} Earl`}
                  </div>
                  <div className="text-sm leading-relaxed prose prose-invert max-w-none">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={markdownComponents}
                    >
                      {msg.content}
                    </ReactMarkdown>
                  </div>
                </div>
              </div>
            ))}

            {loading && (
              <div className="flex justify-start">
                <div className="bg-white/5 border border-white/10 rounded-2xl px-4 py-3 max-w-[85%] break-words">
                  <div className="text-xs font-semibold text-gray-500 mb-1 uppercase tracking-wide">
                    {SPORT_EMOJIS[sport]} Earl
                  </div>
                  <span className="w-2 h-2 bg-earl-400 rounded-full animate-pulse" />
                  <span className="italic ml-2 text-sm text-gray-400" ref={statusRef}>{statusText}</span>
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="border-t border-white/10 p-4">
            <div className="flex gap-2">
              <input
                ref={inputRef}
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                placeholder={SPORT_PLACEHOLDERS[sport]}
                className="flex-1 px-4 py-3 rounded-xl bg-white/5 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
                disabled={loading}
              />
              <button
                onClick={handleSend}
                disabled={loading || !input.trim()}
                className="px-6 py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition disabled:opacity-50"
              >
                Send
              </button>
            </div>
          </div>
        </div>
        {loginModal}
    </div>
    </div>
  );
}