"use client";
import { useState, useRef, useEffect } from "react";

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

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

// Direct backend URL — Next.js proxy buffers streaming responses (GZIPs them,
// waits for entire stream). CORS is wide-open so direct calls work fine.
const API_HOST = "http://localhost:8001";
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
    return <p className="mb-2 last:mb-0">{children}</p>;
  },
  strong({ children }) {
    return <strong className="text-gray-100 font-semibold">{children}</strong>;
  },
  code({ children, className, ...props }) {
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
  const [sport, setSport] = useState<Sport>("nfl");
  const [messages, setMessages] = useState<Record<Sport, Message[]>>({
    nfl: [{ role: "assistant", content: SPORT_WELCOME.nfl }],
    nba: [{ role: "assistant", content: SPORT_WELCOME.nba }],
    mlb: [{ role: "assistant", content: SPORT_WELCOME.mlb }],
  });
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState<string | null>(null);
  const [conversationIds, setConversationIds] = useState<Record<Sport, string | null>>({
    nfl: null,
    nba: null,
    mlb: null,
  });
  const [token, setToken] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showLogin, setShowLogin] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);
const statusRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, sport]);

  useEffect(() => {
    const saved = localStorage.getItem("earl_token");
    if (saved) setToken(saved);
  }, []);

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      const data = await res.json();
      localStorage.setItem("earl_token", data.access_token);
      setToken(data.access_token);
    } catch {
      alert("Login failed");
    }
  }

  async function handleRegister(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password, display_name: email.split("@")[0] }),
      });
      const data = await res.json();
      localStorage.setItem("earl_token", data.access_token);
      setToken(data.access_token);
    } catch {
      alert("Registration failed. Email may already be in use.");
    }
  }

  async function handleSend() {
    if (!input.trim() || loading || !token) return;
    const userMsg = input.trim();
    setInput("");
    setMessages((prev) => ({
      ...prev,
      [sport]: [...prev[sport], { role: "user", content: userMsg }],
    }));
    setLoading(true);
    setStatusText("Asking Earl...");
    // Yield so React commits the loading card to the DOM before fetch starts.
    // Otherwise statusRef.current won't exist when SSE status events arrive.
    await new Promise((r) => setTimeout(r, 0));

    // Track via mutation so finally block can check without scope issues
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
          conversation_id: conversationIds[sport],
        }),
      });

      if (res.status === 401) {
        localStorage.removeItem("earl_token");
        setToken(null);
        setStatusText(null);
        setMessages((prev) => ({
          ...prev,
          [sport]: [
            ...prev[sport],
            { role: "assistant", content: "🔑 Session expired. Please log in again." },
          ],
        }));
        return;
      }

      if (res.status === 403) {
        setStatusText(null);
        setMessages((prev) => ({
          ...prev,
          [sport]: [
            ...prev[sport],
            { role: "assistant", content: "⚠️ Premium subscription required. Upgrade to chat with Earl." },
          ],
        }));
        return;
      }

      if (!res.ok) {
        setStatusText(null);
        setMessages((prev) => ({
          ...prev,
          [sport]: [...prev[sport], { role: "assistant", content: "Sorry, I hit an error. Try again." }],
        }));
        return;
      }

      // --- SSE streaming ---
      const reader = res.body?.getReader();
      if (!reader) throw new Error("No response body");

      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // Normalize \r\n to \n — sse-starlette uses \r\n by default
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");

        // Process complete SSE events (delimited by \n\n)
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";

        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;

          try {
            const data = JSON.parse(part.slice(6));

            if (data.type === "conv_id") {
              setConversationIds((prev) => ({ ...prev, [sport]: data.id }));
            } else if (data.type === "status") {
              // Write directly to the DOM — bypasses React batching
              if (statusRef.current) statusRef.current.textContent = data.message;
              // Wait 500ms so each status text is readable before the next.
              await new Promise((r) => setTimeout(r, 500));
            } else if (data.type === "answer") {
              gotAnswer.value = true;
              setStatusText(null);
              setMessages((prev) => ({
                ...prev,
                [sport]: [...prev[sport], { role: "assistant", content: data.content }],
              }));
              setLoading(false);
            }
          } catch {
            // Skip malformed SSE lines
          }
        }
      }

      if (!gotAnswer.value) {
        setMessages((prev) => ({
          ...prev,
          [sport]: [
            ...prev[sport],
            { role: "assistant", content: "I was researching your question but hit a snag. Could you try rephrasing?" },
          ],
        }));
        setLoading(false);
        setStatusText(null);
      }
    } catch {
      setStatusText(null);
      setLoading(false);
      setMessages((prev) => ({
        ...prev,
        [sport]: [...prev[sport], { role: "assistant", content: "Sorry, I hit an error. Try again." }],
      }));
    } finally {
      // If error but we got an answer, keep loading already cleared by answer handler
      if (!gotAnswer.value) {
        setLoading(false);
        setStatusText(null);
      }
    }
  }

  // If not logged in, show auth form
  if (!token) {
    return (
      <div className="max-w-md mx-auto pt-16 space-y-6">
        <div className="text-center space-y-2">
          <h1 className="font-display text-3xl font-bold">
            🧠 <span className="text-earl-400">Earl</span> Knows Ball
          </h1>
          <p className="text-gray-400 text-sm">Sign in to chat with Earl</p>
        </div>

        <div className="border border-white/10 rounded-xl p-6 bg-white/5 space-y-4">
          <div className="flex gap-2">
            <button
              onClick={() => setShowLogin(true)}
              className={`flex-1 py-2 rounded-lg text-sm font-semibold transition ${
                showLogin ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400"
              }`}
            >
              Login
            </button>
            <button
              onClick={() => setShowLogin(false)}
              className={`flex-1 py-2 rounded-lg text-sm font-semibold transition ${
                !showLogin ? "bg-earl-600 text-white" : "bg-white/5 text-gray-400"
              }`}
            >
              Register
            </button>
          </div>

          <form onSubmit={showLogin ? handleLogin : handleRegister} className="space-y-3">
            <input
              type="email"
              placeholder="Email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
            />
            <input
              type="password"
              placeholder="Password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
            />
            <button
              type="submit"
              className="w-full py-2 rounded-lg bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
            >
              {showLogin ? "Login" : "Register"}
            </button>
          </form>
        </div>
      </div>
    );
  }

  // Chat UI with sport tabs
  const currentMessages = messages[sport];

  return (
    <div className="max-w-3xl mx-auto flex flex-col h-[calc(100vh-8rem)]">
      {/* Sport Tab Bar */}
      <div className="flex gap-1 px-4 pt-2 border-b border-white/10">
        {(Object.keys(SPORT_NAMES) as Sport[]).map((s) => (
          <button
            key={s}
            onClick={() => setSport(s)}
            className={`px-4 py-2 rounded-t-lg text-sm font-semibold transition ${
              sport === s
                ? "bg-earl-600 text-white"
                : "bg-white/5 text-gray-400 hover:text-gray-200"
            }`}
          >
            {SPORT_EMOJIS[s]} {SPORT_NAMES[s]}
          </button>
        ))}
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto space-y-4 p-4">
        {currentMessages.map((m, i) => (
          <div key={i} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div
              className={`max-w-[80%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                m.role === "user"
                  ? "bg-earl-600 text-white rounded-br-md"
                  : "bg-white/10 text-gray-200 rounded-bl-md"
              }`}
            >
              {m.role === "assistant" && (
                <span className="font-semibold text-earl-400 text-xs block mb-1">
                  Earl ({SPORT_NAMES[sport]})
                </span>
              )}
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={markdownComponents}
              >
                {m.content}
              </ReactMarkdown>
            </div>
          </div>
        ))}

        {loading && statusText && (
          <div className="flex justify-start">
            <div className="max-w-[80%] rounded-2xl px-4 py-3 text-sm bg-white/10 text-gray-400 rounded-bl-md">
              <div className="flex items-center gap-2">
                <span className="w-2 h-2 bg-earl-400 rounded-full animate-pulse" />
                <span className="italic" ref={statusRef}>{statusText}</span>
              </div>
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
  );
}
