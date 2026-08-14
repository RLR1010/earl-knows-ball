"use client";

import { useEffect, useRef, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { useSeo } from "@/components/Seo";

interface CSMsg {
  id: number;
  role: string;
  content: string;
  tokens_used: number;
  model: string | null;
  created_at: string | null;
}

interface ChatReply {
  reply: string;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  monthly_tokens: number;
  monthly_limit: number;
  escalated: boolean;
}

export default function SupportPage() {
  useSeo({ title: "Customer Service — Earl Knows Ball" });
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();

  const [messages, setMessages] = useState<CSMsg[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [monthly, setMonthly] = useState(0);
  const [limit, setLimit] = useState(200000);
  const [error, setError] = useState<string | null>(null);

  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const loaded = useRef(false);

  const token = () => localStorage.getItem("earl_token");

  const loadHistory = useCallback(async () => {
    try {
      const res = await fetch("/api/cs/history", {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setMessages(data.messages);
      setMonthly(data.monthly_tokens);
      setLimit(data.monthly_limit);
    } catch (e) {
      console.error("Failed to load CS history:", e);
    }
  }, []);

  // Redirect if not logged in.
  useEffect(() => {
    if (!authLoading && !user) router.replace("/login");
  }, [authLoading, user, router]);

  // Load history once when authenticated.
  useEffect(() => {
    if (authLoading || !user || loaded.current) return;
    loaded.current = true;
    loadHistory();
  }, [authLoading, user, loadHistory]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const send = async () => {
    const text = input.trim();
    if (!text || loading) return;
    setError(null);
    const userMsg: CSMsg = {
      id: Date.now(),
      role: "user",
      content: text,
      tokens_used: 0,
      model: null,
      created_at: new Date().toISOString(),
    };
    setMessages((m) => [...m, userMsg]);
    setInput("");
    setLoading(true);

    // Optimistically bump usage so the bar reflects the pending message.
    const pendingMonthly = monthly + Math.round(text.length / 4);
    setMonthly(pendingMonthly);

    try {
      const res = await fetch("/api/cs/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token()}`,
        },
        body: JSON.stringify({ message: text }),
      });
      if (res.status === 429) {
        const d = await res.json();
        setError(d.detail || "You've reached your monthly customer service limit.");
        setMonthly(pendingMonthly);
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d: ChatReply = await res.json();
      if (d.total_tokens > 0) {
        setMonthly(d.monthly_tokens);
      } else {
        setMonthly(pendingMonthly);
      }
      setMessages((m) => [
        ...m,
        {
          id: Date.now(),
          role: "assistant",
          content: d.reply,
          tokens_used: d.total_tokens,
          model: null,
          created_at: new Date().toISOString(),
        },
      ]);
    } catch (e: any) {
      console.error("CS chat error:", e);
      setError("Something went wrong sending your message. Please try again.");
    } finally {
      setLoading(false);
      inputRef.current?.focus();
    }
  };

  const pct = Math.min(100, Math.round((monthly / limit) * 100));

  return (
    <div className="max-w-3xl mx-auto px-4 py-10">
      <div className="mb-6">
        <h1 className="text-3xl font-bold text-white">Customer Service</h1>
        <p className="text-gray-400 mt-1">
          Questions about your account, billing, or the product? We're here to help.
        </p>
      </div>

      {/* Monthly usage bar */}
      <div className="mb-6">
        <div className="flex justify-between text-xs text-gray-400 mb-1">
          <span>Monthly usage</span>
          <span>
            {monthly.toLocaleString()} / {limit.toLocaleString()} tokens
          </span>
        </div>
        <div className="h-2 w-full bg-white/10 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              pct >= 90 ? "bg-red-500" : pct >= 70 ? "bg-amber-500" : "bg-earl-500"
            }`}
            style={{ width: `${pct}%` }}
          />
        </div>
        {pct >= 90 && (
          <p className="text-red-400 text-xs mt-1">
            You're close to your monthly customer service limit.
          </p>
        )}
      </div>

      {/* Chat window */}
      <div className="rounded-2xl border border-white/10 bg-[#11151c] overflow-hidden flex flex-col h-[520px]">
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {messages.length === 0 && !loading ? (
            <div className="text-gray-500 text-sm text-center mt-10">
              Ask us anything about your account, billing, or the service. 👋
            </div>
          ) : (
            messages.map((m) => (
              <div key={m.id} className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[80%] rounded-2xl px-4 py-2.5 text-sm whitespace-pre-wrap ${
                    m.role === "user"
                      ? "bg-earl-600 text-white rounded-br-sm"
                      : "bg-white/10 text-gray-100 rounded-bl-sm"
                  }`}
                >
                  {m.content}
                </div>
              </div>
            ))
          )}
          {loading && (
            <div className="flex justify-start">
              <div className="bg-white/10 text-gray-300 rounded-2xl rounded-bl-sm px-4 py-2.5 text-sm flex items-center gap-2">
                <span className="inline-block h-2 w-2 bg-earl-400 rounded-full animate-pulse" />
                Earl is typing…
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {error && (
          <div className="px-4 py-2 bg-red-900/30 border-t border-red-900/40 text-red-300 text-sm">
            {error}
          </div>
        )}

        <div className="border-t border-white/10 p-3 flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder="Type your question… (Enter to send)"
            className="flex-1 bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-500 resize-none"
            disabled={loading || monthly >= limit}
          />
          <button
            onClick={send}
            disabled={loading || !input.trim() || monthly >= limit}
            className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm font-medium hover:bg-earl-500 disabled:opacity-40 disabled:cursor-not-allowed transition"
          >
            Send
          </button>
        </div>
      </div>
    </div>
  );
}
