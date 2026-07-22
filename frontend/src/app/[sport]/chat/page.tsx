"use client";

import { useState, useRef, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import ChatSidebar from "@/components/ChatSidebar";
import LoginModal from "@/components/LoginModal";
import { useAuth } from "@/lib/auth-context";

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

const API_HOST = process.env.NEXT_PUBLIC_API_HOST || "http://localhost:8001";
const SPORT_CHAT_ENDPOINTS: Record<Sport, string> = {
  nfl: `${API_HOST}/chat`,
  nba: `${API_HOST}/chat/nba`,
  mlb: `${API_HOST}/chat/mlb`,
};

const SPORT_WELCOME: Record<Sport, string> = {
  nfl: "I'm Earl. I know ball. Ask me about spreads, player props, DFS lineups, or matchups — I've got the data to back it up.",
  nba: "I'm Earl. I know NBA. Ask me about spreads, player props, DFS builds, or matchups for basketball.",
  mlb: "I'm Earl. I know MLB. Ask me about moneyline bets, pitcher props, DFS stacks, or matchups for baseball.",
};

const SPORT_PLACEHOLDERS: Record<Sport, string> = {
  nfl: "Ask about spreads, DFS lineups, props, or matchups...",
  nba: "Ask about NBA spreads, DFS lineups, or matchups...",
  mlb: "Ask about MLB bets, DFS stacks, or matchups...",
};

const markdownComponents: Components = {
  table({ children }) {
    return (
      <div className="overflow-x-auto my-3">
        <table className="w-full text-xs border-collapse">{children}</table>
      </div>
    );
  },
  thead({ children }) {
    return <thead className="bg-white/10">{children}</thead>;
  },
  th({ children }) {
    return (
      <th className="px-3 py-2 text-left font-semibold text-earl-300 border-b border-white/10">
        {children}
      </th>
    );
  },
  td({ children }) {
    return <td className="px-3 py-1.5 border-b border-white/5">{children}</td>;
  },
  h1({ children }) {
    return <h1 className="text-base font-bold text-gray-100 mt-4 mb-1">{children}</h1>;
  },
  h2({ children }) {
    return <h2 className="text-sm font-bold text-gray-100 mt-4 mb-1">{children}</h2>;
  },
  h3({ children }) {
    return <h3 className="text-sm font-semibold text-gray-100 mt-3 mb-1">{children}</h3>;
  },
  hr() {
    return <hr className="border-white/10 my-4" />;
  },
  ul({ children }) {
    return <ul className="list-disc list-inside space-y-1 my-2">{children}</ul>;
  },
  ol({ children }) {
    return <ol className="list-decimal list-inside space-y-1 my-2">{children}</ol>;
  },
  p({ children }) {
    return <p className="text-gray-300 leading-relaxed mb-2 text-sm">{children}</p>;
  },
  a({ href, children }) {
    return (
      <a href={href} className="text-earl-400 hover:text-earl-300 underline" target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    );
  },
  strong({ children }) {
    return <strong className="font-bold text-gray-100">{children}</strong>;
  },
  code({ className, children, ...props }: React.ComponentPropsWithoutRef<"code">) {
    const isInline = !className;
    if (isInline) {
      return (
        <code className="bg-white/10 px-1 rounded text-xs" {...props}>
          {children}
        </code>
      );
    }
    return (
      <pre className="bg-black/40 rounded-lg p-3 my-3 overflow-x-auto text-xs">
        <code {...props}>{children}</code>
      </pre>
    );
  },
};

export default function ChatPage() {
  const params = useParams();
  const rawSport = params.sport as string;
  const sport: Sport = rawSport === "nba" || rawSport === "mlb" ? rawSport : "nfl";

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
    if (!convId) {
      startNewChat();
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
    }
  }, [sport, token, startNewChat]);



  async function handleSend() {
    if (!input.trim() || loading || !token) return;
    const userMsg = input.trim();

    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: userMsg }]);
    setLoading(true);
    setStatusText("Asking Earl...");
    await new Promise((r) => setTimeout(r, 0));

    const gotAnswer = { value: false };
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

      // Push an empty assistant message to stream into
      setMessages((prev) => [...prev, { role: "assistant", content: "" }]);

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
                }
                return updated;
              });
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
              }
              return updated;
            });
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