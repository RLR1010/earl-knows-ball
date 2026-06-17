"use client";
import { useState, useRef, useEffect } from "react";

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

const SPORT_CHAT_ENDPOINTS: Record<Sport, string> = {
  nfl: "/api/chat",
  nba: "/api/chat/nba",
  mlb: "/api/chat/mlb",
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

export default function ChatPage() {
  const [sport, setSport] = useState<Sport>("nfl");
  const [messages, setMessages] = useState<Record<Sport, Message[]>>({
    nfl: [{ role: "assistant", content: SPORT_WELCOME.nfl }],
    nba: [{ role: "assistant", content: SPORT_WELCOME.nba }],
    mlb: [{ role: "assistant", content: SPORT_WELCOME.mlb }],
  });
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
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

      if (res.status === 403) {
        setMessages((prev) => ({
          ...prev,
          [sport]: [
            ...prev[sport],
            { role: "assistant", content: "⚠️ Premium subscription required. Upgrade to chat with Earl." },
          ],
        }));
        return;
      }

      const data = await res.json();
      if (data.conversation_id) {
        setConversationIds((prev) => ({ ...prev, [sport]: data.conversation_id }));
      }
      setMessages((prev) => ({
        ...prev,
        [sport]: [...prev[sport], { role: "assistant", content: data.response }],
      }));
    } catch {
      setMessages((prev) => ({
        ...prev,
        [sport]: [...prev[sport], { role: "assistant", content: "Sorry, I hit an error. Try again." }],
      }));
    } finally {
      setLoading(false);
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
              {m.content}
            </div>
          </div>
        ))}
        {loading && (
          <div className="flex justify-start">
            <div className="bg-white/10 rounded-2xl rounded-bl-md px-4 py-3">
              <span className="text-earl-400 text-sm">Earl is thinking</span>
              <span className="animate-pulse">...</span>
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
