"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

const token = () => localStorage.getItem("earl_token");

// ---- types mirroring backend contracts (app/routers/social_x.py) ----
type DraftStatus = "draft" | "queued" | "approved" | "scheduled" | "sent" | "failed" | "discarded";

interface StatusRes {
  connected?: boolean;
  ok?: boolean | null;
  error?: string | null;
  username?: string | null;
  user_id?: string | null;
  name?: string | null;
  verified?: boolean | null;
  message?: string | null;
}

interface Seed {
  kind: string;
  text: string;
  source_ref: Record<string, unknown>;
}

interface ContentTypeMeta {
  label: string;
  desc: string;
  loader: string;
}

interface Draft {
  id: number;
  text: string;
  content_type: string;
  sport?: string | null;
  source_ref: Record<string, unknown>;
  status: string;
  created_at?: string | null;
  media_id?: string | null;
  card_image_ref?: string | null;
  error?: string | null;
  tweet_id?: string | null;
}

interface HistoryPost {
  x_tweet_id: string;
  text: string;
  created_at?: string | null;
  error?: string | null;
}

type Tab = "connect" | "compose" | "drafts" | "history";

async function xFetch<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`/api/admin/x${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token()}`,
      ...(options?.headers || {}),
    },
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const j = await res.json();
      detail = (j as { detail?: string }).detail || detail;
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

// Reusable auth-gated-request wrapper with a helpful message when we lack an admin token.
async function authed<T>(fn: () => Promise<T>): Promise<T> {
  if (!token()) throw new Error("No admin token found. Please sign in first.");
  return fn();
}

export default function XSocialPage() {
  useSeo({ title: "X Social — Earl Admin", description: "Connect X + compose/send posts" });

  const [tab, setTab] = useState<Tab>("connect");
  const [status, setStatus] = useState<StatusRes | null>(null);
  const [statusLoading, setStatusLoading] = useState(false);

  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [history, setHistory] = useState<HistoryPost[]>([]);
  const [listMsgs, setListMsgs] = useState<{ ok: boolean; text: string } | null>(null);

  const refreshStatus = useCallback(async () => {
    setStatusLoading(true);
    try {
      const s = await authed(() => xFetch<StatusRes>("/status"));
      setStatus(s);
    } catch (e) {
      setStatus(null);
      setListMsgs({ ok: false, text: (e as Error).message });
    } finally {
      setStatusLoading(false);
    }
  }, []);

  const refreshDrafts = useCallback(async () => {
    try {
      const d = await authed(() => xFetch<{ drafts: Draft[] }>("/drafts?limit=50"));
      setDrafts(d.drafts);
    } catch (e) {
      setListMsgs({ ok: false, text: (e as Error).message });
      setDrafts([]);
    }
  }, []);

  const refreshHistory = useCallback(async () => {
    try {
      const h = await authed(() => xFetch<{ posts: HistoryPost[] }>("/history"));
      setHistory(h.posts);
    } catch (e) {
      setListMsgs({ ok: false, text: (e as Error).message });
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    if (tab === "drafts") refreshDrafts();
    if (tab === "history") refreshHistory();
  }, [tab, refreshStatus, refreshDrafts, refreshHistory]);

  const setMsg = (ok: boolean, text: string) => {
    setListMsgs({ ok, text });
    setTimeout(() => setListMsgs(null), 6000);
  };

  const onDelete = async (id: number) => {
    try {
      await authed(() => xFetch<void>(`/drafts/${id}`, { method: "DELETE" }));
      await refreshDrafts();
    } catch (e) { setMsg(false, (e as Error).message); }
  };

  const onStatus = async (id: number, statusVal: string) => {
    try {
      await authed(() => xFetch<Draft>(`/drafts/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: statusVal }),
      }));
      await refreshDrafts();
    } catch (e) { setMsg(false, (e as Error).message); }
  };

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">X — @earlknowsball</h1>
      </div>

      <div className="flex flex-wrap gap-2 border-b border-white/10 pb-3">
        {(
          [
            ["connect", "Connect / Status"],
            ["compose", "Compose"],
            ["drafts", "Drafts"],
            ["history", "Published"],
          ] as [Tab, string][]
        ).map(([k, label]) => (
          <button
            key={k}
            onClick={() => setTab(k)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition
              ${tab === k ? "bg-white/10 text-white" : "text-gray-400 hover:text-white hover:bg-white/5"}`}
          >
            {label}
          </button>
        ))}
      </div>

      {listMsgs && (
        <div
          className={`rounded-xl border px-4 py-3 text-sm ${
            listMsgs.ok
              ? "bg-emerald-900/20 border-emerald-700/40 text-emerald-300"
              : "bg-red-900/20 border-red-800/30 text-red-300"
          }`}
        >
          {listMsgs.text}
        </div>
      )}

      {tab === "connect" && <ConnectTab status={status} loading={statusLoading} refreshing={statusLoading} onRefresh={refreshStatus} />}
      {tab === "compose" && <ComposeTab onSaved={setMsg} />}
      {tab === "drafts" && <DraftsTab drafts={drafts} onDelete={onDelete} onStatus={onStatus} />}
      {tab === "history" && <HistoryTab posts={history} />}
    </div>
  );
}

