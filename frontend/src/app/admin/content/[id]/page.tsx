"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";

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
}

interface QCResult {
  check: string;
  passed: boolean;
  detail: string;
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
   Component
   ───────────────────────────────────────────── */

export default function ContentEditor() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const writeupId = params.id as string;
  const sport = searchParams.get("sport") || "mlb";

  const [writeup, setWriteup] = useState<Writeup | null>(null);
  const [publicContent, setPublicContent] = useState("");
  const [premiumContent, setPremiumContent] = useState("");
  const [title, setTitle] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [qcResults, setQcResults] = useState<QCResult[]>([]);
  const [activeTab, setActiveTab] = useState<"public" | "premium">("public");
  const [showQc, setShowQc] = useState(false);
  const [showResearch, setShowResearch] = useState(false);
  const [researchBrief, setResearchBrief] = useState<any>(null);

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
      }

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
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 180_000);
      const res = await fetch(
        `http://localhost:8001/writeups/${sport}/generate/${writeup.game_id}`,
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
        alert("Regeneration timed out after 3 minutes.");
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
          onClick={() => router.back()}
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
              onClick={() => router.push("/admin/content")}
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
