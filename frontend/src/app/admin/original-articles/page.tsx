"use client";

import { useEffect, useState, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSeo } from "@/components/Seo";

const SPORTS = ["all", "mlb", "nfl", "nba"] as const;
type Sport = (typeof SPORTS)[number];

const SPORT_LABEL: Record<Sport, string> = { all: "All Sports", mlb: "MLB", nfl: "NFL", nba: "NBA" };

interface Article {
  id: number;
  sport: Sport;
  title: string;
  summary: string | null;
  content: string;
  status: string;
  published_at: string | null;
  created_at: string;
  author?: string;
  tokens_used?: number | null;
  has_prompt?: boolean;
  has_research?: boolean;
  research_steps?: number;
  reasoning?: string | null;
  word_min?: number | null;
  word_max?: number | null;
  word_count?: number | null;
  slug?: string | null;
  seo_description?: string | null;
  seo_keywords?: string | null;
  has_inaccuracy?: boolean;
  accuracy_check?: any;
  accuracy_check_tokens?: number | null;
  rejection_history?: any[];
  visibility?: string;
}

interface ArticleDetail extends Article {
  prompt_json?: unknown[];
  research_json?: unknown[];
}

const token = () => localStorage.getItem("earl_token");
const JSON_HEADERS = { "Content-Type": "application/json" };
const authHeaders = (extra: Record<string, string> = {}) => {
  const t = token();
  return { ...JSON_HEADERS, ...(t ? { Authorization: `Bearer ${t}` } : {}), ...extra };
};

const DEFAULT_INSTRUCTIONS: Record<Sport, string> = {
  nfl: "Write an original NFL article previewing this week's slate of games. Cover marquee matchups, key storylines, injuries, and betting angles (spread/OU). Use research to back up your points.",
  nba: "Write an original NBA article previewing this week's slate of games. Cover marquee matchups, star players, injuries, and betting angles (spread/OU). Use research to back up your points.",
  mlb: "Write an original MLB article previewing this week's slate of games. Cover pitching matchups, hot/cold lineups, injuries, and betting angles (run line/OU). Use research to back up your points.",
  all: "Write an original site-wide editorial article that cuts across the NFL, NBA, and MLB at once. Cover the biggest stories, marquee matchups, and what fans should be watching across all three leagues (e.g. upcoming games, play of the day, biggest stories in sports). Use the sport-prefixed research tools (mlb_, nfl_, nba_) to ground your claims in each league's data.",
};

type Tab = "create" | "edit" | "ideas";

