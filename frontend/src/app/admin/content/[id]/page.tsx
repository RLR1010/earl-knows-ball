"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useSeo } from "@/components/Seo";

/* ─────────────────────────────────────────────
   Types
   ───────────────────────────────────────────── */

interface Writeup {
  id: number;
  game_id: number;
  title: string;
  content: string;
  matchup: string;
  status: string;
  version: number;
  is_historical: boolean;
  generated_by: string;
  published_at: string | null;
  created_at: string | null;
  game_date: string | null;
  // When editing we need both content versions
  public_content: string;
  premium_content: string;
  quality_checks: any;
  research_brief: any;
  total_tokens: number | null;
  accuracy_check: any;
  accuracy_check_tokens: number | null;
}

interface QCResult {
  check: string;
  passed: boolean;
  detail: string;
}

/* Renders the stored accuracy-check result for a write-up. */
function AccuracyCheckView({ data }: { data: any }) {
  if (!data || typeof data !== "object" || Object.keys(data).length === 0) {
    return (
      <div className="text-xs text-gray-500">
        No accuracy check result stored yet.
      </div>
    );
  }
  const passed = data.passed;
  const skipped = !!data.skipped;
  const findings = Array.isArray(data.findings) ? data.findings : [];
  let badge: {
    label: string;
    cls: string;
  };
  if (skipped) {
    badge = { label: "Skipped (no response)", cls: "bg-yellow-500/15 text-yellow-400" };
  } else if (passed) {
    badge = { label: "Passed", cls: "bg-green-500/15 text-green-400" };
  } else {
    badge = { label: "Failed", cls: "bg-red-500/15 text-red-400" };
  }
  return (
    <div className="space-y-2 text-sm">
      <div className="flex items-center gap-3">
        <span
          className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${badge.cls}`}
        >
          {badge.label}
        </span>
        {typeof data.tokens === "number" && (
          <span className="text-xs text-gray-500">
            {data.tokens.toLocaleString()} tokens
          </span>
        )}
      </div>

      {data.error && (
        <div className="text-xs text-red-400/80">{data.error}</div>
      )}

      {findings.length > 0 ? (
        <ul className="space-y-1 list-disc list-inside">
          {findings.map((f: any, i: number) => (
            <li key={i} className="text-xs text-gray-400">
              {typeof f === "string" ? f : JSON.stringify(f)}
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-xs text-gray-500">
          {skipped ? "DeepSeek returned no response, so no findings were evaluated." : "No findings."}
        </div>
      )}

      {data.reviewed && (
        <div className="text-xs text-gray-500">Marked as reviewed manually.</div>
      )}
      {data.retries_used != null && (
        <div className="text-xs text-gray-500">
          Retries used: {data.retries_used}
        </div>
      )}
    </div>
  );
}

interface RejectedDraft {
  attempt?: number;
  timestamp?: string;
  accuracy_check?: any;
  public_content?: string;
  premium_content?: string;
}

function DraftMiniView({ label, content }: { label: string; content?: string }) {
  const [open, setOpen] = useState(false);
  const txt = content || "(empty)";
  const preview = txt.length > 400 ? txt.slice(0, 400) + "…" : txt;
  return (
    <div className="text-xs">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300"
      >
        {open ? "▾" : "▸"} {label}
      </button>
      {open && (
        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded bg-black/40 border border-gray-700 p-2 text-[11px] leading-snug text-gray-300">
          {txt}
        </pre>
      )}
    </div>
  );
}

function RejectedDraftsView({ drafts }: { drafts: RejectedDraft[] }) {
  if (!drafts || drafts.length === 0) {
    return (
      <div className="text-xs text-gray-500">No rejected drafts recorded.</div>
    );
  }
  return (
    <div className="space-y-3">
      {drafts.map((d, idx) => {
        const findings = Array.isArray(d.accuracy_check?.findings)
          ? d.accuracy_check.findings
          : [];
        const passed = d.accuracy_check?.passed;
        return (
          <div
            key={idx}
            className="rounded border border-red-500/25 bg-red-500/5 p-3 space-y-2"
          >
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-medium text-red-400">
                Attempt {d.attempt ?? idx + 1}
              </span>
              {d.timestamp && (
                <span className="text-gray-500">
                  {new Date(d.timestamp).toLocaleString()}
                </span>
              )}
              <span
                className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                  passed
                    ? "bg-green-500/15 text-green-400"
                    : "bg-red-500/15 text-red-400"
                }`}
              >
                accuracy {passed ? "passed" : "failed"}
              </span>
              {typeof d.accuracy_check?.tokens === "number" && (
                <span className="text-gray-500">
                  {(d.accuracy_check.tokens as number).toLocaleString()} tokens
                </span>
              )}
            </div>

            {findings.length > 0 ? (
              <ul className="list-disc list-inside space-y-1 text-xs text-gray-300">
                {findings.map((f: any, i: number) => (
                  <li key={i}>
                    {typeof f === "string" ? f : JSON.stringify(f)}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="text-xs text-gray-500">No detailed findings.</div>
            )}

            <div className="grid grid-cols-1 gap-2">
              <DraftMiniView label="Public draft" content={d.public_content} />
              <DraftMiniView label="Premium draft" content={d.premium_content} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ─────────────────────────────────────────────
   Helpers
   ───────────────────────────────────────────── */

const token = () => localStorage.getItem("earl_token");

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-yellow-500/20 text-yellow-400 border border-yellow-500/30",
  review: "bg-blue-500/20 text-blue-400 border border-blue-500/30",
  published: "bg-green-500/20 text-green-400 border border-green-500/30",
  archived: "bg-gray-500/20 text-gray-400 border border-gray-500/30",
};

const STATUS_LABELS: Record<string, string> = {
  draft: "Draft",
  review: "Awaiting Review",
  published: "Published",
  archived: "Archived",
};

/* ─────────────────────────────────────────────
   Usage Log (per-call token & cost breakdown)
   ─────────────────────────────────────────────
   DeepSeek V4 Flash pricing (per 1M tokens USD).
   Update these if the model or price changes.
*/
const DEEPSEEK_PRICES = {
  inputCacheHit: 0.0028, // cached input
  inputCacheMiss: 0.14, // uncached input
  output: 0.28, // completion / output
};

function fmtTokens(n: number | null | undefined): string {
  return n ? n.toLocaleString() : "0";
}

function fmtUSD(cents: number): string {
  if (cents >= 100) return "$" + (cents / 100).toFixed(2);
  return (cents / 100).toFixed(2) + "¢";
}

function UsageLogView({ log }: { log: any[] | null | undefined }) {
  const [open, setOpen] = useState(false);
  const calls = Array.isArray(log) ? log : [];

  const totals = calls.reduce(
    (acc, e) => {
      acc.prompt += e.prompt_tokens || 0;
      acc.hit += e.prompt_cache_hit_tokens || 0;
      acc.miss += e.prompt_cache_miss_tokens || 0;
      acc.completion += e.completion_tokens || 0;
      acc.reasoning += e.reasoning_tokens || 0;
      acc.total += e.total_tokens || 0;
      return acc;
    },
    { prompt: 0, hit: 0, miss: 0, completion: 0, reasoning: 0, total: 0 }
  );

  const costUSD =
    (totals.hit * DEEPSEEK_PRICES.inputCacheHit +
      totals.miss * DEEPSEEK_PRICES.inputCacheMiss +
      totals.completion * DEEPSEEK_PRICES.output) /
    1_000_000;

  const CALL_LABELS: Record<string, string> = {
    public_write: "Public writeup",
    premium_write: "Premium writeup",
    accuracy_public: "Accuracy check (public)",
    accuracy_premium: "Accuracy check (premium)",
    correction: "Correction rewrite",
    seo: "SEO meta",
    generate: "Generate",
  };

  return (
    <div className="mt-6 bg-white/[0.03] border border-blue-500/20 rounded-xl p-4">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between text-left"
      >
        <h3 className="text-sm font-medium text-gray-400">
          Usage Log
          <span className="ml-2 text-xs text-gray-500 font-normal">
            ({calls.length} call{calls.length === 1 ? "" : "s"})
          </span>
        </h3>
        <span className="text-xs font-mono text-emerald-400">
          ~{fmtUSD(costUSD * 100)} est
        </span>
      </button>

      <p className="mt-1 text-xs text-gray-500">
        Total tokens: {fmtTokens(totals.total)} · cached input:{" "}
        {fmtTokens(totals.hit)} · uncached input: {fmtTokens(totals.miss)} ·
        output: {fmtTokens(totals.completion)} · reasoning:{" "}
        {fmtTokens(totals.reasoning)}
      </p>

      {open && (
        <div className="mt-3 overflow-x-auto">
          {calls.length === 0 ? (
            <p className="text-xs text-gray-500">
              No per-call usage recorded. This run predates the usage-log
              capture (or it wasn't persisted).
            </p>
          ) : (
            <table className="w-full text-xs text-gray-300">
              <thead>
                <tr className="text-left text-gray-500 uppercase tracking-wider border-b border-white/10">
                  <th className="py-1.5 pr-3">Call</th>
                  <th className="py-1.5 pr-3">#</th>
                  <th className="py-1.5 pr-3">Cached in</th>
                  <th className="py-1.5 pr-3">Miss in</th>
                  <th className="py-1.5 pr-3">Output</th>
                  <th className="py-1.5 pr-3">Reasoning</th>
                  <th className="py-1.5 pr-3">Total</th>
                  <th className="py-1.5">Est cost</th>
                </tr>
              </thead>
              <tbody>
                {calls.map((e: any, i: number) => {
                  const c =
                    (e.prompt_cache_hit_tokens || 0) *
                      DEEPSEEK_PRICES.inputCacheHit +
                    (e.prompt_cache_miss_tokens || 0) *
                      DEEPSEEK_PRICES.inputCacheMiss +
                    (e.completion_tokens || 0) * DEEPSEEK_PRICES.output;
                  return (
                    <tr
                      key={i}
                      className="border-b border-white/5 align-top"
                    >
                      <td className="py-1.5 pr-3 whitespace-nowrap">
                        {CALL_LABELS[e.call] || e.call || "—"}
                      </td>
                      <td className="py-1.5 pr-3">{e.attempt ?? ""}</td>
                      <td className="py-1.5 pr-3 font-mono">
                        {fmtTokens(e.prompt_cache_hit_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 font-mono">
                        {fmtTokens(e.prompt_cache_miss_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 font-mono">
                        {fmtTokens(e.completion_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 font-mono">
                        {fmtTokens(e.reasoning_tokens)}
                      </td>
                      <td className="py-1.5 pr-3 font-mono">
                        {fmtTokens(e.total_tokens)}
                      </td>
                      <td className="py-1.5 font-mono text-emerald-400/80">
                        {fmtUSD(c)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────
   Component
   ───────────────────────────────────────────── */

export default function ContentEditor() {
  useSeo({ title: "Content Editor — Admin — Earl Knows Ball" });
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const writeupId = params.id as string;
  const sport = searchParams.get("sport") || "mlb";
  const days = searchParams.get("days");
  // Return to the content listing at the same sport tab + day offset we left from.
  const backToContent =
    "/admin/content?sport=" +
    sport +
    (days && Number.isFinite(Number(days)) ? `&days=${days}` : "");

  const [writeup, setWriteup] = useState<Writeup | null>(null);
  const [publicContent, setPublicContent] = useState("");
  const [premiumContent, setPremiumContent] = useState("");
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [qcResults, setQcResults] = useState<QCResult[]>([]);
  const [activeTab, setActiveTab] = useState<"public" | "premium">("public");
  const [propTitle, setPropTitle] = useState<string | null>(null);
  const [propContent, setPropContent] = useState<string | null>(null);
  const [showProps, setShowProps] = useState(false);
  const [showQc, setShowQc] = useState(false);
  const [showResearch, setShowResearch] = useState(false);
  const [researchBrief, setResearchBrief] = useState<any>(null);
  const [totalTokens, setTotalTokens] = useState<number | null>(null);
  const [accuracyCheck, setAccuracyCheck] = useState<any>(null);
  const [accuracyCheckTokens, setAccuracyCheckTokens] = useState<number | null>(null);
  const [rejectionHistory, setRejectionHistory] = useState<RejectedDraft[]>([]);
  const [usageLog, setUsageLog] = useState<any[]>([]);

  // ── Fetch write-up ────────────────────────────

  const fetchWriteup = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/writeups/${sport}/${writeupId}?tier=premium`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`Failed to load: ${res.status}`);
      const data = await res.json();
      setWriteup(data);
      setTitle(data.title || "");

      // Extract quality checks and research brief
      if (data.quality_checks) {
        if (Array.isArray(data.quality_checks)) {
          setQcResults(data.quality_checks);
        } else if (typeof data.quality_checks === "object") {
          setQcResults(data.quality_checks.results || []);
        }
      }
      if (data.research_brief) {
        setResearchBrief(data.research_brief);
        // Per-call usage log is nested inside the persisted research_brief
        const usage = (data.research_brief as any)?._usage_log;
        setUsageLog(Array.isArray(usage) ? usage : []);
      }
      if (data.total_tokens != null) setTotalTokens(data.total_tokens);
      setPropTitle(data.prop_title || null);
      setPropContent(data.prop_content || null);
      if (data.accuracy_check != null) setAccuracyCheck(data.accuracy_check);
      if (data.accuracy_check_tokens != null) setAccuracyCheckTokens(data.accuracy_check_tokens);
      if (Array.isArray(data.rejection_history)) setRejectionHistory(data.rejection_history);
      else setRejectionHistory([]);

      // We need both versions — fetch with tier=public as well
      const pubRes = await fetch(`/api/writeups/${sport}/${writeupId}?tier=public`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (pubRes.ok) {
        const pubData = await pubRes.json();
        setPublicContent(pubData.content || "");
      } else {
        setPublicContent(data.content || "");
      }
      setPremiumContent(data.content || "");
    } catch (e: any) {
      console.error("fetchWriteup error:", e);
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [writeupId, sport]);

  useEffect(() => {
    fetchWriteup();
  }, [fetchWriteup]);

  // ── Save ───────────────────────────────────────

  const handleSave = async () => {
    setSaving(true);
    try {
      // Update title via backend
      // For now just save content locally
      const res = await fetch(`/api/writeups/${sport}/${writeupId}?tier=premium`, {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${token()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          title,
          public_content: publicContent,
          premium_content: premiumContent,
        }),
      });
      if (!res.ok) throw new Error(`Save failed: ${res.status}`);
      await fetchWriteup();
    } catch (e: any) {
      alert(`Save failed: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  // ── Change status ──────────────────────────────

  const handleStatusChange = async (newStatus: string) => {
    try {
      const res = await fetch(
        `/api/writeups/${sport}/${writeupId}/status?status=${newStatus}`,
        {
          method: "PATCH",
          headers: { Authorization: `Bearer ${token()}` },
        }
      );
      if (!res.ok) throw new Error(`Status update failed: ${res.status}`);
      await fetchWriteup();
    } catch (e: any) {
      alert(`Status update failed: ${e.message}`);
    }
  };

  // ── Regenerate ─────────────────────────────────

  const handleRegenerate = async () => {
    if (!writeup) return;
    if (!confirm("Regenerate this write-up? Current content will be versioned."))
      return;

    try {
      // Call backend directly to avoid proxy timeout
      // Full write-up regeneration runs a research loop + enrichment and can take
      // ~4-5 min. Give it generous headroom (420s) past the ~4.5 min typical run.
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 420_000);
      const res = await fetch(
        `/writeups/${sport}/generate/${writeup.game_id}`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
          signal: controller.signal,
        }
      );
      clearTimeout(timeout);

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(errText);
      }
      await fetchWriteup();
    } catch (e: any) {
      if (e.name === "AbortError") {
        alert("Regeneration timed out after 7 minutes. It may still be running in the background — refresh in a bit.");
      } else {
        alert(`Regeneration failed: ${e.message}`);
      }
    }
  };

  // ── Render ─────────────────────────────────────

  if (loading) {
    return (
      <div className="text-center py-20">
        <div className="text-gray-400 text-lg animate-pulse">
          Loading write-up...
        </div>
      </div>
    );
  }

  if (error || !writeup) {
    return (
      <div className="text-center py-20">
        <div className="text-red-400 text-lg mb-2">Failed to load</div>
        <div className="text-gray-500 text-sm">{error || "Not found"}</div>
        <button
          onClick={() => router.push(backToContent)}
          className="mt-4 px-4 py-2 text-sm rounded-lg bg-white/[0.03] border border-white/10 text-gray-400 hover:text-white transition"
        >
          ← Back
        </button>
      </div>
    );
  }

  const qcSummary = writeup.status === "review" || writeup.status === "published" ? (
    <span className="text-green-400 text-xs">QC Passed</span>
  ) : null;

  return (
    <div>
      {/* ── Header ──────────────────────────────── */}
      <div className="flex items-start justify-between mb-6">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-3 mb-2">
            <button
              onClick={() => router.push(backToContent)}
              className="text-gray-500 hover:text-white transition"
            >
              ← Content
            </button>
            <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[writeup.status] || STATUS_COLORS.draft}`}>
              {STATUS_LABELS[writeup.status] || writeup.status}
            </span>
            {qcSummary}
            {writeup.is_historical && (
              <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-purple-500/20 text-purple-400 border border-purple-500/30">
                Historical
              </span>
            )}
          </div>
          <h1 className="text-xl font-bold text-white">{title || writeup.title}</h1>
          <div className="text-sm text-gray-500 mt-1">
            {writeup.matchup} &middot; v{writeup.version} &middot; {writeup.generated_by}
          </div>
        </div>

        <div className="flex items-center gap-2 ml-4 flex-shrink-0">
          {writeup.status !== "published" && (
            <button
              onClick={() => handleStatusChange("published")}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-green-600/20 text-green-400 border border-green-600/30 hover:bg-green-600/30 transition"
            >
              Publish
            </button>
          )}
          {writeup.status !== "review" && (
            <button
              onClick={() => handleStatusChange("review")}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-blue-600/20 text-blue-400 border border-blue-600/30 hover:bg-blue-600/30 transition"
            >
              Submit for Review
            </button>
          )}
          {writeup.status === "published" && (
            <button
              onClick={() => handleStatusChange("archived")}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-gray-600/20 text-gray-400 border border-gray-600/30 hover:bg-gray-600/30 transition"
            >
              Archive
            </button>
          )}
          <button
            onClick={handleRegenerate}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-orange-600/20 text-orange-400 border border-orange-600/30 hover:bg-orange-600/30 transition"
          >
            Regenerate
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-earl-600/20 text-earl-400 border border-earl-600/30 hover:bg-earl-600/30 transition disabled:opacity-50"
          >
            {saving ? "Saving..." : "Save"}
          </button>
        </div>
      </div>

      {/* ── Title editor ────────────────────────── */}
      <div className="mb-4">
        <label className="block text-xs text-gray-500 uppercase tracking-wider mb-1">
          Title
        </label>
        <input
          type="text"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full px-4 py-2 text-sm bg-white/[0.03] border border-white/10 rounded-lg text-white placeholder-gray-500 focus:outline-none focus:border-earl-500/50"
          placeholder="Game title..."
        />
      </div>

      {/* ── Tab switcher ────────────────────────── */}
      <div className="flex items-center gap-2 mb-4">
        <button
          onClick={() => setActiveTab("public")}
          className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
            activeTab === "public"
              ? "bg-earl-600/20 text-earl-400 border-earl-600/30"
              : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
          }`}
        >
          Public
        </button>
        <button
          onClick={() => setActiveTab("premium")}
          className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
            activeTab === "premium"
              ? "bg-earl-600/20 text-earl-400 border-earl-600/30"
              : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
          }`}
        >
          Premium
        </button>
        <button
          onClick={() => setShowQc(!showQc)}
          className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
            showQc
              ? "bg-blue-600/20 text-blue-400 border-blue-600/30"
              : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
          }`}
        >
          Quality Checks
        </button>
        <button
          onClick={() => setShowResearch(!showResearch)}
          className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
            showResearch
              ? "bg-purple-600/20 text-purple-400 border-purple-600/30"
              : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
          }`}
        >
          Research Context
        </button>
        {(propTitle || propContent) && (
          <button
            onClick={() => setShowProps(!showProps)}
            className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
              showProps
                ? "bg-amber-600/20 text-amber-400 border-amber-600/30"
                : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
            }`}
          >
            ⚖ Prop Bets
          </button>
        )}
      </div>

      {/* ── Editor pane ─────────────────────────── */}
      <div className="grid grid-cols-1 gap-4">
        <div>
          <div className="bg-white/[0.03] border border-white/10 rounded-xl overflow-hidden">
            <div className="border-b border-white/10 px-4 py-2 flex items-center justify-between">
              <span className="text-xs font-medium text-gray-400 uppercase tracking-wider">
                {activeTab === "public" ? "Public Version (no picks)" : "Premium Version (with picks)"}
              </span>
              <span className="text-xs text-gray-600">
                {activeTab === "public"
                  ? publicContent.split(/\s+/).length + " words"
                  : premiumContent.split(/\s+/).length + " words"}
              </span>
            </div>
            <textarea
              value={activeTab === "public" ? publicContent : premiumContent}
              onChange={(e) => {
                if (activeTab === "public") setPublicContent(e.target.value);
                else setPremiumContent(e.target.value);
              }}
              className="w-full h-[60vh] p-4 text-sm text-gray-300 bg-transparent resize-none focus:outline-none font-mono leading-relaxed"
              placeholder="Write-up content..."
            />
          </div>
        </div>
      </div>

      {/* ── Prop Bets article ──────────────────── */}
      {showProps && (
        <div className="mt-6 bg-white/[0.03] border border-amber-600/20 rounded-xl overflow-hidden">
          <div className="border-b border-amber-600/20 px-4 py-2 flex items-center justify-between">
            <span className="text-sm font-medium text-amber-400">
              ⚖ {propTitle || "Prop Bets"}
            </span>
            <span className="text-xs text-gray-500">
              {(propContent || "").split(/\s+/).length + " words"} · premium
            </span>
          </div>
          <div className="p-4">
            <textarea
              readOnly
              value={propContent || ""}
              className="w-full h-[40vh] p-4 text-sm text-gray-300 bg-transparent resize-none focus:outline-none font-mono leading-relaxed"
              placeholder="No prop article generated for this game."
            />
          </div>
        </div>
      )}

      {/* ── Quality checks ──────────────────────── */}
      {showQc && (
        <div className="mt-6 bg-white/[0.03] border border-white/10 rounded-xl p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            Quality Check Results
          </h3>
          <div className="space-y-2">
            {qcResults.length === 0 ? (
              <p className="text-xs text-gray-500">
                No quality check results available. To run checks, regenerate the write-up.
              </p>
            ) : (
              qcResults.map((qc, i) => (
                <div
                  key={i}
                  className={`flex items-start gap-3 p-2 rounded-lg ${
                    qc.passed ? "bg-green-500/5" : "bg-red-500/5"
                  }`}
                >
                  <span className={qc.passed ? "text-green-400" : "text-red-400"}>
                    {qc.passed ? "✓" : "✗"}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div className="text-sm text-gray-300">{qc.check}</div>
                    <div className="text-xs text-gray-500 mt-0.5">
                      {qc.detail}
                    </div>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {/* ── Tokens & Accuracy Check ────────────────── */}
      {(totalTokens != null || accuracyCheckTokens != null || accuracyCheck != null) && (
        <div className="mt-6 bg-white/[0.03] border border-amber-500/20 rounded-xl p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            Tokens & Accuracy Check
          </h3>
          <div className="space-y-3">
            {(totalTokens != null || accuracyCheckTokens != null) && (
              <div className="flex gap-6 text-sm">
                {totalTokens != null && (
                  <div>
                    <span className="text-gray-500">Total tokens: </span>
                    <span className="text-gray-200 font-medium">{totalTokens.toLocaleString()}</span>
                  </div>
                )}
                {accuracyCheckTokens != null && (
                  <div>
                    <span className="text-gray-500">Accuracy check tokens: </span>
                    <span className="text-gray-200 font-medium">{accuracyCheckTokens.toLocaleString()}</span>
                  </div>
                )}
              </div>
            )}

            <UsageLogView log={usageLog} />

            <AccuracyCheckView data={accuracyCheck} />
          </div>
        </div>
      )}

      {/* ── Rejected drafts (accuracy-failed history) ── */}
      {rejectionHistory.length > 0 && (
        <div className="mt-6 bg-white/[0.03] border border-red-500/25 rounded-xl p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            Rejected Drafts
            <span className="ml-2 text-xs text-red-400">
              ({rejectionHistory.length})
            </span>
            <span className="ml-2 text-xs text-gray-500 font-normal">
              — drafts flagged by the accuracy check before correction
            </span>
          </h3>
          <RejectedDraftsView drafts={rejectionHistory} />
        </div>
      )}

      {/* ── Research Context ──────────────────── */}
      {showResearch && (
        <div className="mt-6 bg-white/[0.03] border border-purple-600/20 rounded-xl p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-3">
            Research Context (enrich_writeup_context)
          </h3>
          {!researchBrief ? (
            <p className="text-xs text-gray-500">
              No research context available. Generate the write-up first to populate this data.
            </p>
          ) : (
          <div className="space-y-4">
            {/* Article Enrichment */}
            {researchBrief.article_enrichment && (
              <div>
                <h4 className="text-xs font-medium text-purple-400 mb-2 uppercase tracking-wider">
                  Article Enrichment
                </h4>
                <div className="bg-black/20 rounded-lg p-3 space-y-2">
                  <div className="flex items-center gap-4 text-xs text-gray-400">
                    <span>Articles found: <strong className="text-gray-300">{researchBrief.article_enrichment.article_count ?? "?"}</strong></span>
                    <span>Queries: <strong className="text-gray-300">{(researchBrief.article_enrichment.search_queries ?? []).length}</strong></span>
                  </div>
                  {researchBrief.article_enrichment.search_queries?.length > 0 && (
                    <div>
                      <span className="text-xs text-gray-500">Search queries:</span>
                      <ul className="mt-1 space-y-0.5">
                        {researchBrief.article_enrichment.search_queries.map((q: string, i: number) => (
                          <li key={i} className="text-xs text-gray-400 font-mono pl-3 border-l border-purple-600/30">
                            {q}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {researchBrief.article_enrichment.enriched_summary && (
                    <div>
                      <span className="text-xs text-gray-500">Enriched summary:</span>
                      <div className="mt-1 text-xs text-gray-300 leading-relaxed bg-purple-500/5 rounded p-2 max-h-[300px] overflow-y-auto">
                        {researchBrief.article_enrichment.enriched_summary}
                      </div>
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Full Research Brief (collapsible raw JSON) */}
            <details className="group">
              <summary className="cursor-pointer text-xs font-medium text-gray-500 hover:text-gray-300 transition">
                Raw Research Brief (JSON)
              </summary>
              <pre className="mt-2 text-xs text-gray-500 font-mono bg-black/30 rounded-lg p-3 max-h-[500px] overflow-auto whitespace-pre-wrap">
                {JSON.stringify(researchBrief, null, 2)}
              </pre>
            </details>
          </div>
          )}
        </div>
      )}
    </div>
  );
}
