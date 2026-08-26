"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

interface ChatMessage {
  id: string;
  role: string;
  message: string;
  sport: string;
  tokens_used: number | null;
  model: string | null;
  created_at: string | null;
}

interface ChatConversation {
  conversation_id: string;
  user_id: string;
  user_email: string;
  user_name: string | null;
  user_tier: string;
  sport: string;
  message_count: number;
  turn_count: number;
  total_tokens: number;
  first_message_at: string | null;
  last_message_at: string | null;
  messages: ChatMessage[];
}

interface ChatListResponse {
  conversations: ChatConversation[];
  total: number;
}

const TIER_COLORS: Record<string, string> = {
  premium: "bg-amber-900/30 text-amber-400",
  premium_yearly: "bg-purple-900/30 text-purple-400",
  free: "bg-gray-800 text-gray-400",
};

const SPORT_COLORS: Record<string, string> = {
  nfl: "bg-green-900/30 text-green-400",
  nba: "bg-orange-900/30 text-orange-400",
  mlb: "bg-blue-900/30 text-blue-400",
};

const token = () => localStorage.getItem("earl_token");

export default function AdminChats() {
  useSeo({ title: "User Chats — Admin — Earl Knows Ball" });
  const [items, setItems] = useState<ChatConversation[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [skip, setSkip] = useState(0);
  const [limit] = useState(25);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const fetchChats = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      params.set("skip", String(skip));
      params.set("limit", String(limit));
      const res = await fetch(`/api/admin/chats?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data: ChatListResponse = await res.json();
      setItems(data.conversations);
      setTotal(data.total);
    } catch (e: any) {
      console.error("Failed to load chats:", e);
    } finally {
      setLoading(false);
    }
  }, [skip, limit]);

  useEffect(() => { fetchChats(); }, [fetchChats]);

  const formatDateTime = (d: string | null) =>
    d ? new Date(d).toLocaleString() : "—";
  const formatDate = (d: string | null) =>
    d ? new Date(d).toLocaleDateString() : "—";
  const isNow = (ts: string | null) =>
    ts === null || Math.abs(Date.now() - new Date(ts).getTime()) < 60000;

  const page = Math.floor(skip / limit);
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">User Chats</h1>
        <p className="text-gray-400 text-sm mt-1">
          Latest conversations across all users — see what they ask and how Earl responds
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3 mb-6">
        <button
          onClick={fetchChats}
          className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
        >
          Refresh
        </button>
        <span className="text-xs text-gray-500">{total} conversation{total === 1 ? "" : "s"}</span>
      </div>

      {loading ? (
        <div className="text-gray-400">Loading chats...</div>
      ) : items.length === 0 ? (
        <div className="text-gray-500">No chats yet.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-white/10 text-left text-gray-400 text-xs uppercase tracking-wider">
                <th className="pb-3 pr-4 font-semibold">User</th>
                <th className="pb-3 pr-4 font-semibold">Tier</th>
                <th className="pb-3 pr-4 font-semibold">Sport</th>
                <th className="pb-3 pr-4 font-semibold">Msgs</th>
                <th className="pb-3 pr-4 font-semibold">Turns</th>
                <th className="pb-3 pr-4 font-semibold">Tokens</th>
                <th className="pb-3 font-semibold">Last Activity</th>
              </tr>
            </thead>
            <tbody>
              {items.map((conv) => (
                <FragmentRow
                  key={conv.conversation_id}
                  conv={conv}
                  expanded={expandedId === conv.conversation_id}
                  onToggle={() =>
                    setExpandedId((cur) =>
                      cur === conv.conversation_id ? null : conv.conversation_id
                    )
                  }
                  formatDateTime={formatDateTime}
                  formatDate={formatDate}
                  isNow={isNow}
                />
              ))}
            </tbody>
          </table>
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
        <span className="text-xs text-gray-500">
          Page {page + 1} of {pages}
        </span>
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

function FragmentRow({
  conv,
  expanded,
  onToggle,
  formatDateTime,
  formatDate,
  isNow,
}: {
  conv: ChatConversation;
  expanded: boolean;
  onToggle: () => void;
  formatDateTime: (d: string | null) => string;
  formatDate: (d: string | null) => string;
  isNow: (ts: string | null) => boolean;
}) {
  return (
    <>
      <tr
        onClick={onToggle}
        className="border-b border-white/5 hover:bg-white/[0.03] cursor-pointer"
      >
        <td className="py-3 pr-4">
          <div className="text-white">{conv.user_email}</div>
          {conv.user_name && (
            <div className="text-xs text-gray-500">{conv.user_name}</div>
          )}
        </td>
        <td className="py-3 pr-4">
          <span
            className={`px-2 py-0.5 rounded-full text-xs font-medium ${
              TIER_COLORS[conv.user_tier] || "bg-gray-800 text-gray-400"
            }`}
          >
            {conv.user_tier}
          </span>
        </td>
        <td className="py-3 pr-4">
          {conv.sport ? (
            <span
              className={`px-2 py-0.5 rounded-full text-xs font-medium uppercase ${
                SPORT_COLORS[conv.sport] || "bg-gray-800 text-gray-400"
              }`}
            >
              {conv.sport}
            </span>
          ) : (
            <span className="text-gray-600">—</span>
          )}
        </td>
        <td className="py-3 pr-4 text-gray-300">{conv.message_count}</td>
        <td className="py-3 pr-4 text-gray-300">{conv.turn_count}</td>
        <td className="py-3 pr-4 text-gray-300">
          {conv.total_tokens > 0 ? conv.total_tokens.toLocaleString() : "—"}
        </td>
        <td className="py-3 pr-4 text-xs text-gray-400">
          {isNow(conv.last_message_at) ? (
            <span className="text-green-400">just now</span>
          ) : (
            <span title={formatDateTime(conv.last_message_at)}>
              {formatDate(conv.last_message_at)}
            </span>
          )}
        </td>
      </tr>

      {expanded && (
        <tr className="border-b border-white/10 bg-black/30">
          <td colSpan={7} className="py-4 px-2">
            <div className="space-y-4 max-h-[65vh] overflow-y-auto pr-2">
              {conv.messages.length === 0 ? (
                <div className="text-gray-500 text-sm">No messages captured.</div>
              ) : (
                conv.messages.map((m) => (
                  <div key={m.id} className="flex gap-3">
                    <div
                      className={`flex-none w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold ${
                        m.role === "user"
                          ? "bg-earl-600/80 text-white"
                          : "bg-white/10 text-white"
                      }`}
                      title={m.role}
                    >
                      {m.role === "user" ? "U" : "E"}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="text-xs font-medium text-gray-300 uppercase tracking-wider">
                          {m.role === "user" ? "User" : "Earl"}
                        </span>
                        <span className="text-[11px] text-gray-600">
                          {formatDateTime(m.created_at)}
                        </span>
                        {m.tokens_used != null && m.tokens_used > 0 && (
                          <span className="text-[11px] text-gray-500">
                            · {m.tokens_used.toLocaleString()} tok
                          </span>
                        )}
                        {m.model && (
                          <span className="text-[11px] text-gray-600 font-mono">
                            · {m.model}
                          </span>
                        )}
                      </div>
                      <div
                        className={`text-sm whitespace-pre-wrap rounded-lg px-3 py-2 ${
                          m.role === "user"
                            ? "bg-earl-900/20 text-green-100"
                            : "bg-white/5 text-gray-100"
                        }`}
                      >
                        {m.message || <span className="text-gray-600">(empty)</span>}
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
