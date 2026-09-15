"use client";

import { useCallback, useEffect, useState } from "react";
import { useSeo } from "@/components/Seo";

const token = () => localStorage.getItem("earl_token");

interface FreeChatConfig {
  enabled: boolean;
  monthly_tokens: number;
}

export default function AdminFreeChat() {
  useSeo({ title: "Free Chat — Admin — Earl Knows Ball" });
  const [cfg, setCfg] = useState<FreeChatConfig | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [monthlyTokens, setMonthlyTokens] = useState<number>(100000);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/admin/settings/free-chat", {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) {
        const data: FreeChatConfig = await res.json();
        setCfg(data);
        setEnabled(data.enabled);
        setMonthlyTokens(data.monthly_tokens);
      } else {
        alert(`Failed to load free-chat settings (HTTP ${res.status})`);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const res = await fetch("/api/admin/settings/free-chat", {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token()}`,
        },
        body: JSON.stringify({ enabled, monthly_tokens: Math.max(0, Math.floor(monthlyTokens || 0)) }),
      });
      if (res.ok) {
        const data: FreeChatConfig = await res.json();
        setCfg(data);
        setMsg("Saved.");
        setTimeout(() => setMsg(null), 2500);
      } else {
        alert(`Save failed (HTTP ${res.status})`);
      }
    } finally {
      setSaving(false);
    }
  };

  const dirty =
    !!cfg && (cfg.enabled !== enabled || cfg.monthly_tokens !== Math.max(0, Math.floor(monthlyTokens || 0)));

  return (
    <div className="max-w-2xl">
      <h1 className="text-2xl font-bold text-white mb-1">Free Chat</h1>
      <p className="text-gray-400 text-sm mb-6">
        Let logged-in, non-premium members use chat on a limited monthly token grant.
        Free chat never exposes picks, predictions, or futures, and the model is instructed
        not to give betting advice.
      </p>

      {loading ? (
        <p className="text-gray-400">Loading…</p>
      ) : (
        <div className="bg-gray-800/60 border border-gray-700 rounded-lg p-5 space-y-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-white font-medium">Enable free chat</div>
              <div className="text-gray-400 text-xs">
                When off, non-premium members see an upgrade prompt instead of the chat.
              </div>
            </div>
            <button
              type="button"
              role="switch"
              aria-checked={enabled}
              onClick={() => setEnabled((v) => !v)}
              className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                enabled ? "bg-green-600" : "bg-gray-600"
              }`}
            >
              <span
                className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                  enabled ? "translate-x-6" : "translate-x-1"
                }`}
              />
            </button>
          </div>

          <div>
            <label className="block text-white font-medium mb-1">
              Monthly tokens for non-premium members
            </label>
            <div className="text-gray-400 text-xs mb-2">
              Resets at the start of each month. Premium members keep their plan allotment.
            </div>
            <input
              type="number"
              min={0}
              step={1000}
              value={monthlyTokens}
              onChange={(e) => setMonthlyTokens(Number(e.target.value))}
              className="w-48 bg-gray-900 border border-gray-700 rounded px-3 py-2 text-white"
            />
            <div className="text-gray-500 text-xs mt-1">
              {Math.max(0, Math.floor(monthlyTokens || 0)).toLocaleString()} tokens / month
            </div>
          </div>

          <div className="flex items-center gap-3">
            <button
              onClick={save}
              disabled={saving || !dirty}
              className="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 disabled:cursor-not-allowed text-white text-sm font-medium px-4 py-2 rounded"
            >
              {saving ? "Saving…" : "Save"}
            </button>
            {msg && <span className="text-green-400 text-sm">{msg}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