export default function AdminOriginalArticles() {
  useSeo({ title: "Original Articles — Admin — Earl Knows Ball" });
  const [tab, setTab] = useState<Tab>("create");

  // --- shared sport state ---
  const [sport, setSport] = useState<Sport>("mlb");
  const [articles, setArticles] = useState<Article[]>([]);
  const [loading, setLoading] = useState(true);

  // --- create article state ---
  const [instructions, setInstructions] = useState("");
  const [author, setAuthor] = useState("Earl");
  const [reasoning, setReasoning] = useState("medium");
  const [visibility, setVisibility] = useState("public");
  const [wordRange, setWordRange] = useState<[number, number]>([400, 700]);
  const [draftId, setDraftId] = useState<number | null>(null);
  const [draft, setDraft] = useState<{
    title: string;
    content: string;
    summary: string;
    tokens?: number;
    prompt?: unknown[];
    research?: unknown[];
    accuracy_check?: any | null;
    rejection_history?: any[];
  } | null>(null);
  const [generating, setGenerating] = useState(false);

  // --- edit article state ---
  const [editing, setEditing] = useState<Article | null>(null);
  const [editRejectionHistory, setEditRejectionHistory] = useState<any[]>([]);
  const [editAccuracyCheck, setEditAccuracyCheck] = useState<any>(null);
  const [editUsageLog, setEditUsageLog] = useState<any[]>([]);
  const [editTitle, setEditTitle] = useState("");
  const [editContent, setEditContent] = useState("");
  const [editAuthor, setEditAuthor] = useState("Earl");
  const [saving, setSaving] = useState(false);
  const [autoSaving, setAutoSaving] = useState(false);
  const [editMarkdown, setEditMarkdown] = useState(false);
  const [editInstructions, setEditInstructions] = useState("");
  const [editSeoDesc, setEditSeoDesc] = useState("");
  const [editSeoKeywords, setEditSeoKeywords] = useState("");
  const [editVisibility, setEditVisibility] = useState("public");
  const [includeResearch, setIncludeResearch] = useState(true);
  const [reediting, setReediting] = useState(false);
  const [regenerating, setRegenerating] = useState(false);

  // --- research panel state ---
  const [openResearchId, setOpenResearchId] = useState<number | null>(null);
  const [researchDetail, setResearchDetail] = useState<ArticleDetail | null>(null);
  const [researchLoading, setResearchLoading] = useState(false);

  const fetchArticles = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/admin/original-articles/${sport}`, {
        headers: authHeaders(),
      });
      if (res.ok) {
        const data = await res.json();
        setArticles(data.articles ?? []);
      }
    } catch {
    } finally {
      setLoading(false);
    }
  }, [sport]);

  useEffect(() => {
    setDraft(null);
    fetchArticles();
  }, [sport, fetchArticles]);

  // ───────────────────────────
  //  Create Article
  // ───────────────────────────

  const handleGenerate = async () => {
    if (!instructions.trim()) {
      alert("Enter instructions for the article first.");
      return;
    }
    setDraft(null);
    setGenerating(true);
    try {
      const res = await fetch(`/api/original-articles/${sport}/generate`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ instructions, reasoning, word_count: wordRange, visibility }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const e = await res.json();
          detail = e.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      const data = await res.json();
      setDraftId(data.draft_id ?? null);
      setDraft({
        title: data.title,
        content: data.content,
        summary: data.summary,
        tokens: data.tokens,
        prompt: data.prompt,
        research: data.research,
        accuracy_check: data.accuracy_check ?? null,
        rejection_history: Array.isArray(data.rejection_history) ? data.rejection_history : [],
      });
      await fetchArticles(); // draft now visible in Edit tab
    } catch (e: any) {
      alert(`Generation failed: ${e.message}`);
    } finally {
      setGenerating(false);
    }
  };

  const handlePublish = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      // Publish flips the generated draft (already persisted with prompt/research
      // + token count) to published via PATCH. Fall back to POST /publish only
      // if there's no persisted draft to update.
      let data: any;
      if (draftId) {
        const res = await fetch(`/api/admin/original-articles/${sport}/${draftId}`, {
          method: "PATCH",
          headers: authHeaders(),
          body: JSON.stringify({
            title: draft.title,
            content: draft.content,
            author: author.trim() || "Earl",
            status: "published",
            visibility,
          }),
        });
        if (!res.ok) {
          let detail = `HTTP ${res.status}`;
          try {
            const e = await res.json();
            detail = e.detail || detail;
          } catch {}
          throw new Error(detail);
        }
        const upd = await res.json();
        data = { article: upd.article };
      } else {
        const res = await fetch(`/api/original-articles/${sport}/publish`, {
          method: "POST",
          headers: authHeaders(),
        body: JSON.stringify({
          title: draft.title,
          content: draft.content,
          summary: draft.summary,
          instructions,
          prompt: draft.prompt,
          research: draft.research,
          author: author.trim() || "Earl",
          tokens_used: draft.tokens ?? null,
          visibility,
        }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const e = await res.json();
          detail = e.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      data = await res.json(); // fallback POST /publish (no persisted draft)
      }
      setDraft(null);
      setDraftId(null);
      setInstructions("");
      await fetchArticles();
      alert(`✅ Published "${data.article.title}" to /${sport}.`);
    } catch (e: any) {
      alert(`Publish failed: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  // ───────────────────────────
  //  Edit Articles
  // ───────────────────────────

  const startEdit = async (a: Article) => {
    setEditing(a);
    setEditTitle(a.title);
    setEditContent(a.content);
    setEditAuthor(a.author || "Earl");
    setEditSeoDesc(a.seo_description || "");
    setEditSeoKeywords(a.seo_keywords || "");
    setEditVisibility(a.visibility || "public");
    setEditMarkdown(false);
    setEditInstructions("");
    // Fetch full detail (incl. accuracy_check + rejection_history) for audit view.
    setEditRejectionHistory([]);
    setEditAccuracyCheck(null);
    try {
      const res = await fetch(
        `/api/admin/original-articles/${a.sport}/${a.id}`,
        { headers: authHeaders() }
      );
      if (res.ok) {
        const d = await res.json();
        const art = d.article || d;
        setEditRejectionHistory(Array.isArray(art.rejection_history) ? art.rejection_history : []);
        setEditAccuracyCheck(art.accuracy_check ?? null);
        setEditUsageLog(Array.isArray(art.usage_log) ? art.usage_log : []);
        if (art.visibility) setEditVisibility(art.visibility);
      }
    } catch {}
  };

  const cancelEdit = () => {
    setEditing(null);
  };

  const handleReEdit = async () => {
    if (!editing) return;
    if (!editInstructions.trim()) {
      alert("Enter instructions for the AI rewrite first.");
      return;
    }
    setReediting(true);
    try {
      const res = await fetch(
        `/api/admin/original-articles/${editing.sport}/${editing.id}/re-edit`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({
            instructions: editInstructions.trim(),
            include_research: includeResearch,
          }),
        }
      );
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const e = await res.json();
          detail = e.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      const data = await res.json();
      setEditTitle(data.title || editTitle);
      setEditContent(data.content || editContent);
      setEditMarkdown(false);
      alert("✅ AI rewrite ready. Review it, then hit Save to persist.");
    } catch (e: any) {
      alert(`AI rewrite failed: ${e.message}`);
    } finally {
      setReediting(false);
    }
  };

  const handleRegenTitle = async () => {
    if (!editing) return;
    setRegenerating(true);
    const prev = editTitle;
    try {
      const res = await fetch(
        `/api/admin/original-articles/${editing.sport}/${editing.id}/regenerate-title`,
        {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({
            include_research: includeResearch,
            extra: editInstructions.trim(), // reuse any guidance written in the AI box
          }),
        }
      );
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const e = await res.json();
          detail = e.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      const data = await res.json();
      setEditTitle(data.title || prev);
      alert("✅ New title ready. Review it, then hit Save to persist.");
    } catch (e: any) {
      alert(`Title regeneration failed: ${e.message}`);
    } finally {
      setRegenerating(false);
    }
  };

  const handleSaveEdit = async () => {
    if (!editing) return;
    if (!editTitle.trim() || !editContent.trim()) {
      alert("Title and content cannot be empty.");
      return;
    }
    setSaving(true);
    try {
      const res = await fetch(`/api/admin/original-articles/${editing.sport}/${editing.id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({
          title: editTitle.trim(),
          content: editContent,
          author: editAuthor.trim() || "Earl",
          summary: null, // re-derive on backend
          seo_description: editSeoDesc.trim() || null,
          seo_keywords: editSeoKeywords.trim() || null,
          visibility: editVisibility,
        }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const e = await res.json();
          detail = e.detail || detail;
        } catch {}
        throw new Error(detail);
      }
      await fetchArticles();
      setEditing(null);
      alert("✅ Article saved.");
    } catch (e: any) {
      alert(`Save failed: ${e.message}`);
    } finally {
      setSaving(false);
    }
  };

  /** Save the currently-edited article as a continuously-generated template. */
  const handleSaveAsContinuous = async () => {
    if (!editing) return;
    if (!editTitle.trim()) {
      alert("Give the article a title before saving as a continuous template.");
      return;
    }
    setAutoSaving(true);
    try {
      const res = await fetch(`/api/admin/auto-generation/from-article`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          sport: editing.sport,
          article_id: editing.id,
          title: editTitle.trim(),
          description: editSeoDesc.trim() || null,
          instructions: editInstructions.trim() || null,
          cadence: "daily",
          scope_type: "sport",
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `HTTP ${res.status}`);
      alert(`✅ Saved as a continuous template (id ${data?.id}).

Manage it under Admin → Auto Generation.`);
    } catch (e: any) {
      alert(`Could not save as continuous template: ${e.message}`);
    } finally {
      setAutoSaving(false);
    }
  };

  const handleToggleStatus = async (a: Article) => {
    const next = a.status === "published" ? "draft" : "published";
    if (!confirm(`Move "${a.title.slice(0, 60)}..." to ${next === "published" ? "published" : "draft"}?`))
      return;
    try {
      const res = await fetch(`/api/admin/original-articles/${a.sport}/${a.id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ status: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchArticles();
    } catch (e: any) {
      alert(`Status change failed: ${e.message}`);
    }
  };

  const handleDelete = async (id: number, title: string) => {
    if (!confirm(`Delete article "${title.slice(0, 60)}..."? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/original-articles/${sport}/${id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (editing?.id === id) setEditing(null);
      if (openResearchId === id) {
        setOpenResearchId(null);
        setResearchDetail(null);
      }
      await fetchArticles();
    } catch (e: any) {
      alert(`Delete failed: ${e.message}`);
    }
  };

  const toggleResearch = async (a: Article) => {
    if (openResearchId === a.id) {
      setOpenResearchId(null);
      setResearchDetail(null);
      return;
    }
    setOpenResearchId(a.id);
    setResearchDetail(null);
    setResearchLoading(true);
    try {
      const res = await fetch(`/api/admin/original-articles/${a.sport}/${a.id}`, {
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setResearchDetail(data.article ?? null);
    } catch (e: any) {
      setOpenResearchId(null);
      setResearchDetail(null);
      alert(`Could not load research: ${e.message}`);
    } finally {
      setResearchLoading(false);
    }
  };

  const tabButton = (t: Tab, label: string) => (
    <button
      onClick={() => setTab(t)}
      className={`px-4 py-2 rounded-md text-sm font-medium transition ${
        t === tab ? "bg-blue-600 text-white" : "text-gray-300 hover:bg-white/5 hover:text-white"
      }`}
    >
      {label}
    </button>
  );

  const sportTab = (s: Sport) => (
    <button
      key={s}
      onClick={() => setSport(s)}
      className={`px-4 py-2 rounded-md text-sm font-medium transition ${
        s === sport
          ? "bg-blue-600 text-white"
          : "text-gray-300 hover:bg-white/5 hover:text-white"
      }`}
    >
      {SPORT_LABEL[s]}
    </button>
  );

  const statusBadge = (status: string) =>
    status === "published" ? (
      <span className="text-xs px-2 py-0.5 rounded-full bg-green-600/20 text-green-400 border border-green-600/30">
        Published
      </span>
    ) : (
      <span className="text-xs px-2 py-0.5 rounded-full bg-yellow-600/20 text-yellow-400 border border-yellow-600/30">
        Draft
      </span>
    );

  return (
    <div className="max-w-5xl mx-auto p-6">
      <div className="mb-6">
        <h1 className="text-2xl font-bold mb-1">Original Articles</h1>
        <p className="text-sm text-gray-400">
          Write LLM-authored editorial articles using the same research tools as chat, or manage existing
          articles.
        </p>
      </div>

      {/* Top-level tabs: Create Article / Edit Articles / Article Ideas */}
      <div className="flex gap-1 mb-6 bg-white/[0.03] border border-white/10 rounded-lg p-1 w-fit">
        {tabButton("create", "Create Article")}
        {tabButton("edit", "Edit Articles")}
        {tabButton("ideas", "Article Ideas")}
      </div>

      {/* Sport tabs (shared) */}
      <div className="flex gap-1 mb-6 bg-white/[0.03] border border-white/10 rounded-lg p-1 w-fit">
        {SPORTS.map(sportTab)}
      </div>

      {tab === "create" ? (
        <>
          {/* Composer */}
          <div className="bg-white/[0.03] border border-white/10 rounded-lg p-5 mb-8">
            <div className="flex items-center justify-between mb-2">
              <label className="text-sm font-medium text-gray-200">
                Article instructions ({SPORT_LABEL[sport]})
              </label>
              <button
                className="text-xs text-gray-400 hover:text-gray-200 underline"
                onClick={() => setInstructions(DEFAULT_INSTRUCTIONS[sport])}
              >
                Use default prompt
              </button>
            </div>
            <textarea
              className="w-full bg-black/40 border border-white/10 rounded-md p-3 text-sm min-h-[110px] focus:outline-none focus:border-blue-500"
              placeholder="Describe the article you want written… e.g. Preview this week's marquee matchup and give betting angles."
              value={instructions}
              onChange={(e) => setInstructions(e.target.value)}
            />
            <div className="flex flex-wrap items-center gap-4 mt-3">
              <div className="flex items-center gap-2">
                <label className="text-xs text-gray-400 shrink-0">Reasoning</label>
                <select
                  className="bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                  value={reasoning}
                  onChange={(e) => setReasoning(e.target.value)}
                >
                  <option value="minimal">Minimal</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                  <option value="xhigh">Extra high</option>
                </select>
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-gray-400 shrink-0">Visibility</label>
                <select
                  className="bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                  value={visibility}
                  onChange={(e) => setVisibility(e.target.value)}
                >
                  <option value="public">Public (FREE — no betting advice)</option>
                  <option value="premium">Premium (members — betting advice OK)</option>
                </select>
              </div>
              <div className="flex items-center gap-2">
                <label className="text-xs text-gray-400 shrink-0">Length</label>
                <select
                  className="bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                  value={wordRange.join("–")}
                  onChange={(e) => {
                    const [lo, hi] = e.target.value.split("–").map((s) => parseInt(s, 10));
                    if (!Number.isNaN(lo) && !Number.isNaN(hi)) setWordRange([lo, hi]);
                  }}
                >
                  <option value="300–500">Short (~300–500 words)</option>
                  <option value="400–700">Medium (~400–700 words)</option>
                  <option value="700–1100">Long (~700–1,100 words)</option>
                  <option value="1100–1600">Very long (~1,100–1,600 words)</option>
                  <option value="1600–2200">Deep dive (~1,600–2,200 words)</option>
                </select>
              </div>
            </div>
            <div className="flex items-center gap-3 mt-3">
              <button
                onClick={handleGenerate}
                disabled={generating || saving}
                className="px-4 py-2 rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-sm font-medium"
              >
                {generating ? "Generating…" : "Generate article"}
              </button>
              {generating && (
                <span className="text-xs text-blue-300 animate-pulse">
                  Researching via chat engine (stats, lines, injuries)…
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 mt-3 pt-3 border-t border-white/10">
              <label className="text-xs text-gray-400 shrink-0">Author</label>
              <input
                className="flex-1 max-w-xs bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                value={author}
                onChange={(e) => setAuthor(e.target.value)}
                placeholder="Earl"
              />
            </div>
          </div>

          {/* Draft preview */}
          {draft && (
            <div className="bg-white/[0.03] border border-white/10 rounded-lg p-5 mb-8">
              <div className="flex items-center justify-between mb-4">
                <h2 className="text-lg font-semibold">Draft — {SPORT_LABEL[sport]}</h2>
                <div className="flex items-center gap-3">
                  {typeof draft.tokens === "number" && (
                    <span className="text-xs text-gray-500">{draft.tokens.toLocaleString()} tokens</span>
                  )}
                  {draft.accuracy_check && (
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium border ${
                        draft.accuracy_check.passed
                          ? "bg-green-500/10 text-green-400 border-green-500/30"
                          : "bg-red-500/10 text-red-400 border-red-500/30"
                      }`}
                      title="Accuracy check result"
                    >
                      {draft.accuracy_check.passed ? "✓ Accurate" : "✗ Inaccurate"}
                      {typeof draft.accuracy_check.retries_used === "number" &&
                        draft.accuracy_check.retries_used > 0 && (
                          <span className="ml-1 opacity-70">({draft.accuracy_check.retries_used} fix)</span>
                        )}
                    </span>
                  )}
                  {Array.isArray(draft.rejection_history) && draft.rejection_history.length > 0 && (
                    <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/10 text-amber-400 border border-amber-500/30">
                      ⚠ {draft.rejection_history.length} rejected
                    </span>
                  )}
                  <button
                    onClick={() => handlePublish()}
                    disabled={saving}
                    className="px-4 py-2 rounded-md bg-green-600 hover:bg-green-500 disabled:opacity-50 text-sm font-medium"
                  >
                    {saving ? "Publishing…" : "Publish to /" + sport}
                  </button>
                </div>
              </div>
              <div className="writeup-content max-h-[60vh] overflow-y-auto">
                <div className="text-gray-300 leading-relaxed">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{draft.content}</ReactMarkdown>
                </div>
              </div>
            </div>
          )}
        </>
      ) : tab === "edit" ? (
        /* ── Edit Articles ── */
        <div>
          {loading ? (
            <div className="text-sm text-gray-500">Loading…</div>
          ) : articles.length === 0 ? (
            <div className="text-sm text-gray-500 bg-white/[0.03] border border-white/10 rounded-lg p-4">
              No original articles for {SPORT_LABEL[sport]} yet.
            </div>
          ) : (
            <ul className="divide-y divide-white/10 border border-white/10 rounded-lg">
              {articles.map((a) => (
                <li key={a.id}>
                  <div className="flex items-center justify-between px-4 py-3 gap-4">
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-white truncate">{a.title}</span>
                        {statusBadge(a.status)}
                        {(a.visibility === "premium" ? (
                          <span
                            title="Premium article (members only) — may include Earl's picks and betting advice."
                            className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-purple-500/20 text-purple-300 border border-purple-500/30"
                          >
                            ★ Premium
                          </span>
                        ) : (
                          <span
                            title="Public article (free) — no betting advice allowed."
                            className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-600/30 text-gray-300 border border-gray-600/40"
                          >
                            Public
                          </span>
                        ))}
                        {a.has_inaccuracy && (
                          <span
                            title="Accuracy check flagged claims that couldn't be resolved. Needs human review."
                            className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30"
                          >
                            ⚠ Inaccurate
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 mt-0.5">
                        {a.author && (
                          <>
                            <span className="text-gray-300">{a.author}</span>
                            {" · "}
                          </>
                        )}
                        {a.published_at
                          ? `Published ${new Date(a.published_at).toLocaleString()}`
                          : "Not published yet"}
                        {" · "}
                        {a.summary?.slice(0, 120) || "No summary"}
                        {typeof a.tokens_used === "number" && (
                          <>
                            {" · "}
                            {a.tokens_used.toLocaleString()} tokens
                          </>
                        )}
                      </div>
                      <div className="text-[11px] text-gray-600 mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5">
                        <span>Reasoning: <span className="text-gray-400 capitalize">{a.reasoning || "medium"}</span></span>
                        <span>
                          Target: <span className="text-gray-400">
                            {typeof a.word_min === "number" && typeof a.word_max === "number"
                              ? `${a.word_min.toLocaleString()}–${a.word_max.toLocaleString()} words`
                              : "—"}
                          </span>
                        </span>
                        <span>
                          Final: <span className="text-gray-400">
                            {typeof a.word_count === "number"
                              ? `${a.word_count.toLocaleString()} words`
                              : "—"}
                          </span>
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <a
                        href={`/${a.sport}/articles/${a.slug || a.id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-blue-400 hover:text-blue-300"
                      >
                        View
                      </a>
                      <button
                        onClick={() => startEdit(a)}
                        className="text-xs text-blue-400 hover:text-blue-300"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => toggleResearch(a)}
                        className="text-xs text-purple-400 hover:text-purple-300"
                        title={(a.has_research ? `${a.research_steps ?? 0} research steps` : "No research stored") + "; view prompt + research"}
                      >
                        Research
                      </button>
                      <button
                        onClick={() => handleToggleStatus(a)}
                        className="text-xs text-yellow-400 hover:text-yellow-300"
                        title={a.status === "published" ? "Move to draft" : "Publish"}
                      >
                        {a.status === "published" ? "→ Draft" : "→ Publish"}
                      </button>
                      <button
                        onClick={() => handleDelete(a.id, a.title)}
                        className="text-xs text-red-400 hover:text-red-300"
                      >
                        Delete
                      </button>
                    </div>
                  </div>

                  {/* Research + Prompt panel */}
                  {openResearchId === a.id && (
                    <ResearchPanel
                      loading={researchLoading}
                      detail={researchDetail}
                      hasResearch={!!a.has_research}
                      onClose={() => {
                        setOpenResearchId(null);
                        setResearchDetail(null);
                      }}
                    />
                  )}

                  {/* Inline editor */}
                  {editing && editing.id === a.id && (
                    <div className="border-t border-white/10 bg-black/20 p-4">
                      <div className="flex items-center justify-between mb-3">
                        <span className="text-sm font-medium text-gray-200">Editing</span>
                        <div className="flex items-center gap-3">
                          <button
                            onClick={() => setEditMarkdown(!editMarkdown)}
                            className="text-xs text-gray-400 hover:text-gray-200 underline"
                          >
                            {editMarkdown ? "View rendered" : "Edit markdown"}
                          </button>
                          <button
                            onClick={cancelEdit}
                            className="text-xs text-gray-400 hover:text-gray-200"
                          >
                            Cancel
                          </button>
                          <button
                            onClick={handleSaveEdit}
                            disabled={saving}
                            className="px-3 py-1.5 rounded-md bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-sm font-medium"
                          >
                            {saving ? "Saving…" : "Save"}
                          </button>
                          <button
                            onClick={handleSaveAsContinuous}
                            disabled={autoSaving}
                            title="Save this article as a continuously-generated template (shows up on the Auto Generation page)."
                            className="px-3 py-1.5 rounded-md bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 text-xs font-medium"
                          >
                            {autoSaving ? "Adding…" : "♻ Save as Continuous"}
                          </button>
                          <button
                            onClick={handleRegenTitle}
                            disabled={regenerating}
                            title="Generate a new headline from the article content."
                            className="px-3 py-1.5 rounded-md bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-xs font-medium"
                          >
                            {regenerating ? "Writing…" : "✦ Regenerate Title"}
                          </button>
                        </div>
                      </div>
                      <input
                        className="w-full bg-black/40 border border-white/10 rounded-md p-2 mb-2 text-sm focus:outline-none focus:border-blue-500"
                        value={editTitle}
                        onChange={(e) => setEditTitle(e.target.value)}
                        placeholder="Title"
                      />
                      <div className="flex items-center gap-2 mb-2">
                        <label className="text-xs text-gray-400 shrink-0">Author</label>
                        <input
                          className="flex-1 bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                          value={editAuthor}
                          onChange={(e) => setEditAuthor(e.target.value)}
                          placeholder="Earl"
                        />
                      </div>
                      <div className="flex items-center gap-2 mb-2">
                        <label className="text-xs text-gray-400 shrink-0">Visibility</label>
                        <select
                          className="flex-1 bg-black/40 border border-white/10 rounded-md p-2 text-sm focus:outline-none focus:border-blue-500"
                          value={editVisibility}
                          onChange={(e) => setEditVisibility(e.target.value)}
                        >
                          <option value="public">Public (FREE — no betting advice)</option>
                          <option value="premium">Premium (members — betting advice OK)</option>
                        </select>
                      </div>
                      <div className="mb-2 p-3 rounded-md border border-white/10 bg-black/25">
                        <div className="flex items-center justify-between mb-1.5">
                          <label className="text-xs font-medium text-gray-200">SEO (meta description & keywords)</label>
                          <button
                            onClick={() => {
                              // Trigger a fresh LLM regenerate on next save by clearing fields.
                              setEditSeoDesc("");
                              setEditSeoKeywords("");
                            }}
                            className="text-xs text-blue-400 hover:text-blue-300 underline"
                          >
                            Regenerate on save
                          </button>
                        </div>
                        <textarea
                          className="w-full bg-black/40 border border-white/10 rounded-md p-2 mb-2 text-xs focus:outline-none focus:border-blue-500 resize-y"
                          rows={2}
                          value={editSeoDesc}
                          onChange={(e) => setEditSeoDesc(e.target.value)}
                          placeholder="Meta description (140-160 chars)…"
                        />
                        <input
                          className="w-full bg-black/40 border border-white/10 rounded-md p-2 text-xs focus:outline-none focus:border-blue-500"
                          value={editSeoKeywords}
                          onChange={(e) => setEditSeoKeywords(e.target.value)}
                          placeholder="keyword1, keyword2, keyword3…"
                        />
                      </div>
                      <div className="mb-2 rounded-md border border-white/10 bg-black/25">
                        <div className="flex items-center justify-between px-3 py-2">
                          <label className="text-xs text-gray-300 flex items-center gap-1.5 select-none cursor-pointer">
                            <input
                              type="checkbox"
                              checked={includeResearch}
                              onChange={(e) => setIncludeResearch(e.target.checked)}
                              className="accent-emerald-500"
                            />
                            Send previously gathered research
                          </label>
                          <button
                            onClick={handleReEdit}
                            disabled={reediting || !editInstructions.trim()}
                            className="px-3 py-1 rounded-md bg-emerald-600 hover:bg-emerald-500 disabled:opacity-50 disabled:cursor-not-allowed text-xs font-medium"
                          >
                            {reediting ? "Rewriting…" : "Apply with AI"}
                          </button>
                        </div>
                        <textarea
                          className="w-full bg-transparent border-t border-white/10 p-3 text-sm min-h-[80px] focus:outline-none resize-y"
                          value={editInstructions}
                          onChange={(e) => setEditInstructions(e.target.value)}
                          placeholder={"Tell the AI what to change, e.g. \"Add a section on the QB matchup\" or \"Tighten the intro and expand the betting analysis\". It sends the article's prior research (toggle above) plus these instructions, and can pull more data if needed."}
                        />
                      </div>
                      <textarea
                        className="w-full bg-black/40 border border-white/10 rounded-md p-3 text-sm min-h-[200px] font-mono focus:outline-none focus:border-blue-500"
                        value={editContent}
                        onChange={(e) => setEditContent(e.target.value)}
                        placeholder="Article content (markdown)"
                      />
                      {/* ── Accuracy + Rejected Drafts (audit) ── */}
                      {
                        (editAccuracyCheck || editRejectionHistory.length > 0) && (
                          <div className="mt-3 rounded-md border border-white/10 bg-black/25 p-3 space-y-3">
                            {editAccuracyCheck && (
                              <div>
                                <div className="flex items-center gap-2 text-xs mb-1.5">
                                  <span className="font-medium text-gray-300">Accuracy check</span>
                                  <span
                                    className={`inline-block px-2 py-0.5 rounded text-xs font-medium ${
                                      editAccuracyCheck.passed
                                        ? "bg-green-500/15 text-green-400"
                                        : "bg-red-500/15 text-red-400"
                                    }`}
                                  >
                                    {editAccuracyCheck.passed ? "passed" : "failed"}
                                  </span>
                                  {typeof editAccuracyCheck.retries_used === "number" && (
                                    <span className="text-gray-500">retries: {editAccuracyCheck.retries_used}</span>
                                  )}
                                </div>
                                {Array.isArray(editAccuracyCheck.findings) &&
                                  editAccuracyCheck.findings.length > 0 && (
                                    <ul className="list-disc list-inside space-y-0.5 text-xs text-gray-400">
                                      {editAccuracyCheck.findings.map((f: any, i: number) => (
                                        <li key={i}>{typeof f === "string" ? f : JSON.stringify(f)}</li>
                                      ))}
                                    </ul>
                                  )}
                                {editAccuracyCheck.raw ? (
                                  <details className="text-xs">
                                    <summary className="cursor-pointer text-gray-500 hover:text-gray-300">Show accuracy log</summary>
                                    <pre className="mt-1 whitespace-pre-wrap rounded bg-black/40 p-2 text-[11px] text-gray-400 max-h-48 overflow-auto">
                                      {editAccuracyCheck.raw}
                                    </pre>
                                  </details>
                                ) : editAccuracyCheck.error ? (
                                  <p className="text-[11px] text-red-400/80">Error: {String(editAccuracyCheck.error)}
                                  </p>
                                ) : null}
                              </div>
                            )}
                            {editRejectionHistory.length > 0 && (
                              <div>
                                <div className="flex items-center gap-2 text-xs mb-1.5">
                                  <span className="font-medium text-gray-300">Rejected drafts</span>
                                  <span className="px-2 py-0.5 rounded bg-amber-500/15 text-amber-400 text-xs font-medium">
                                    {editRejectionHistory.length}
                                  </span>
                                </div>
                                {editRejectionHistory.map((d: any, idx: number) => {
                                  const findings = Array.isArray(d.accuracy_check?.findings)
                                    ? d.accuracy_check.findings
                                    : [];
                                  const passed = d.accuracy_check?.passed;
                                  return (
                                    <div key={idx} className="rounded border border-red-500/25 bg-red-500/5 p-2 mb-2 space-y-1.5">
                                      <div className="flex flex-wrap items-center gap-2 text-xs">
                                        <span className="font-medium text-red-400">Attempt {d.attempt ?? idx + 1}</span>
                                        {d.timestamp && <span className="text-gray-500">{new Date(d.timestamp).toLocaleString()}</span>}
                                        <span
                                          className={`px-1.5 py-0.5 rounded text-[11px] font-medium ${
                                            passed ? "bg-green-500/15 text-green-400" : "bg-red-500/15 text-red-400"
                                          }`}
                                        >
                                          {passed ? "passed" : "failed"}
                                        </span>
                                      </div>
                                      {findings.length > 0 && (
                                        <ul className="list-disc list-inside space-y-0.5 text-[11px] text-gray-400">
                                          {findings.map((f: any, i: number) => (
                                            <li key={i}>{typeof f === "string" ? f : JSON.stringify(f)}</li>
                                          ))}
                                        </ul>
                                      )}
                                      {d.accuracy_check?.raw ? (
                                        <details className="text-[11px]">
                                          <summary className="text-gray-500 cursor-pointer">Show accuracy log</summary>
                                          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-black/40 border border-gray-700 p-2 text-[11px] text-gray-400">
                                            {d.accuracy_check.raw}
                                          </pre>
                                        </details>
                                      ) : d.accuracy_check?.error ? (
                                        <p className="text-[11px] text-red-400/80">Error: {String(d.accuracy_check.error)}</p>
                                      ) : null}
                                      {d.content && (
                                        <details className="text-[11px]">
                                          <summary className="text-blue-400 cursor-pointer">View rejected draft</summary>
                                          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-black/40 border border-gray-700 p-2 text-[11px] text-gray-300">
                                            {d.content}
                                          </pre>
                                        </details>
                                      )}
                                    </div>
                                  );
                                })}
                              </div>
                            )}
                          </div>
                        )
                      }
                      {/* ── Usage Log (per-call token & cost breakdown) ── */}
                      {editUsageLog.length > 0 && (
                        <div className="mt-3 rounded-md border border-white/10 bg-black/25 p-3">
                          <div className="text-xs font-medium text-gray-300 mb-2">
                            Usage Log — cost breakdown
                          </div>
                          <div className="space-y-1.5">
                            {editUsageLog.map((c: any, i: number) => {
                              let label = String(c.call || "").replace(
                                /_/g,
                                " "
                              );
                              const reasoning =
                                c.reasoning === "disabled"
                                  ? "thinking off"
                                  : `reasoning ${c.reasoning || "default"}`;
                              const comp = Number(c.completion_tokens || 0);
                              const reas = Number(c.reasoning_tokens || 0);
                              const outTok = comp + reas;
                              const outCost = (outTok / 1_000_000) * 0.28;
                              const hit = Number(c.prompt_cache_hit_tokens || 0);
                              const miss = Number(c.prompt_cache_miss_tokens || 0);
                              const inCost =
                                (hit / 1_000_000) * 0.0028 +
                                (miss / 1_000_000) * 0.14;
                              const total = outCost + inCost;
                              return (
                                <div
                                  key={i}
                                  className="rounded-md border border-white/10 bg-black/30 px-2.5 py-2 text-[11px]"
                                >
                                  <div className="flex items-center justify-between flex-wrap gap-1">
                                    <span className="font-medium capitalize text-gray-200">
                                      {label}
                                    </span>
                                    <span className="text-gray-500">{reasoning}</span>
                                  </div>
                                  <div className="mt-1 grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-0.5 text-gray-400">
                                    <span title="Cached input tokens">
                                      cached {hit.toLocaleString()}
                                    </span>
                                    <span title="Uncached input tokens">
                                      input {miss.toLocaleString()}
                                    </span>
                                    <span title="Output tokens">
                                      output {outTok.toLocaleString()}
                                    </span>
                                    <span className="text-gray-200 font-medium">
                                      ${total.toFixed(4)}
                                    </span>
                                  </div>
                                  {c.total_tokens !== undefined && (
                                    <div className="mt-0.5 text-gray-500">
                                      total {Number(c.total_tokens).toLocaleString()}{" "}
                                      tokens
                                    </div>
                                  )}
                                </div>
                              );
                            })}
                          </div>
                          <div className="mt-2 pt-2 border-t border-white/10 flex justify-end">
                            <span className="text-xs font-medium text-gray-200">
                              Estimated total: $
                              {editUsageLog
                                .reduce((sum, c: any) => {
                                  const comp = Number(c.completion_tokens || 0);
                                  const reas = Number(c.reasoning_tokens || 0);
                                  const outTok = comp + reas;
                                  const hit = Number(
                                    c.prompt_cache_hit_tokens || 0
                                  );
                                  const miss = Number(
                                    c.prompt_cache_miss_tokens || 0
                                  );
                                  return (
                                    sum +
                                    (outTok / 1_000_000) * 0.28 +
                                    (hit / 1_000_000) * 0.0028 +
                                    (miss / 1_000_000) * 0.14
                                  );
                                }, 0)
                                .toFixed(4)}
                            </span>
                          </div>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <ArticleIdeasPanel
          sport={sport}
          onUsePrompt={(prompt, ideaSport) => {
            if (SPORT_LABEL[ideaSport as Sport]) setSport(ideaSport as Sport);
            setInstructions(prompt);
            setTab("create");
          }}
        />
      )}
    </div>
  );
}

interface IdeaItemType {
  id: number;
  sport: Sport;
  title: string;
  description: string | null;
  prompt: string | null;
  team_id: number | null;
  team_abbr: string | null;
  team_name: string | null;
  status: "active" | "used" | "archived";
  used_at: string | null;
  used_article_id: number | null;
  created_at: string;
  updated_at: string;
}

function ArticleIdeasPanel({
  sport,
  onUsePrompt,
}: {
  sport: Sport;
  onUsePrompt: (prompt: string, ideaSport: string) => void;
}) {
  const [instructions, setInstructions] = useState("");
  const [generating, setGenerating] = useState(false);
  const [quickMode, setQuickMode] = useState(false);
  const [storing, setStoring] = useState(false);
  const [ideas, setIdeas] = useState<IdeaItemType[]>([]);
  const [loadingIdeas, setLoadingIdeas] = useState(false);
  const [draftIdeas, setDraftIdeas] = useState<
    Array<{
      title: string;
      description: string | null;
      team_id: number | null;
      team_abbr: string | null;
      team_name: string | null;
    }>
  >([]);
  const [draftMsg, setDraftMsg] = useState<string | null>(null);
  const [generated, setGenerated] = useState(false);
  const [buildingId, setBuildingId] = useState<number | null>(null);
  const [builtPrompt, setBuiltPrompt] = useState<{ id: number; prompt: string } | null>(null);
  const [teams, setTeams] = useState<{ id: number; abbr: string; name: string }[]>([]);

  // Reload stored ideas + teams whenever the sport changes.
  useEffect(() => {
    let cancelled = false;
    setIdeas([]);
    setDraftIdeas([]);
    setGenerated(false);
    setBuiltPrompt(null);
    const load = async () => {
      setLoadingIdeas(true);
      try {
        const [res, tRes] = await Promise.all([
          fetch(`/api/admin/article-ideas/${sport}`, { headers: authHeaders() }),
          fetch(`/api/admin/article-ideas/${sport}/teams`, { headers: authHeaders() }),
        ]);
        if (!res.ok) throw new Error((await res.json()).detail || "Failed to load ideas");
        const data = await res.json();
        if (!cancelled) setIdeas(data);
        if (tRes.ok && !cancelled) setTeams(await tRes.json());
      } catch (e: any) {
        if (!cancelled) alert(`Could not load article ideas: ${e.message}`);
      } finally {
        if (!cancelled) setLoadingIdeas(false);
      }
    };
    load();
    return () => {
      cancelled = true;
    };
  }, [sport]);

  const generate = async () => {
    setGenerating(true);
    setDraftMsg(null);
    setGenerated(false);
    setBuiltPrompt(null);
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/generate`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ instructions, count: 8, quick: quickMode }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Generation failed");
      setDraftIdeas(data.ideas || []);
      setGenerated(true);
      if (!(data.ideas || []).length) setDraftMsg("LLM returned no ideas — try different instructions.");
    } catch (e: any) {
      alert(`Generation failed: ${e.message}`);
    } finally {
      setGenerating(false);
    }
  };

  const storeAll = async () => {
    if (!draftIdeas.length) return;
    setStoring(true);
    setDraftMsg(null);
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/store`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ ideas: draftIdeas }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Store failed");
      setDraftIdeas([]);
      setGenerated(false);
      setDraftMsg(`Stored ${data.count} idea${data.count === 1 ? "" : "s"}.`);
      setIdeas(await (await fetch(`/api/admin/article-ideas/${sport}`, { headers: authHeaders() })).json());
    } catch (e: any) {
      alert(`Store failed: ${e.message}`);
    } finally {
      setStoring(false);
    }
  };

  const storeOne = async (idea: (typeof draftIdeas)[number]) => {
    setStoring(true);
    setDraftMsg(null);
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/store`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ ideas: [idea] }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Store failed");
      setDraftIdeas((prev) => prev.filter((d) => d !== idea));
      if (!draftIdeas.length) setGenerated(false);
      setDraftMsg(`Saved.`);
      setIdeas(await (await fetch(`/api/admin/article-ideas/${sport}`, { headers: authHeaders() })).json());
    } catch (e: any) {
      alert(`Save failed: ${e.message}`);
    } finally {
      setStoring(false);
    }
  };

  const buildPrompt = async (ideaId: number) => {
    setBuildingId(ideaId);
    setBuiltPrompt(null);
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/build-prompt`, {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ idea_id: ideaId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Prompt build failed");
      setBuiltPrompt({ id: ideaId, prompt: data.prompt });
    } catch (e: any) {
      alert(`Prompt build failed: ${e.message}`);
    } finally {
      setBuildingId(null);
    }
  };

  // Compose a ready-to-use Create Article instruction from an idea.
  // Uses the saved LLM prompt when present; otherwise renders a brief
  // from the idea's title + description so the box is always populated.
  const sendToCreate = (idea: IdeaItemType) => {
    const brief = idea.prompt
      ? idea.prompt
      : [
          `Write an article about: ${idea.title}${idea.team_name ? ` (${idea.team_name})` : ""}`,
          idea.description || "",
        ]
          .filter(Boolean)
          .join("\n\n");
    onUsePrompt(brief, idea.sport);
  };

  const setIdeaStatus = async (idea: IdeaItemType, status: "active" | "used" | "archived") => {
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/${idea.id}`, {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ status }),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "Update failed");
      setIdeas((prev) => prev.map((i) => (i.id === idea.id ? { ...i, status, used_at: status === "used" ? new Date().toISOString() : status === "active" ? null : i.used_at } : i)));
    } catch (e: any) {
      alert(`Update failed: ${e.message}`);
    }
  };

  const deleteIdea = async (idea: IdeaItemType) => {
    if (!confirm(`Delete idea?\n\n${idea.title}`)) return;
    try {
      const res = await fetch(`/api/admin/article-ideas/${sport}/${idea.id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) throw new Error((await res.json()).detail || "Delete failed");
      setIdeas((prev) => prev.filter((i) => i.id !== idea.id));
    } catch (e: any) {
      alert(`Delete failed: ${e.message}`);
    }
  };

  const statusPill = (status: string, usedAt: string | null) => {
    if (status === "used")
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-green-500/20 text-green-300 border border-green-500/30">
          ✓ Used{usedAt ? ` ${new Date(usedAt).toLocaleDateString()}` : ""}
        </span>
      );
    if (status === "archived")
      return (
        <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-gray-500/20 text-gray-400 border border-gray-500/30">
          Archived
        </span>
      );
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-blue-500/20 text-blue-300 border border-blue-500/30">
        Active
      </span>
    );
  };

  const renderIdea = (idea: IdeaItemType) => (
    <li
      key={idea.id}
      className={`px-4 py-3 border border-white/10 rounded-lg bg-white/[0.02] ${
        idea.status === "archived" ? "opacity-60" : ""
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-white">{idea.title}</span>
            {idea.team_name && (
              <span className="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-medium bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                🏷 {idea.team_name}
              </span>
            )}
            {statusPill(idea.status, idea.used_at)}
          </div>
          {idea.description && <p className="text-sm text-gray-400 mt-1">{idea.description}</p>}
          {idea.prompt && (
            <details className="mt-2">
              <summary className="text-xs text-gray-500 cursor-pointer hover:text-gray-300">
                View saved prompt ({idea.prompt.length} chars)
              </summary>
              <pre className="mt-2 text-xs text-gray-300 whitespace-pre-wrap bg-black/25 border border-white/10 rounded-md p-3">
                {idea.prompt}
              </pre>
            </details>
          )}
          {builtPrompt && builtPrompt.id === idea.id && (
            <div className="mt-2 border border-emerald-500/30 bg-emerald-500/5 rounded-md p-3">
              <div className="flex items-center justify-between mb-2 gap-2">
                <span className="text-xs font-medium text-emerald-300">Generated prompt ready</span>
                <button
                  onClick={() => onUsePrompt(builtPrompt.prompt, sport)}
                  className="text-xs px-2 py-1 rounded-md bg-emerald-600 hover:bg-emerald-500 text-white font-medium"
                >
                  Use in Create Article →
                </button>
              </div>
              <pre className="text-xs text-gray-200 whitespace-pre-wrap">{builtPrompt.prompt}</pre>
            </div>
          )}
        </div>
        <div className="flex flex-col gap-1 shrink-0">
          <button
            onClick={() => sendToCreate(idea)}
            className="text-xs px-2 py-1 rounded-md border border-emerald-500/40 bg-emerald-600/20 hover:bg-emerald-600/40 text-emerald-300 font-medium"
          >
            To Create Article →
          </button>
          <button
            onClick={() => buildPrompt(idea.id)}
            disabled={buildingId === idea.id}
            className="text-xs px-2 py-1 rounded-md bg-blue-600 hover:bg-blue-500 text-white font-medium disabled:opacity-50"
          >
            {buildingId === idea.id ? "Building…" : "Build prompt"}
          </button>
          {idea.status !== "used" && (
            <button
              onClick={() => setIdeaStatus(idea, "used")}
              className="text-xs px-2 py-1 rounded-md bg-green-600 hover:bg-green-500 text-white font-medium"
            >
              Mark used
            </button>
          )}
          {idea.status === "archived" ? (
            <button
              onClick={() => setIdeaStatus(idea, "active")}
              className="text-xs px-2 py-1 rounded-md bg-gray-600 hover:bg-gray-500 text-white"
            >
              Restore
            </button>
          ) : (
            <button
              onClick={() => setIdeaStatus(idea, "archived")}
              className="text-xs px-2 py-1 rounded-md bg-gray-700 hover:bg-gray-600 text-gray-200"
            >
              Archive
            </button>
          )}
          <button
            onClick={() => deleteIdea(idea)}
            className="text-xs px-2 py-1 rounded-md bg-red-900/40 hover:bg-red-800/60 text-red-300 border border-red-500/20"
          >
            Delete
          </button>
        </div>
      </div>
    </li>
  );

  return (
    <div>
      {/* Brainstorm box */}
      <div className="border border-white/10 rounded-lg p-4 mb-4 bg-white/[0.02]">
        <h2 className="text-sm font-semibold text-gray-200 mb-1">
          Brainstorm article ideas — {SPORT_LABEL[sport]}
        </h2>
        <p className="text-xs text-gray-500 mb-3">
          Ask the LLM to come up with article ideas. It first researches our news database,
          standings, stats, and injuries, then proposes grounded ideas. Add your own guidance
          (teams, angles, players, storylines), then generate and store the ones you like.
        </p>
        <textarea
          value={instructions}
          onChange={(e) => setInstructions(e.target.value)}
          placeholder={`e.g. Focus on ${
            teams.length ? (teams[0]?.name ?? "our local team") : "our local team"
          } losing skid, pitching matchups, and a contrarian betting angle this week…`}
          rows={3}
          className="w-full rounded-md bg-black/30 border border-white/10 text-sm text-white placeholder-gray-600 px-3 py-2 focus:outline-none focus:ring-1 focus:ring-blue-500"
        />
        <button
          onClick={generate}
          disabled={generating}
          className="mt-2 text-sm px-3 py-2 rounded-md bg-blue-600 hover:bg-blue-500 text-white font-medium disabled:opacity-50"
        >
          {generating
            ? (quickMode ? "Thinking…" : "Researching…")
            : "Generate ideas"}
        </button>
        <label className="mt-2 ml-2 inline-flex items-center gap-2 text-xs text-gray-400 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={quickMode}
            onChange={(e) => setQuickMode(e.target.checked)}
            className="accent-blue-500"
          />
          Quick mode (faster, no research tools)
        </label>
        {draftMsg && <div className="mt-2 text-xs text-gray-400">{draftMsg}</div>}
      </div>

      {/* Draft (generated, not yet stored) ideas */}
      {draftIdeas.length > 0 && (
        <div className="border border-yellow-500/30 bg-yellow-500/5 rounded-lg p-4 mb-4">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-sm font-semibold text-yellow-200">Generated preview ({draftIdeas.length})</h3>
            <button
              onClick={storeAll}
              disabled={storing}
              className="text-xs px-3 py-1.5 rounded-md bg-yellow-600 hover:bg-yellow-500 text-white font-medium disabled:opacity-50"
            >
              {storing ? "Storing…" : `Store all ${draftIdeas.length}`}
            </button>
          </div>
          <ul className="space-y-2">
            {draftIdeas.map((idea, i) => (
              <li key={i} className="px-3 py-2 bg-black/20 border border-white/10 rounded-md">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-white">
                      {idea.title}
                      {idea.team_name && (
                        <span className="ml-2 inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                          🏷 {idea.team_name}
                        </span>
                      )}
                    </div>
                    {idea.description && <p className="text-xs text-gray-400 mt-1">{idea.description}</p>}
                  </div>
                  <div className="flex flex-col gap-1 shrink-0">
                    <button
                      onClick={() => storeOne(idea)}
                      disabled={storing}
                      className="text-xs px-2 py-1 rounded-md bg-yellow-600 hover:bg-yellow-500 text-white font-medium disabled:opacity-50"
                    >
                      {storing ? "…" : "Save"}
                    </button>
                    <button
                      onClick={() =>
                        onUsePrompt(
                          [
                            `Write an article about: ${idea.title}${idea.team_name ? ` (${idea.team_name})` : ""}`,
                            idea.description || "",
                          ]
                            .filter(Boolean)
                            .join("\n\n"),
                          sport
                        )
                      }
                      className="text-xs px-2 py-1 rounded-md border border-emerald-500/40 bg-emerald-600/20 hover:bg-emerald-600/40 text-emerald-300 font-medium"
                    >
                      To Create Article →
                    </button>
                  </div>
                </div>
              </li>
            ))}
          </ul>
          <button
            onClick={() => {
              setDraftIdeas([]);
              setGenerated(false);
            }}
            className="mt-2 text-xs text-gray-400 hover:text-gray-200"
          >
            Discard preview
          </button>
        </div>
      )}

      {/* Stored ideas */}
      <h3 className="text-sm font-semibold text-gray-300 mb-2">Stored ideas — {SPORT_LABEL[sport]}</h3>
      {loadingIdeas ? (
        <div className="text-sm text-gray-500">Loading…</div>
      ) : ideas.length === 0 ? (
        <div className="text-sm text-gray-500 bg-white/[0.03] border border-white/10 rounded-lg p-4">
          No article ideas stored for {SPORT_LABEL[sport]} yet.{generated ? "" : " Generate some above."}
        </div>
      ) : (
        <ul className="space-y-2">{ideas.map(renderIdea)}</ul>
      )}
    </div>
  );
}

function ResearchPanel({
  loading,
  detail,
  hasResearch,
  onClose,
}: {
  loading: boolean;
  detail: ArticleDetail | null;
  hasResearch: boolean;
  onClose: () => void;
}) {
  const prompt = detail?.prompt_json;
  const research = detail?.research_json;

  return (
    <div className="border-t border-white/10 bg-black/25 p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-medium text-gray-200">Prompt + Research</span>
        <button onClick={onClose} className="text-xs text-gray-400 hover:text-gray-200">
          Close
        </button>
      </div>

      {loading ? (
        <div className="text-sm text-gray-500">Loading…</div>
      ) : !hasResearch && !prompt ? (
        <div className="text-sm text-gray-500">No prompt or research stored for this article.</div>
      ) : (
        <div className="space-y-4 text-sm">
          {/* Prompt */}
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">Prompt</div>
            {prompt && prompt.length ? (
              <div className="space-y-2">
                {prompt.map((m: any, i: number) => (
                  <div key={i} className="bg-white/[0.03] border border-white/10 rounded p-2">
                    <div className="text-[11px] uppercase text-gray-500 mb-1">{m.role}</div>
                    <pre className="text-xs text-gray-300 whitespace-pre-wrap font-sans">
                      {typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2)}
                    </pre>
                    {m.has_tool_calls ? (
                      <div className="text-[11px] text-purple-300 mt-1">
                        {m.has_tool_calls} research call(s)
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-gray-500">No prompt stored.</div>
            )}
          </div>

          {/* Research trace */}
          <div>
            <div className="text-xs uppercase tracking-wide text-gray-500 mb-1">
              Research performed
            </div>
            {research && research.length ? (
              <ol className="space-y-2 list-none">
                {research.map((step: any, i: number) => (
                  <li key={i} className="bg-white/[0.03] border border-white/10 rounded p-2">
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[11px] text-gray-500">{i + 1}.</span>
                      <span className="text-xs font-medium text-purple-300">
                        {step.tool}
                      </span>
                    </div>
                    {step.arguments && Object.keys(step.arguments).length > 0 && (
                      <pre className="text-xs text-gray-400 whitespace-pre-wrap font-sans mb-1">
                        {JSON.stringify(step.arguments)}
                      </pre>
                    )}
                    <div className="text-xs text-gray-300">
                      <span className="text-gray-500">→ </span>
                      {typeof step.result === "string" ? (
                        step.result
                      ) : (
                        <pre className="text-xs text-gray-300 whitespace-pre-wrap font-sans inline">
                          {JSON.stringify(step.result, null, 2)}
                        </pre>
                      )}
                    </div>
                  </li>
                ))}
              </ol>
            ) : (
              <div className="text-xs text-gray-500">No research stored.</div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
