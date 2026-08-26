"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

interface CsMsg {
  id: number;
  role: string;
  content: string;
  tokens_used: number;
  model: string | null;
  created_at: string | null;
}

interface CsConv {
  user_id: string;
  user_email: string;
  user_name: string | null;
  user_tier: string;
  message_count: number;
  total_tokens: number;
  last_message_at: string | null;
  messages: CsMsg[];
}

interface CsListResponse {
  conversations: CsConv[];
  total: number;
}

const TIER_COLORS: Record<string, string> = {
  premium: "bg-amber-900/30 text-amber-400",
  premium_yearly: "bg-purple-900/30 text-purple-400",
  free: "bg-gray-800 text-gray-400",
};

const token = () => localStorage.getItem("earl_token");

const fmtDT = (d: string | null) => (d ? new Date(d).toLocaleString() : "—");
const fmtD = (d: string | null) => (d ? new Date(d).toLocaleDateString() : "—");
const isNow = (ts: string | null) =>
  ts === null || Math.abs(Date.now() - new Date(ts).getTime()) < 60000;

export default function CustomerServiceAdmin() {
  useSeo({ title: "Customer Service — Admin — Earl Knows Ball" });

  const [convs, setConvs] = useState<CsConv[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [skip, setSkip] = useState(0);
  const [limit] = useState(50);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null); // email composer for this user

  const fetchConvs = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set("skip", String(skip));
      params.set("limit", String(limit));
      const res = await fetch(`/api/admin/cs/chats?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: CsListResponse = await res.json();
      setConvs(data.conversations);
      setTotal(data.total);
    } catch (e) {
      console.error("Failed to load CS conversations:", e);
    } finally {
      setLoading(false);
    }
  }, [skip, limit]);

  useEffect(() => { fetchConvs(); }, [fetchConvs]);

  const toggle = async (uid: string) => {
    if (expandedId === uid) { setExpandedId(null); return; }
    setExpandedId(uid);
    setOpen(null);
    // Fetch the full thread if not already loaded.
    try {
      const res = await fetch(`/api/admin/cs/chats/${uid}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) {
        const data: CsConv = await res.json();
        setConvs((prev) => prev.map((c) => (c.user_id === uid ? data : c)));
      }
    } catch (e) {
      console.error("Failed to load thread:", e);
    }
  };

  const page = Math.floor(skip / limit);
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Customer Service</h1>
        <p className="text-gray-400 text-sm mt-1">
          Support chat conversations across all users. Click a user to read the thread or email them.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 mb-6">
        <button
          onClick={fetchConvs}
          className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
        >
          Refresh
        </button>
        <span className="text-xs text-gray-500">{total} conversation{total === 1 ? "" : "s"}</span>
      </div>

      {loading ? (
        <div className="text-gray-400">Loading conversations...</div>
      ) : convs.length === 0 ? (
        <div className="text-gray-500">No customer service chats yet.</div>
      ) : (
        <div className="space-y-2">
          {convs.map((c) => (
            <div key={c.user_id} className="rounded-xl border border-white/10 bg-white/[0.02]">
              {/* Header row */}
              <button
                onClick={() => toggle(c.user_id)}
                className="w-full flex flex-wrap items-center gap-4 px-5 py-3 text-left hover:bg-white/[0.03] transition"
              >
                <div className="min-w-0 flex-1">
                  <span className="text-white font-medium truncate">{c.user_email}</span>
                  {c.user_name && (
                    <span className="text-gray-500 text-sm"> · {c.user_name}</span>
                  )}
                </div>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${TIER_COLORS[c.user_tier] || "bg-gray-800 text-gray-400"}`}>
                  {c.user_tier}
                </span>
                <span className="text-xs text-gray-400">{c.message_count} msgs</span>
                <span className="text-xs text-gray-400">{c.total_tokens.toLocaleString()} tok</span>
                <span className="text-xs text-gray-500">
                  {isNow(c.last_message_at) ? (
                    <span className="text-green-400">just now</span>
                  ) : (
                    <span title={fmtDT(c.last_message_at)}>{fmtD(c.last_message_at)}</span>
                  )}
                </span>
                <span className="text-gray-500 text-xs">▾</span>
              </button>

              {/* Expanded thread + email composer */}
              {expandedId === c.user_id && (
                <div className="border-t border-white/10 px-5 py-4">
                  {/* Thread */}
                  <div className="space-y-4 max-h-[60vh] overflow-y-auto pr-2 mb-5">
                    {c.messages.length === 0 ? (
                      <div className="text-gray-500 text-sm">No messages in this thread.</div>
                    ) : (
                      c.messages.map((m) => (
                        <div key={m.id} className="flex gap-3">
                          <div className={`flex-none w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${m.role === "user" ? "bg-earl-600/80 text-white" : "bg-white/10 text-white"}`}>
                            {m.role === "user" ? "U" : "E"}
                          </div>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2 mb-1">
                              <span className="text-xs font-medium text-gray-300 uppercase tracking-wider">
                                {m.role === "user" ? "User" : "Earl"}
                              </span>
                              <span className="text-[11px] text-gray-600">{fmtDT(m.created_at)}</span>
                              {m.tokens_used > 0 && (
                                <span className="text-[11px] text-gray-500">· {m.tokens_used.toLocaleString()} tok</span>
                              )}
                              {m.model && (
                                <span className="text-[11px] text-gray-600 font-mono">· {m.model}</span>
                              )}
                            </div>
                            <div className={`text-sm whitespace-pre-wrap rounded-lg px-3 py-2 ${m.role === "user" ? "bg-earl-900/20 text-green-100" : "bg-white/5 text-gray-100"}`}>
                              {m.content}
                            </div>
                          </div>
                        </div>
                      ))
                    )}
                  </div>

                  {/* Email the customer */}
                  <button
                    onClick={() => setOpen(open === c.user_id ? null : c.user_id)}
                    className="text-xs font-medium text-earl-400 hover:text-earl-300 transition"
                  >
                    {open === c.user_id ? "− Close email composer" : "+ Email this customer"}
                  </button>
                  {open === c.user_id && (
                    <EmailComposer userId={c.user_id} email={c.user_email} onSent={() => {}} />
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Pagination */}
      <div className="flex items-center justify-between mt-6">
        <button
          onClick={() => setSkip((s) => Math.max(0, s - limit))}
          disabled={page === 0}
          className="px-4 py-2 bg-white/5 border border-white/10 rounded-lg text-sm text-white hover:bg-white/10 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          ← Prev
        </button>
        <span className="text-xs text-gray-500">Page {page + 1} of {pages}</span>
        <button
          onClick={() => setSkip((s) => s + limit)}
          disabled={page + 1 >= pages}
          className="px-4 py-2 bg-white/5 border border-white/10 rounded-lg text-sm text-white hover:bg-white/10 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          Next →
        </button>
      </div>
    </div>
  );
}

function EmailComposer({ userId, email, onSent }: { userId: string; email: string; onSent: () => void }) {
  const [subject, setSubject] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const send = async () => {
    if (!subject.trim() || !body.trim() || sending) return;
    setSending(true);
    setResult(null);
    try {
      const res = await fetch("/api/admin/cs/emails", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token()}`,
        },
        body: JSON.stringify({ user_id: userId, subject, body }),
      });
      const data = await res.json();
      setResult(data.message || (res.ok ? "Email queued." : "Failed to send."));
      if (res.ok) { setSubject(""); setBody(""); onSent(); }
    } catch (e) {
      setResult("Failed to send email.");
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="mt-3 rounded-lg border border-white/10 bg-black/30 p-4 space-y-3">
      <div className="text-xs text-gray-500">
        Sending from Earl Knows Ball Support &lt;support@users.earlknowsball.com&gt; to <span className="text-gray-300">{email}</span>
      </div>
      <input
        value={subject}
        onChange={(e) => setSubject(e.target.value)}
        placeholder="Subject"
        className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-500"
      />
      <textarea
        value={body}
        onChange={(e) => setBody(e.target.value)}
        rows={5}
        placeholder="Message…"
        className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-500 resize-none"
      />
      <div className="flex items-center gap-3">
        <button
          onClick={send}
          disabled={sending || !subject.trim() || !body.trim()}
          className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm font-medium hover:bg-earl-500 disabled:opacity-40 disabled:cursor-not-allowed transition"
        >
          {sending ? "Sending…" : "Send Email"}
        </button>
        {result && <span className="text-xs text-gray-400">{result}</span>}
      </div>
    </div>
  );
}
