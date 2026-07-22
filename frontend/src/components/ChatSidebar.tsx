"use client";

import { useState, useEffect, useCallback } from "react";


interface Conversation {
  id: string;
  title: string;
  message_count: number;
  started_at: string | null;
}

interface ChatSidebarProps {
  sport: "nfl" | "nba" | "mlb";
  activeConversationId: string | null;
  onSelectConversation: (id: string | null) => void;
  onRefreshNeeded: boolean;
  onRefreshed: () => void;
  onClose?: () => void;
}

export default function ChatSidebar({
  sport,
  activeConversationId,
  onSelectConversation,
  onRefreshNeeded,
  onRefreshed,
  onClose,
}: ChatSidebarProps) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const API_HOST = "http://localhost:8001";

  const getToken = () => localStorage.getItem("earl_token");

  const fetchConversations = useCallback(async () => {
    const token = getToken();
    if (!token) return;
    setLoading(true);
    try {
      const res = await fetch(`${API_HOST}/chat/conversations/${sport}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        const data = await res.json();
        setConversations(data.conversations || []);
      }
    } catch {
      // silently fail
    } finally {
      setLoading(false);
      onRefreshed();
    }
  }, [sport, API_HOST, onRefreshed]);

  useEffect(() => {
    fetchConversations();
  }, [fetchConversations]);

  useEffect(() => {
    if (onRefreshNeeded) {
      fetchConversations();
    }
  }, [onRefreshNeeded, fetchConversations]);

  const handleDelete = async (convId: string, e: React.MouseEvent) => {
    e.stopPropagation();
    const token = getToken();
    if (!token) return;
    setDeleting(convId);
    try {
      const res = await fetch(`${API_HOST}/chat/conversations/${sport}/${convId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token}` },
      });
      if (res.ok) {
        // If we were viewing this conversation, go to new chat
        if (activeConversationId === convId) {
          onSelectConversation(null);
        }
        fetchConversations();
      }
    } catch {
      // silently fail
    } finally {
      setDeleting(null);
    }
  };

  const formatDate = (iso: string | null) => {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffDays === 0) {
      return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } else if (diffDays === 1) {
      return "Yesterday";
    } else if (diffDays < 7) {
      return `${diffDays}d ago`;
    } else {
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    }
  };

  return (
    <div className="flex flex-col h-full w-full bg-[#0c1220] border-r border-white/10 overflow-hidden">
      {/* Header */}
      <div className="p-3 border-b border-white/10 space-y-2">
        {/* Close button - top right */}
        {onClose && (
          <div className="flex justify-end">
            <button
              onClick={onClose}
              className="md:hidden text-gray-400 hover:text-white p-1 rounded-md hover:bg-white/10"
              aria-label="Close sidebar"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M11 19l-7-7 7-7m8 14l-7-7 7-7" />
              </svg>
            </button>
          </div>
        )}
        <button
          onClick={() => onSelectConversation(null)}
          className={`w-full py-2.5 px-4 rounded-lg text-sm font-semibold transition-colors flex items-center justify-center gap-2 ${
            activeConversationId === null
              ? "bg-earl-600 text-white"
              : "text-gray-300 hover:bg-white/10 hover:text-white border border-white/10"
          }`}
        >
          <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          New Chat
        </button>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto">
        {loading && conversations.length === 0 ? (
          <div className="flex items-center justify-center py-8">
            <div className="w-5 h-5 border-2 border-gray-500 border-t-white rounded-full animate-spin" />
          </div>
        ) : conversations.length === 0 ? (
          <div className="text-center py-8 px-4">
            <p className="text-xs text-gray-500">No conversations yet</p>
            <p className="text-xs text-gray-600 mt-1">Start a new chat above</p>
          </div>
        ) : (
          <div className="py-1 space-y-0.5">
            {conversations.map((conv) => {
              const isActive = conv.id === activeConversationId;
              return (
                <button
                  key={conv.id}
                  onClick={() => onSelectConversation(conv.id)}
                  className={`w-full text-left px-3 py-2.5 transition-colors group relative ${
                    isActive
                      ? "bg-earl-600/15 border-l-2 border-earl-500"
                      : "hover:bg-white/5 border-l-2 border-transparent"
                  }`}
                >
                  <div className="flex items-start justify-between gap-1">
                    <div className="flex-1 min-w-0">
                      <p className={`text-xs font-medium truncate ${
                        isActive ? "text-gray-100" : "text-gray-300"
                      }`}>
                        {conv.title || "New conversation"}
                      </p>
                      <p className="text-[10px] text-gray-500 mt-0.5">
                        {formatDate(conv.started_at)}
                      </p>
                    </div>
                    <button
                      onClick={(e) => handleDelete(conv.id, e)}
                      disabled={deleting === conv.id}
                      className="shrink-0 p-1 rounded text-gray-600 hover:text-red-400 hover:bg-red-400/10 opacity-0 group-hover:opacity-100 transition-opacity disabled:opacity-50"
                      title="Delete conversation"
                    >
                      {deleting === conv.id ? (
                        <div className="w-3 h-3 border-2 border-gray-500 border-t-red-400 rounded-full animate-spin" />
                      ) : (
                        <svg xmlns="http://www.w3.org/2000/svg" className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                        </svg>
                      )}
                    </button>
                  </div>
                </button>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
