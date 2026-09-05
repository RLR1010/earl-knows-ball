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

type Tab = "connect" | "compose" | "drafts" | "history" | "triage" | "following";

interface FollowingUser {
  id: number;
  x_user_id: string;
  username: string;
  name?: string | null;
  description?: string | null;
  snapshot_at?: string | null;
  read_posts: boolean;
  profile_url: string;
}

interface TriagePost {
  id: number;
  tweet_id: string;
  author_username?: string | null;
  text: string;
  created_at?: string | null;
  likes?: number | null;
  retweets?: number | null;
  replies?: number | null;
  suggestion_count?: number;
  responded?: boolean;
}

interface ReplySuggestion {
  id: number;
  post_id?: number | null;
  tweet_id?: string | null;
  author_username?: string | null;
  body: string;
  rationale?: string | null;
  status?: string | null;
  created_at?: string | null;
}

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
  const [triagePosts, setTriagePosts] = useState<TriagePost[]>([]);
  const [pendingSuggestions, setPendingSuggestions] = useState<ReplySuggestion[]>([]);
  const [triageBusyId, setTriageBusyId] = useState<number | null>(null);
  const [listMsgs, setListMsgs] = useState<{ ok: boolean; text: string } | null>(null);
  const [following, setFollowing] = useState<FollowingUser[]>([]);

  const refreshTriage = useCallback(async () => {
    try {
      const [p, s] = await Promise.all([
        authed(() => xFetch<{ posts: TriagePost[] }>("/posts?limit=40")),
        authed(() => xFetch<{ suggestions: ReplySuggestion[] }>("/reply-suggestions?status=pending&limit=50")),
      ]);
      setTriagePosts(p.posts);
      setPendingSuggestions(s.suggestions);
    } catch (e) {
      setListMsgs({ ok: false, text: (e as Error).message });
    }
  }, []);

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

  const refreshFollowing = useCallback(async () => {
    try {
      const f = await authed(() => xFetch<{ following: FollowingUser[] }>("/following"));
      setFollowing(f.following);
    } catch (e) {
      setListMsgs({ ok: false, text: (e as Error).message });
      setFollowing([]);
    }
  }, []);

  useEffect(() => {
    refreshStatus();
    if (tab === "drafts") refreshDrafts();
    if (tab === "history") refreshHistory();
    if (tab === "triage") refreshTriage();
    if (tab === "following") refreshFollowing();
  }, [tab, refreshStatus, refreshDrafts, refreshHistory, refreshTriage, refreshFollowing]);

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

  const toggleFollowRead = async (id: number, readPosts: boolean) => {
    try {
      await authed(() => xFetch<FollowingUser>(`/following/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ read_posts: readPosts }),
      }));
      await refreshFollowing();
    } catch (e) { setMsg(false, (e as Error).message); }
  };

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">X — @earl_knows_ball</h1>
      </div>

      <div className="flex flex-wrap gap-2 border-b border-white/10 pb-3">
        {(
          [
            ["connect", "Connect / Status"],
            ["compose", "Compose"],
            ["drafts", "Drafts"],
            ["history", "Published"],
            ["triage", "Read + Reply"],
            ["following", "Following"],
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
      {tab === "triage" && (
        <TriageTab
          posts={triagePosts}
          suggestions={pendingSuggestions}
          busyId={triageBusyId}
          onRefresh={refreshTriage}
          onBusyChange={setTriageBusyId}
          onMsg={setMsg}
        />
      )}
      {tab === "following" && (
        <FollowingTab users={following} onRefresh={refreshFollowing} onToggle={toggleFollowRead} />
      )}
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
          {/* OAuth2 authorization: grants full read + write (tweet.read/users.read for ingesting
              the feed of accounts WE follow + tweet.write/like.write/follows.write so we can post
              replies & act as @earl_knows_ball). Admin only. Auto-refresh via refresh token. */}
          <button
            onClick={async () => {
              setBusy(true);
              setResMsg(null);
              try {
                const a = await authed(() => xFetch<{ authorize_url: string; state: string; note?: string }>("/oauth/authorize"));
                if (!navigator.clipboard) {
                  setResMsg({ ok: true, text: "Open the authorize link to continue (grants read + write for @earl_knows_ball)." });
                }
                setResMsg({ ok: true, text: "Opening X authorize… Approve @earl_knows_ball then return here." });
                window.open(a.authorize_url, "_blank", "noopener,noreferrer");
              } catch (e) {
                setResMsg({ ok: false, text: (e as Error).message });
              } finally {
                setBusy(false);
              }
            }}
            disabled={busy}
            className="px-3 py-2 rounded-lg bg-emerald-600/20 hover:bg-emerald-600/30 border border-emerald-500/30 text-sm text-emerald-200 disabled:opacity-50"
            title="Authorize @earl_knows_ball on X (OAuth2, full scope: read feed we follow + post/like/follow). Token refreshes automatically."
          >
            Authorize access on X
          </button>
        </div>
        <p className="text-2xs text-gray-600 text-xs mt-2">
          Approving grants Earl full access: read the feed of accounts we follow to find posts worth
          engaging with, AND post replies (“Approve and send”) / like / follow as @earl_knows_ball.
          The token refreshes automatically so access stays active.
        </p>
      </div>

      <div className="bg-white/[0.02] border border-white/5 rounded-xl p-6 space-y-4">
        <h2 className="text-lg font-semibold text-white">Connect @earl_knows_ball</h2>
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
            onSaved(!!sent.tweet_id, sent.error ? `Failed on X: ${sent.error}` : `Posted to X ✅ tweet ${sent.text ? "— check @earl_knows_ball" : ""}`);
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

/* ============================== TRIAGE: READ + REPLY ============================== */
// Uses the 2026-09-02 X pipeline: we ingested recent posts from followed accounts; Earl
// drafts reply suggestions; Rich reviews + approves/rejects here before anything is posted.

function fmtWhen(iso?: string | null): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function xLink(tweetId?: string | null, author?: string | null): string | null {
  return tweetId ? `https://x.com/${author || "earlknowsball"}/status/${tweetId}` : null;
}

function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      return navigator.clipboard.writeText(text).then(() => true, () => false);
    }
  } catch {
    /* fall through to legacy */
  }
  // Legacy fallback for older browsers / non-secure contexts.
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return Promise.resolve(ok);
  } catch {
    return Promise.resolve(false);
  }
}