/* ============================== CONNECT ============================== */
function ConnectTab({ status, loading, refreshing, onRefresh }: {
  status: StatusRes | null;
  loading: boolean;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  const [form, setForm] = useState({ api_key: "", api_secret: "", access_token: "", access_token_secret: "" });
  const [busy, setBusy] = useState(false);
  const [resMsg, setResMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const realStatus = status || { connected: false };

  const doConnect = async (withForm: boolean) => {
    setBusy(true);
    try {
      const payload = withForm ? form : {};
      const s = await authed(() => xFetch<StatusRes>("/connect", {
        method: "POST",
        body: JSON.stringify(payload),
      }));
      setResMsg({ ok: !!s.ok, text: s.ok ? `Connected as @${s.username}${s.verified ? " ✅" : ""}` : (s.error || s.message || "Not connected.") });
      onRefresh();
    } catch (e) {
      setResMsg({ ok: false, text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid lg:grid-cols-2 gap-6">
      <div className="bg-white/[0.02] border border-white/5 rounded-xl p-6 space-y-4">
        <h2 className="text-lg font-semibold text-white">Connection status</h2>
        <dl className="space-y-2 text-sm">
          <Row label="Configured" value={status ? (status.connected ? "Yes" : "No") : "loading…"} />
          <Row label="Connected" value={status?.connected && status?.ok ? "Yes ✅" : refreshing ? "checking…" : "No"} />
          <Row label="Handle" value={status?.username || (status?.ok === false ? "—" : "…")} />
        </dl>
        {status?.error && (
          <div className="bg-amber-900/20 border border-amber-700/40 rounded-lg px-3 py-2 text-amber-300 text-xs break-words">
            Probe error: {status.error}
          </div>
        )}
        {status?.message && (
          <div className="bg-blue-900/20 border border-blue-700/40 rounded-lg px-3 py-2 text-blue-300 text-xs">
            {status.message}
          </div>
        )}
        <div className="flex gap-2">
          <button
            onClick={async () => { try { await authed(() => xFetch<Seed[]>(`/seeds?content_type=best_pick&limit=1`)); setResMsg({ ok: true, text: "API reachable." }); } catch (e) { setResMsg({ ok: false, text: (e as Error).message }); } }}
            className="px-3 py-2 rounded-lg bg-white/10 hover:bg-white/15 text-sm text-white"
          >
            Test API
          </button>
        </div>
      </div>

      <div className="bg-white/[0.02] border border-white/5 rounded-xl p-6 space-y-4">
        <h2 className="text-lg font-semibold text-white">Connect @earlknowsball</h2>
        <p className="text-sm text-gray-400">
          Best approach: put the four keys in <code className="text-gray-300">compute .env</code> as{" "}
          <code className="text-gray-300">X_CONSUMER_KEY / X_CONSUMER_SECRET / X_ACCESS_TOKEN / X_ACCESS_TOKEN_SECRET</code>.
          Use the form below to test a specific set (empty fields fall back to .env) without touching the server files.
        </p>
        <div className="space-y-2">
          {(
            [
              ["api_key", "API Key (consumer key)", true],
              ["api_secret", "API Secret (consumer secret)", true],
              ["access_token", "Access Token", true],
              ["access_token_secret", "Access Token Secret", true],
            ] as [keyof typeof form, string, boolean][]
          ).map(([k, label]) => (
            <label key={k} className="block text-sm">
              <span className="text-gray-400">{label}</span>
              <input
                type="text"
                value={form[k]}
                onChange={(e) => setForm((f) => ({ ...f, [k]: e.target.value }))}
                className="mt-1 w-full rounded-lg bg-black/40 border border-white/10 px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                placeholder={label}
              />
            </label>
          ))}
        </div>
        {resMsg && <StatusMsg ok={resMsg.ok} text={resMsg.text} />}
        <button
          onClick={() => doConnect(false)}
          disabled={busy}
          className="w-full px-4 py-2.5 rounded-lg bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-sm font-semibold text-white"
        >
          {busy ? "Testing…" : "Test current credentials (.env)"}
        </button>
        <button
          onClick={() => doConnect(true)}
          disabled={busy}
          className="w-full px-4 py-2 rounded-lg bg-white/10 hover:bg-white/15 disabled:opacity-50 text-sm text-white"
        >
          Test with the keys above
        </button>
        <p className="text-2xs text-gray-600 text-xs">Posting spends metered credits — this screen only verifies connect (a read, ~free). Sending happens in Compose/Drafts.</p>
      </div>
    </div>
  );
}

/* ============================== COMPOSE ============================== */
function ComposeTab({ onSaved }: { onSaved: (ok: boolean, s: string) => void }) {
  const [contentType, setContentType] = useState<"best_pick" | "record_update">("best_pick");
  const [types, setTypes] = useState<Record<string, ContentTypeMeta>>({});
  const [seeds, setSeeds] = useState<Seed[]>([]);
  const [seedIdx, setSeedIdx] = useState(0);
  const [text, setText] = useState("");
  const [sport, setSport] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    authed(() => xFetch<{ content_types: Record<string, ContentTypeMeta> }>("/content-types"))
      .then((r) => setTypes(r.content_types))
      .catch(() => {});
  }, []);

  const loadSeeds = useCallback(async () => {
    setBusy(true);
    try {
      const q = `/seeds?content_type=${contentType}&limit=6${sport ? `&sport=${sport}` : ""}`;
      const s = await authed(() => xFetch<Seed[]>(q));
      setSeeds(s);
      setSeedIdx(0);
      setText(s[0]?.text || "");
    } catch (e) {
      onSaved(false, (e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [contentType, sport, onSaved]);

  const activeType = types[contentType] || { label: contentType, desc: "" };

  const saveDraft = async (sendNow: boolean) => {
    if (!text.trim()) { onSaved(false, "Text is empty."); return; }
    const src = seeds[seedIdx]?.source_ref || {};
    try {
      const body: Record<string, unknown> = {
        content_type: contentType,
        sport: sport || null,
        source_ref: src,
        text: text.trim(),
      };
      const d = await authed(() => xFetch<Draft>("/drafts", { method: "POST", body: JSON.stringify(body) }));
      if (sendNow) {
        void (async () => {
          try {
            const sent = await authed(() => xFetch<Draft>(`/drafts/${d.id}/send`, { method: "POST", body: JSON.stringify({}) }));
            onSaved(!!sent.tweet_id, sent.error ? `Failed on X: ${sent.error}` : `Posted to X ✅ tweet ${sent.text ? "— check @earlknowsball" : ""}`);
          } catch (e) { onSaved(false, (e as Error).message); }
        })();
        onSaved(true, "Saved draft; sending…");
      } else {
        onSaved(true, "Draft saved to queue.");
      }
    } catch (e) {
      onSaved(false, (e as Error).message);
    }
  };

  return (
    <div className="grid lg:grid-cols-2 gap-6">
      <div className="bg-white/[0.02] border border-white/5 rounded-xl p-6 space-y-4">
        <h2 className="text-lg font-semibold text-white">Content source</h2>
        <div className="flex gap-2">
          {Object.entries(types).map(([k, meta]) => (
            <button
              key={k}
              onClick={() => setContentType(k as "best_pick" | "record_update")}
              className={`px-3 py-1.5 rounded-lg text-sm ${contentType === k ? "bg-white/15 text-white" : "bg-white/5 text-gray-400 hover:text-white"}`}
            >
              {meta.label}
            </button>
          ))}
        </div>
        {activeType.desc && <p className="text-xs text-gray-500">{activeType.desc}</p>}

        <div className="flex items-center gap-2">
          <span className="text-xs text-gray-500">Sport</span>
          <select
            value={sport}
            onChange={(e) => setSport(e.target.value)}
            className="rounded-lg bg-black/40 border border-white/10 px-2 py-1 text-sm text-white"
          >
            <option value="">All</option>
            <option value="mlb">MLB</option>
            <option value="nba">NBA</option>
            <option value="nfl">NFL</option>
          </select>
          <span className="flex-1" />
          <button
            onClick={loadSeeds}
            disabled={busy}
            className="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/15 disabled:opacity-50 text-sm"
          >
            {busy ? "Loading…" : "Fetch fresh picks"}
          </button>
        </div>

        {seeds.length > 0 && (
          <div className="space-y-1 border-t border-white/10 pt-3">
            <span className="text-xs text-gray-500">Generated from live picks — pick one to load:</span>
            {seeds.map((s, i) => (
              <button
                key={i}
                onClick={() => { setSeedIdx(i); setText(s.text); }}
                className={`w-full text-left rounded-lg px-3 py-2 text-sm ${i === seedIdx ? "bg-white/10 text-white" : "bg-white/[0.03] text-gray-300 hover:bg-white/5"}`}
              >
                <span className="font-medium mr-2 text-gray-500">{i + 1}.</span>
                {s.text.slice(0, 150)}{s.text.length > 150 ? "…" : ""}
              </button>
            ))}
          </div>
        )}
        {seeds.length === 0 && !busy && (
          <p className="text-xs text-gray-600">No seeds yet. “Fetch fresh picks” pulls today’s best-EV games / record from our DB.</p>
        )}
      </div>

      <div className="bg-white/[0.02] border border-white/5 rounded-xl p-6 space-y-4">
        <h2 className="text-lg font-semibold text-white">Composer</h2>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          maxLength={280}
          className="w-full rounded-xl bg-black/40 border border-white/10 px-4 py-3 text-white text-sm leading-relaxed focus:outline-none focus:border-blue-500"
          placeholder="Type the post…"
        />
        <div className={`text-right text-xs ${text.length > 260 ? "text-amber-300" : "text-gray-500"}`}>
          {text.length}/280
        </div>
        {seedIdx < seeds.length && (
          <p className="text-xs text-gray-600 break-words">
            Source: <code className="text-gray-400">{JSON.stringify(seeds[seedIdx]?.source_ref || {})}</code> — travels with the draft so we never tweet untraceable picks.
          </p>
        )}
        <div className="flex gap-3 pt-1">
          <button
            onClick={() => saveDraft(false)}
            className="flex-1 px-4 py-2.5 rounded-lg bg-white/10 hover:bg-white/15 text-sm font-medium"
          >
            Save to queue
          </button>
          <button
            onClick={() => saveDraft(true)}
            className="flex-1 px-4 py-2.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-sm font-semibold text-white"
          >
            Save + send now
          </button>
        </div>
        <p className="text-xs text-gray-600">Sending spends metered X credits. A “pick” draft carries its source so the model is accountable.</p>
      </div>
    </div>
  );
}

/* ============================== DRAFTS ============================== */
function DraftsTab({ drafts, onDelete, onStatus }: {
  drafts: Draft[];
  onDelete: (id: number) => void;
  onStatus: (id: number, status: string) => void;
}) {
  if (drafts.length === 0) return <div className="text-gray-500 text-sm p-6">No drafts yet.</div>;
  return (
    <div className="space-y-3">
      {drafts.map((d) => (
        <div key={d.id} className="bg-white/[0.02] border border-white/5 rounded-xl p-5 space-y-3">
          <div className="flex items-center gap-2 text-xs">
            <StatusPill status={d.status} />
            <span className="text-gray-500 uppercase">{d.content_type}</span>
            {d.sport && <span className="text-gray-500">{d.sport.toUpperCase()}</span>}
            <span className="flex-1" />
            <span className="text-gray-600">#{d.id}</span>
          </div>
          <p className="text-sm text-gray-200 whitespace-pre-wrap break-words">{d.text}</p>
          {d.error && <div className="text-xs text-red-300 bg-red-900/10 rounded px-2 py-1 break-words">X: {d.error}</div>}
          <div className="flex items-center gap-2 text-sm pt-1 border-t border-white/5">
            {(d.status === "draft" || d.status === "queued") && (
              <button onClick={() => onStatus(d.id, "approved")} className="px-3 py-1.5 rounded-lg bg-emerald-700/60 hover:bg-emerald-600/70 text-xs">Approve</button>
            )}
            {d.status === "approved" && (
              <button onClick={() => onStatus(d.id, "draft")} className="px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 text-xs">Back to draft</button>
            )}
            {(d.status === "draft" || d.status === "queued" || d.status === "approved") && (
              <button
                onClick={async () => {
                  try {
                    const sent = await authed(() => xFetch<Draft>(`/drafts/${d.id}/send`, { method: "POST", body: JSON.stringify({}) }));
                    if (sent.tweet_id) alert(`Posted to X ✅`);
                    else if (sent.error) alert(`X error: ${sent.error}`);
                  } catch (e) { alert((e as Error).message); }
                }}
                className="px-3 py-1.5 rounded-lg bg-sky-600 hover:bg-sky-500 text-xs font-semibold"
              >
                Send now
              </button>
            )}
            <span className="flex-1" />
            {d.status !== "sent" && (
              <button onClick={() => onStatus(d.id, "discarded")} className="px-3 py-1.5 rounded-lg text-gray-400 hover:text-gray-200 hover:bg-white/5 text-xs">Discard</button>
            )}
            {d.status !== "sent" && (
              <button onClick={() => onDelete(d.id)} className="px-3 py-1.5 rounded-lg text-red-300 hover:text-red-200 hover:bg-red-900/20 text-xs">Delete</button>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ============================== HISTORY ============================== */
function HistoryTab({ posts }: { posts: HistoryPost[] }) {
  if (posts.length === 0) return <div className="text-gray-500 text-sm p-6">Nothing published yet.</div>;
  return (
    <div className="space-y-3">
      {posts.map((p, i) => (
        <div key={p.x_tweet_id || i} className="bg-white/[0.02] border border-white/5 rounded-xl p-5 space-y-2">
          <div className="flex items-center gap-2 text-sm">
            <a
              href={`https://x.com/earlknowsball/status/${p.x_tweet_id}`}
              target="_blank"
              rel="noreferrer"
              className="text-sky-400 hover:text-sky-300"
            >
              View post ↗
            </a>
            {p.created_at && <span className="text-xs text-gray-600">{p.created_at}</span>}
            {p.error && <span className="text-xs text-red-300">err: {p.error}</span>}
          </div>
          <p className="text-sm text-gray-200 whitespace-pre-wrap break-words">{p.text}</p>
        </div>
      ))}
    </div>
  );
}

/* ============================== shared bits ============================== */
function StatusPill({ status }: { status: string }) {
  const colors: Record<string, string> = {
    draft: "bg-gray-700/60 text-gray-200",
    queued: "bg-blue-700/50 text-blue-200",
    approved: "bg-emerald-700/60 text-emerald-200",
    scheduled: "bg-purple-700/50 text-purple-200",
    sent: "bg-sky-700/60 text-sky-200",
    failed: "bg-red-800/60 text-red-200",
    discarded: "bg-zinc-700/40 text-zinc-400",
  };
  return <span className={`rounded px-2 py-0.5 text-2xs uppercase ${colors[status] || colors.draft}`}>{status}</span>;
}

function StatusMsg({ ok, text }: { ok: boolean; text: string }) {
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs ${ok ? "bg-emerald-900/20 border-emerald-700/40 text-emerald-300" : "bg-red-900/20 border-red-800/30 text-red-300"}`}>
      {text}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-gray-500">{label}</dt>
      <dd className="text-gray-200 text-right">{value}</dd>
    </div>
  );
}

// Re-augment module-level authed (defined above with import; kept here for tree clarity).
