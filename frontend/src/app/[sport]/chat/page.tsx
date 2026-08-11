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
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    const saved = localStorage.getItem("earl_token");
    if (saved) setToken(saved);
  }, []);

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
  const isPremium = user?.subscription_tier === "premium" || user?.subscription_tier === "ultimate";

  if (!token || !isPremium) {
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
          <div className="flex-1 overflow-y-auto px-4 py-8 space-y-4">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
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