function CopyBtn({ text, label = "Copy", okText = "Copied ✓", className = "" }: {
  text: string;
  label?: string;
  okText?: string;
  className?: string;
}) {
  const [done, setDone] = useState(false);
  return (
    <button
      type="button"
      onClick={async () => {
        const ok = await copyText(text);
        if (ok) {
          setDone(true);
          window.setTimeout(() => setDone(false), 1600);
        }
      }}
      className={`px-3 py-1 rounded-md text-xs border transition-colors disabled:opacity-50 ${done ? "bg-emerald-700/60 border-emerald-600/50 text-emerald-100" : "bg-transparent border-white/15 text-gray-300 hover:bg-white/5 hover:text-gray-100"} ${className}`}
      title="Copy this reply text to paste into X"
    >
      {done ? okText : label}
    </button>
  );
}

function TriageTab({ posts, suggestions, busyId, onRefresh, onBusyChange, onMsg }: {
  posts: TriagePost[];
  suggestions: ReplySuggestion[];
  busyId: number | null;
  onRefresh: () => void;
  onBusyChange: (id: number | null) => void;
  onMsg: (ok: boolean, text: string) => void;
}) {
  const [authorFilter, setAuthorFilter] = useState<string>("");

  const draftReplies = async (post: TriagePost) => {
    onBusyChange(post.id);
    try {
      const res = await authed(() => xFetch<{ count: number }>(`/posts/${post.id}/draft-reply?n_options=3`, { method: "POST" }));
      onMsg(true, `Earl drafted ${res.count} reply suggestion(s) for @${post.author_username || ""}. Review below →`);
      onRefresh();
    } catch (e) {
      onMsg(false, (e as Error).message);
    } finally {
      onBusyChange(null);
    }
  };

  const setSuggestionStatus = async (s: ReplySuggestion, status: "approved" | "rejected") => {
    try {
      await authed(() => xFetch(`/reply-suggestions/${s.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      }));
      onMsg(true, status === "approved" ? "Approved (manual) — post it yourself in the X app." : "Rejected.");
      onRefresh();
    } catch (e) {
      onMsg(false, (e as Error).message);
    }
  };

  const approveAndSend = async (s: ReplySuggestion) => {
    onBusyChange(s.id);
    try {
      const res = await authed(() => xFetch<{ posted: boolean; posted_tweet_id?: string | null; status: string }>(`/reply-suggestions/${s.id}/send`, { method: "POST" }));
      onMsg(true, res.posted
        ? `Posted reply on X${res.posted_tweet_id ? ` — tweet ${res.posted_tweet_id}` : ""}.`
        : "Marked approved; nothing was sent.");
      onRefresh();
    } catch (e) {
      onMsg(false, (e as Error).message);
      onRefresh();
    } finally {
      onBusyChange(null);
    }
  };

  const authors = Array.from(new Set((posts || []).map((p) => p.author_username).filter(Boolean))) as string[];
  const filtered = authors.length && authorFilter
    ? (posts || []).filter((p) => p.author_username === authorFilter)
    : posts || [];

  return (
    <div className="space-y-8">
      {/* Pending Earl drafts - the actionable queue */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-white">Reply drafts to review ({suggestions.length})</h2>
          <button onClick={onRefresh} className="text-xs text-gray-400 hover:text-white underline">refresh</button>
        </div>
        {suggestions.length === 0 ? (
          <p className="text-sm text-gray-500">No pending reply drafts. Pick a post below and hit “Draft replies”.</p>
        ) : (
          <div className="space-y-3">
            {suggestions.map((s) => {
              const link = xLink(s.tweet_id, s.author_username);
              return (
                <div key={s.id} className="bg-white/[0.02] border border-white/5 rounded-xl p-4 space-y-2">
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-2 text-xs text-gray-500">
                      {s.author_username && <span className="text-sky-400">@{s.author_username}</span>}
                      {s.created_at && <span>{fmtWhen(s.created_at)}</span>}
                      {link && (
                        <a href={link} target="_blank" rel="noreferrer" className="text-gray-500 hover:text-sky-300">
                          tweet ↗
                        </a>
                      )}
                    </div>
                    <StatusPill status={s.status || "draft"} />
                  </div>
                  <p className="text-sm text-gray-100 whitespace-pre-wrap break-words">{s.body}</p>
                  {s.rationale && <p className="text-xs text-gray-500 italic">why: {s.rationale}</p>}
                  <div className="flex gap-2 pt-1 flex-wrap items-center">
                    <a
                      href={link || "#"}
                      target="_blank"
                      rel="noreferrer"
                      className="px-3 py-1 rounded-md text-xs font-medium bg-sky-900/50 hover:bg-sky-800/70 text-sky-200 border border-sky-700/40 disabled:opacity-40"
                      title="Open this tweet on X so you can reply/fire it yourself."
                    >
                      Open on X ↗
                    </a>
                    <CopyBtn text={s.body || ""} />
                    <button
                      onClick={() => approveAndSend(s)}
                      disabled={busyId === s.id}
                      className="px-3 py-1 rounded-md text-xs font-semibold bg-emerald-600 hover:bg-emerald-500 text-white disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      {busyId === s.id ? "Posting…" : "Approve and send"}
                    </button>
                    <button
                      onClick={() => setSuggestionStatus(s, "approved")}
                      disabled={busyId === s.id}
                      className="px-3 py-1 rounded-md text-xs font-medium bg-emerald-900/40 hover:bg-emerald-800/60 text-emerald-200 border border-emerald-700/30 disabled:opacity-40 disabled:cursor-not-allowed"
                      title="Mark approved — you publish it yourself through the X app."
                    >
                      Approve – manual
                    </button>
                    <button
                      onClick={() => setSuggestionStatus(s, "rejected")}
                      disabled={busyId === s.id}
                      className="px-3 py-1 rounded-md text-xs bg-red-900/50 hover:bg-red-800 text-red-200 disabled:opacity-40 disabled:cursor-not-allowed"
                    >
                      Reject
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* Ingested posts to pick from */}
      <section>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-lg font-semibold text-white">Recent posts from accounts we follow ({filtered.length})</h2>
          <select
            value={authorFilter}
            onChange={(e) => setAuthorFilter(e.target.value)}
            className="bg-zinc-900 border border-white/10 rounded-md px-2 py-1 text-xs text-gray-300"
          >
            <option value="">All accounts</option>
            {authors.map((a) => <option key={a} value={a}>@{a}</option>)}
          </select>
        </div>
        {filtered.length === 0 ? (
          <p className="text-sm text-gray-500">No posts ingested yet — run the X reader to pull recent posts.</p>
        ) : (
          <div className="space-y-3">
            {filtered.map((p) => {
              const link = xLink(p.tweet_id, p.author_username);
              const busy = busyId === p.id;
              return (
                <div key={p.id} className="bg-white/[0.02] border border-white/5 rounded-xl p-4 space-y-2">
                  <div className="flex items-center justify-between gap-3">
                    <div className="flex items-center gap-2 text-xs text-gray-500">
                      {p.author_username && <span className="text-sky-400">@{p.author_username}</span>}
                      {p.created_at && <span>{fmtWhen(p.created_at)}</span>}
                      {p.likes != null && <span>♥ {p.likes}</span>}
                      {link && <a href={link} target="_blank" rel="noreferrer" className="hover:text-sky-300">tweet ↗</a>}
                    </div>
                    {p.responded ? (
                      <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-700/40 text-emerald-300 border border-emerald-700/40">✓ responded</span>
                    ) : p.suggestion_count ? (
                      <span className="text-xs text-amber-300">{p.suggestion_count} pending draft(s)</span>
                    ) : null}
                  </div>
                  <p className="text-sm text-gray-200 whitespace-pre-wrap break-words">{p.text}</p>
                  <button
                    onClick={() => draftReplies(p)}
                    disabled={busy}
                    className="px-3 py-1.5 rounded-lg text-xs font-medium bg-sky-700/60 hover:bg-sky-600 disabled:opacity-50 text-sky-100"
                  >
                    {busy ? "Drafting…" : "Draft replies"}
                  </button>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

/* ============================== FOLLOWING ============================== */
function FollowingTab({ users, onRefresh, onToggle }: {
  users: FollowingUser[];
  onRefresh: () => void;
  onToggle: (id: number, readPosts: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const q = query.trim().toLowerCase();
  const filtered = q
    ? users.filter(
        (u) =>
          u.username.toLowerCase().includes(q) ||
          u.name?.toLowerCase().includes(q) ||
          u.description?.toLowerCase().includes(q),
      )
    : users;

  const collected = users.filter((u) => u.read_posts).length;

  const toggle = async (u: FollowingUser) => {
    setBusyKey(String(u.id));
    try {
      await onToggle(u.id, !u.read_posts);
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <div className="space-y-6">
      <section>
        <div className="flex items-center justify-between mb-3 gap-3 flex-wrap">
          <div>
            <h2 className="text-lg font-semibold text-white">Accounts we follow ({users.length})</h2>
            <p className="text-xs text-gray-500">
              {collected} of {users.length} have tweet collection ON. Toggle a row to choose
              whose tweets Earl reads into the feed.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search username or name…"
              className="bg-white/5 border border-white/10 rounded-lg px-3 py-1.5 text-sm text-white placeholder:text-gray-500 focus:outline-none focus:border-white/30"
            />
            <button onClick={onRefresh} className="text-xs text-gray-400 hover:text-white underline whitespace-nowrap">
              refresh
            </button>
          </div>
        </div>

        {filtered.length === 0 ? (
          <p className="text-sm text-gray-500">
            {q ? "No accounts match your search." : "No followed accounts yet — run the X following sync."}
          </p>
        ) : (
          <div className="space-y-2">
            {filtered.map((u) => {
              const busy = busyKey === String(u.id);
              return (
                <div
                  key={u.id}
                  className="bg-white/[0.02] border border-white/5 rounded-xl px-4 py-3 flex items-center gap-4"
                >
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <a
                        href={u.profile_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-sm font-semibold text-emerald-300 hover:text-emerald-200 hover:underline"
                      >
                        @{u.username}
                      </a>
                      {u.name ? (
                        <span className="text-sm text-gray-400 truncate">{u.name}</span>
                      ) : null}
                    </div>
                    {u.description ? (
                      <p className="text-xs text-gray-500 mt-0.5 line-clamp-2">{u.description}</p>
                    ) : null}
                    {u.snapshot_at ? (
                      <p className="text-xs text-gray-600 mt-0.5">Snapshot {fmtWhen(u.snapshot_at)}</p>
                    ) : null}
                  </div>

                  {/* link out to profile */}
                  <a
                    href={u.profile_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-gray-400 hover:text-white bg-white/5 hover:bg-white/10 rounded-lg px-3 py-1.5 whitespace-nowrap"
                  >
                    Profile ↗
                  </a>

                  {/* collect toggle */}
                  <button
                    onClick={() => toggle(u)}
                    disabled={busy}
                    className={`relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors focus:outline-none ${
                      u.read_posts ? "bg-emerald-600" : "bg-gray-600"
                    } ${busy ? "opacity-50" : ""}`}
                    role="switch"
                    aria-checked={u.read_posts}
                    title={u.read_posts ? "Collecting tweets — click to stop" : "Not collecting — click to start"}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                        u.read_posts ? "translate-x-6" : "translate-x-1"
                      }`}
                    />
                  </button>
                  <span className={`text-xs w-16 whitespace-nowrap ${u.read_posts ? "text-emerald-300" : "text-gray-500"}`}>
                    {u.read_posts ? "Collect" : "Paused"}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
