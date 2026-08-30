"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

const SPORTS = ["all", "mlb", "nfl", "nba"] as const;
type Sport = (typeof SPORTS)[number];
const SPORT_LABEL: Record<Sport, string> = {
  all: "All Sports",
  mlb: "MLB",
  nfl: "NFL",
  nba: "NBA",
};

const CADENCES = ["daily", "weekly"] as const;
const SCOPES = ["sport", "team"] as const;
const SECTIONS = ["article", "daily_picks"] as const;
const SECTION_LABEL: Record<string, string> = {
  article: "Articles",
  daily_picks: "Daily Picks",
};

interface Team {
  id: number;
  name: string;
  abbreviation: string | null;
}

interface Config {
  id: number;
  sport: Sport;
  title: string;
  description: string | null;
  instructions: string | null;
  cadence: "daily" | "weekly";
  scope_type: "sport" | "team";
  team_id: number | null;
  team_abbr: string | null;
  team_name: string | null;
  template_article_id: number | null;
  section: "article" | "daily_picks";
  status: "active" | "inactive" | "paused";
  reasoning: "minimal" | "low" | "medium" | "high" | "xhigh";
  visibility: "public" | "premium";
  word_min: number | null;
  word_max: number | null;
  title_mode: "fixed" | "llm";
  recency_context: boolean;
  generate_time: string | null;
  last_generated_at: string | null;
  created_at: string;
  updated_at: string;
}

const token = () => localStorage.getItem("earl_token");
const JSON_HEADERS = { "Content-Type": "application/json" };
const authHeaders = (extra: Record<string, string> = {}) => {
  const t = token();
  return { ...JSON_HEADERS, ...(t ? { Authorization: `Bearer ${t}` } : {}), ...extra };
};

interface ConfigFormState {
  sport: Sport;
  title: string;
  description: string;
  instructions: string;
  cadence: "daily" | "weekly";
  generate_time: string;
  scope_type: "sport" | "team";
  team_id: number | null;
  section: "article" | "daily_picks";
  status: "active" | "inactive" | "paused";
  reasoning: "minimal" | "low" | "medium" | "high" | "xhigh";
  visibility: "public" | "premium";
  word_min: number;
  word_max: number;
  title_mode: "fixed" | "llm";
  recency_context: boolean;
}

const EMPTY_FORM: ConfigFormState = {
  sport: "mlb",
  title: "",
  description: "",
  instructions: "",
  cadence: "daily",
  generate_time: "",
  scope_type: "sport",
  team_id: null,
  section: "article",
  status: "active",
  reasoning: "medium",
  visibility: "public",
  word_min: 400,
  word_max: 700,
  title_mode: "fixed",
  recency_context: false,
};

const REASONING_LABEL = {
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
} as const;

const WORD_RANGES: { value: string; label: string; lo: number; hi: number }[] = [
  { value: "300–500", label: "Short (~300–500 words)", lo: 300, hi: 500 },
  { value: "400–700", label: "Medium (~400–700 words)", lo: 400, hi: 700 },
  { value: "700–1100", label: "Long (~700–1,100 words)", lo: 700, hi: 1100 },
  { value: "1100–1600", label: "Very long (~1,100–1,600 words)", lo: 1100, hi: 1600 },
  { value: "1600–2200", label: "Deep dive (~1,600–2,200 words)", lo: 1600, hi: 2200 },
];

const rangeKey = (lo: number | null, hi: number | null) => `${lo ?? 400}–${hi ?? 700}`;

const fmtRange = (lo: number | null, hi: number | null) => {
  if (lo == null || hi == null) return "400–700";
  return `${lo}–${hi}`;
};

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function CadenceBadge({ cadence }: { cadence: "daily" | "weekly" }) {
  return cadence === "daily" ? (
    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-sky-500/15 text-sky-400 border border-sky-500/20">
      Daily
    </span>
  ) : (
    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-violet-500/15 text-violet-400 border border-violet-500/20">
      Weekly
    </span>
  );
}

function StatusBadge({ status }: { status: Config["status"] }) {
  const map = {
    active: { cls: "bg-emerald-500/15 text-emerald-400 border-emerald-500/20", label: "Active" },
    inactive: { cls: "bg-gray-500/15 text-gray-400 border-gray-500/20", label: "Inactive" },
    paused: { cls: "bg-amber-500/15 text-amber-400 border-amber-500/20", label: "Paused" },
  } as const;
  const m = map[status];
  return (
    <span className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full border ${m.cls}`}>
      {m.label}
    </span>
  );
}

export default function AutoGenerationPage() {
  useSeo({ title: "Auto Generation · Earl Admin" });

  const [configs, setConfigs] = useState<Config[]>([]);
  const [teams, setTeams] = useState<Record<Sport, Team[]>>({ mlb: [], nfl: [], nba: [], all: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [sportFilter, setSportFilter] = useState<"*" | Sport>("*");
  const [cadenceFilter, setCadenceFilter] = useState<"all" | "daily" | "weekly">("all");
  const [scopeFilter, setScopeFilter] = useState<"all" | "sport" | "team">("all");

  // Create / edit modal
  const [modalOpen, setModalOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<ConfigFormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [savingMsg, setSavingMsg] = useState<string | null>(null);

  const loadConfigs = useCallback(async () => {
    try {
      setError(null);
      const res = await fetch("/api/admin/auto-generation", { headers: authHeaders() });
      if (!res.ok) throw new Error(`Failed to load (${res.status})`);
      const data = await res.json();
      setConfigs(data);
    } catch (e: any) {
      setError(e?.message || "Failed to load configurations.");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadTeams = useCallback(async (sport: Sport) => {
    try {
      const res = await fetch(`/api/admin/auto-generation/teams/${sport}`, { headers: authHeaders() });
      if (!res.ok) return;
      const data = await res.json();
      setTeams((prev) => ({ ...prev, [sport]: data }));
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    loadConfigs();
  }, [loadConfigs]);

  useEffect(() => {
    SPORTS.forEach((s) => loadTeams(s));
  }, [loadTeams]);

  const openCreate = () => {
    setEditingId(null);
    setForm({ ...EMPTY_FORM });
    setModalOpen(true);
  };

  const openEdit = (cfg: Config) => {
    setEditingId(cfg.id);
    setForm({
      sport: cfg.sport,
      title: cfg.title,
      description: cfg.description || "",
      instructions: cfg.instructions || "",
      cadence: cfg.cadence,
      generate_time: cfg.generate_time || "",
      scope_type: cfg.scope_type,
      section: cfg.section || "article",
      team_id: cfg.team_id,
      status: cfg.status,
      reasoning: cfg.reasoning,
      visibility: cfg.visibility,
      word_min: cfg.word_min ?? 400,
      word_max: cfg.word_max ?? 700,
      title_mode: cfg.title_mode || "fixed",
      recency_context: !!cfg.recency_context,
    });
    setModalOpen(true);
  };

  const handleSave = async () => {
    if (!form.title.trim()) {
      setSavingMsg("Title is required.");
      return;
    }
    setSaving(true);
    setSavingMsg(null);
    try {
      const body = {
        sport: form.sport,
        title: form.title.trim(),
        description: form.description || null,
        instructions: form.instructions || null,
        cadence: form.cadence,
        generate_time: form.generate_time || null,
        scope_type: form.sport === "all" ? "sport" : form.scope_type,
        section: form.section,
        team_id: form.sport === "all" ? null : form.scope_type === "team" ? form.team_id : null,
        status: form.status,
        reasoning: form.reasoning,
        visibility: form.visibility,
        word_min: form.word_min,
        word_max: form.word_max,
        title_mode: form.title_mode,
        recency_context: !!form.recency_context,
      };
      const isEdit = editingId !== null;
      const res = await fetch(
        isEdit ? `/api/admin/auto-generation/${editingId}` : "/api/admin/auto-generation",
        {
          method: isEdit ? "PATCH" : "POST",
          headers: authHeaders(),
          body: JSON.stringify(body),
        }
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data?.detail || `Failed to save (${res.status})`);
      await loadConfigs();
      setModalOpen(false);
    } catch (e: any) {
      setSavingMsg(e?.message || "Save failed.");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (cfg: Config) => {
    if (!window.confirm(`Delete auto-generation config "${cfg.title}"?`)) return;
    try {
      const res = await fetch(`/api/admin/auto-generation/${cfg.id}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data?.detail || `Delete failed (${res.status})`);
      await loadConfigs();
    } catch (e: any) {
      window.alert(e?.message || "Delete failed.");
    }
  };

  const filtered = configs.filter((c) => {
    if (sportFilter !== "*" && c.sport !== sportFilter) return false;
    if (cadenceFilter !== "all" && c.cadence !== cadenceFilter) return false;
    if (scopeFilter !== "all" && c.scope_type !== scopeFilter) return false;
    return true;
  });

  const teamOptions = teams[form.sport] || [];
  const activeCount = configs.filter((c) => c.status === "active").length;

  return (
    <div>
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold text-white">Auto Generation</h1>
          <p className="text-sm text-gray-400 mt-1">
            Manage continuously-generated articles — daily &amp; weekly, team-specific or sport-wide.
          </p>
        </div>
        <button
          onClick={openCreate}
          className="inline-flex items-center gap-2 px-4 py-2 rounded-lg bg-earl-600 hover:bg-earl-500 text-white text-sm font-semibold transition"
        >
          <svg xmlns="http://www.w3.org/2000/svg" className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v16m8-8H4" />
          </svg>
          New Config
        </button>
      </div>

      {error && (
        <div className="mb-4 px-4 py-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-300 text-sm">
          {error}
        </div>
      )}

      {/* Summary stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        {[
          { label: "Total Configs", value: configs.length },
          { label: "Active", value: activeCount },
          { label: "Daily", value: configs.filter((c) => c.cadence === "daily").length },
          { label: "Weekly", value: configs.filter((c) => c.cadence === "weekly").length },
        ].map((s) => (
          <div key={s.label} className="rounded-xl bg-black/30 border border-white/10 p-4">
            <div className="text-xs uppercase tracking-wider text-gray-500">{s.label}</div>
            <div className="text-2xl font-bold text-white mt-1">{s.value}</div>
          </div>
        ))}
      </div>

      {/* Filters */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <FilterChip active={sportFilter === "*"} onClick={() => setSportFilter("*")}>All Sports</FilterChip>
        {SPORTS.map((s) => (
          <FilterChip key={s} active={sportFilter === s} onClick={() => setSportFilter(s)}>
            {SPORT_LABEL[s]}
          </FilterChip>
        ))}
        <div className="w-px h-5 bg-white/10 mx-1" />
        <FilterChip active={cadenceFilter === "all"} onClick={() => setCadenceFilter("all")}>All Cadence</FilterChip>
        <FilterChip active={cadenceFilter === "daily"} onClick={() => setCadenceFilter("daily")}>Daily</FilterChip>
        <FilterChip active={cadenceFilter === "weekly"} onClick={() => setCadenceFilter("weekly")}>Weekly</FilterChip>
        <div className="w-px h-5 bg-white/10 mx-1" />
        <FilterChip active={scopeFilter === "all"} onClick={() => setScopeFilter("all")}>All Scopes</FilterChip>
        <FilterChip active={scopeFilter === "sport"} onClick={() => setScopeFilter("sport")}>Sport-Wide</FilterChip>
        <FilterChip active={scopeFilter === "team"} onClick={() => setScopeFilter("team")}>Team-Specific</FilterChip>
      </div>

      {/* Config list */}
      {loading ? (
        <div className="text-center py-20 text-gray-400">Loading configurations…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-20">
          <div className="text-gray-500">No configurations match.</div>
          <button onClick={openCreate} className="mt-3 text-earl-400 hover:text-earl-300 text-sm">
            + Create your first Auto Generation config
          </button>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((cfg) => {
            const isTeam = cfg.scope_type === "team";
            return (
              <div
                key={cfg.id}
                className="rounded-xl bg-black/30 border border-white/10 p-4 flex flex-col md:flex-row md:items-center gap-4"
              >
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs font-semibold uppercase tracking-wide text-gray-400 px-2 py-0.5 rounded bg-white/5 border border-white/10">
                      {SPORT_LABEL[cfg.sport]}
                    </span>
                    <CadenceBadge cadence={cfg.cadence} />
                    {cfg.generate_time && (
                      <span
                        className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-sky-500/15 text-sky-300 border border-sky-500/20"
                        title="Generate time of day (America/Chicago)"
                      >
                        ⏰ {cfg.generate_time}
                      </span>
                    )}
                    <StatusBadge status={cfg.status} />
                    {cfg.section && cfg.section !== "article" && (
                      <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-amber-500/15 text-amber-400 border border-amber-500/20">
                        {SECTION_LABEL[cfg.section] ?? cfg.section}
                      </span>
                    )}
                    {isTeam && (
                      <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-blue-500/15 text-blue-400 border border-blue-500/20">
                        {cfg.team_name || cfg.team_abbr || "Team"}
                      </span>
                    )}
                    {!isTeam && (
                      <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-purple-500/15 text-purple-400 border border-purple-500/20">
                        {cfg.sport === "all" ? "Site-Wide" : "Sport-Wide"}
                      </span>
                    )}
                    {cfg.reasoning && cfg.reasoning !== "medium" && (
                      <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-teal-500/15 text-teal-400 border border-teal-500/20">
                        {REASONING_LABEL[cfg.reasoning] ?? cfg.reasoning}
                      </span>
                    )}
                    <span
                      className={`inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full border ${
                        cfg.visibility === "premium"
                          ? "bg-amber-500/15 text-amber-400 border-amber-500/20"
                          : "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                      }`}
                    >
                      {cfg.visibility === "premium" ? "Premium" : "Public"}
                    </span>
                    <span className="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded-full bg-white/5 text-gray-300 border border-white/10">
                      {fmtRange(cfg.word_min, cfg.word_max)} words
                    </span>
                  </div>
                  <div className="flex items-center gap-2 mt-2">
                    <h3 className="text-white font-semibold truncate">{cfg.title}</h3>
                    {cfg.title_mode === "llm" && (
                      <span
                        className="inline-flex items-center px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide rounded bg-white/5 border border-white/10 text-gray-400 shrink-0"
                        title="LLM generates a new title each run"
                      >
                        ✨ LLM Title
                      </span>
                    )}
                    {cfg.recency_context && (
                      <span
                        className="inline-flex items-center px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide rounded bg-earl-500/15 border border-earl-500/25 text-earl-300 shrink-0"
                        title="Feeds back previously-published articles so content stays fresh"
                      >
                        🔄 Fresh content
                      </span>
                    )}
                  </div>
                  {cfg.description && (
                    <p className="text-sm text-gray-400 mt-1 line-clamp-2">{cfg.description}</p>
                  )}
                  <div className="text-xs text-gray-500 mt-2 flex flex-wrap gap-x-4 gap-y-1">
                    <span>Created {fmtDate(cfg.created_at)}</span>
                    {cfg.last_generated_at && <span>Last generated {fmtDate(cfg.last_generated_at)}</span>}
                    {cfg.template_article_id && <span>From article #{cfg.template_article_id}</span>}
                  </div>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <button
                    onClick={() => openEdit(cfg)}
                    className="px-3 py-1.5 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-sm text-gray-200 transition"
                  >
                    Edit
                  </button>
                  <button
                    onClick={() => handleDelete(cfg)}
                    className="px-3 py-1.5 rounded-lg bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 text-sm text-red-300 transition"
                  >
                    Delete
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* Create / Edit modal */}
      {modalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4" onClick={() => !saving && setModalOpen(false)}>
          <div
            className="w-full max-w-2xl max-h-[90vh] overflow-y-auto rounded-2xl bg-[#12121a] border border-white/10 p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-5">
              <h2 className="text-xl font-bold text-white">
                {editingId ? "Edit Auto Generation Config" : "New Auto Generation Config"}
              </h2>
              <button
                onClick={() => setModalOpen(false)}
                className="text-gray-400 hover:text-white text-2xl leading-none"
                aria-label="Close"
              >
                ×
              </button>
            </div>

            <ContentField label="Sport">
              <select
                value={form.sport}
                onChange={(e) => {
                  const s = e.target.value as Sport;
                  setForm({
                    ...form,
                    sport: s,
                    scope_type: s === "all" ? "sport" : form.scope_type,
                    team_id: s === "all" ? null : form.team_id,
                  });
                }}
                className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
              >
                {SPORTS.map((s) => (
                  <option key={s} value={s}>{SPORT_LABEL[s]}</option>
                ))}
              </select>
            </ContentField>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <ContentField label="Title" required>
                <input
                  type="text"
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                  placeholder="e.g. Weekly MLB Power Rankings"
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                />
              </ContentField>
              <ContentField label="Title Mode">
                <select
                  value={form.title_mode}
                  onChange={(e) => setForm({ ...form, title_mode: e.target.value as "fixed" | "llm" })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  <option value="fixed">Fixed title (always the same)</option>
                  <option value="llm">LLM-generated title (varies each run)</option>
                </select>
              </ContentField>
              <ContentField label="">
                <label className="flex items-start gap-3 pt-6 cursor-pointer select-none">
                  <input
                    type="checkbox"
                    checked={form.recency_context}
                    onChange={(e) => setForm({ ...form, recency_context: e.target.checked })}
                    className="mt-0.5 h-4 w-4 accent-earl-500"
                  />
                  <span className="text-sm text-gray-300 leading-snug">
                    Send previous-coverage context
                    <span className="block text-xs text-gray-500 mt-0.5">
                      Feeds back recently-published articles so each run is fresh &amp; non-repetitive
                    </span>
                  </span>
                </label>
              </ContentField>
              <ContentField label="Description">
                <input
                  type="text"
                  value={form.description || ""}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                  placeholder="Short summary of this series"
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                />
              </ContentField>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <ContentField label="Cadence">
                <select
                  value={form.cadence}
                  onChange={(e) => setForm({ ...form, cadence: e.target.value as "daily" | "weekly" })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  {CADENCES.map((c) => (
                    <option key={c} value={c}>{c.charAt(0).toUpperCase() + c.slice(1)}</option>
                  ))}
                </select>
              </ContentField>
              <ContentField
                label="Generate Time"
                hint={"Optional. When set, the article is due once per " + form.cadence + " cycle at this clock time (America/Chicago). Leave blank for a rolling " + (form.cadence === "daily" ? "24h" : "7d") + " window."}
              >
                <input
                  type="time"
                  value={form.generate_time}
                  onChange={(e) => setForm({ ...form, generate_time: e.target.value })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500 [color-scheme:dark]"
                />
              </ContentField>
              <ContentField label="Scope">
                <select
                  value={form.scope_type}
                  onChange={(e) =>
                    form.sport === "all"
                      ? undefined
                      : setForm({ ...form, scope_type: e.target.value as "sport" | "team" })
                  }
                  disabled={form.sport === "all"}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500 disabled:opacity-50"
                >
                  <option value="sport">
                    {form.sport === "all" ? "Site-wide (general)" : "Sport-Wide (general)"}
                  </option>
                  {form.sport !== "all" && <option value="team">Team-Specific</option>}
                </select>
              </ContentField>
            </div>

            <ContentField label="Section" required>
              <select
                value={form.section}
                onChange={(e) => setForm({ ...form, section: e.target.value as "article" | "daily_picks" })}
                className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
              >
                {SECTIONS.map((s) => (
                  <option key={s} value={s}>{SECTION_LABEL[s]}</option>
                ))}
              </select>
            </ContentField>

            {form.scope_type === "team" && (
              <ContentField label="Team">
                <select
                  value={form.team_id ?? ""}
                  onChange={(e) => setForm({ ...form, team_id: e.target.value ? Number(e.target.value) : null })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  <option value="">Select a team…</option>
                  {teamOptions.map((t) => (
                    <option key={t.id} value={t.id}>{t.name}</option>
                  ))}
                </select>
              </ContentField>
            )}

            <ContentField label="Status">
              <select
                value={form.status}
                onChange={(e) => setForm({ ...form, status: e.target.value as Config["status"] })}
                className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
              >
                <option value="active">Active</option>
                <option value="inactive">Inactive</option>
                <option value="paused">Paused</option>
              </select>
            </ContentField>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <ContentField label="Reasoning Lever">
                <select
                  value={form.reasoning}
                  onChange={(e) => setForm({ ...form, reasoning: e.target.value as ConfigFormState["reasoning"] })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  {Object.entries(REASONING_LABEL).map(([val, label]) => (
                    <option key={val} value={val}>{label}</option>
                  ))}
                </select>
              </ContentField>
              <ContentField label="Visibility">
                <select
                  value={form.visibility}
                  onChange={(e) => setForm({ ...form, visibility: e.target.value as ConfigFormState["visibility"] })}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  <option value="public">Public (FREE — no betting advice)</option>
                  <option value="premium">Premium (members — betting advice OK)</option>
                </select>
              </ContentField>
              <ContentField label="Length">
                <select
                  value={rangeKey(form.word_min, form.word_max)}
                  onChange={(e) => {
                    const opt = WORD_RANGES.find((r) => r.value === e.target.value);
                    if (opt) setForm({ ...form, word_min: opt.lo, word_max: opt.hi });
                  }}
                  className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500"
                >
                  {WORD_RANGES.map((r) => (
                    <option key={r.value} value={r.value}>{r.label}</option>
                  ))}
                </select>
              </ContentField>
            </div>

            <ContentField label="Instructions (generation prompt)">
              <textarea
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                rows={5}
                placeholder="Instructions the generator uses to write this article every run…"
                className="w-full px-3 py-2 rounded-lg bg-black/40 border border-white/10 text-white text-sm focus:outline-none focus:border-earl-500 resize-y"
              />
            </ContentField>

            {savingMsg && (
              <div className="mt-3 text-sm text-red-300">{savingMsg}</div>
            )}

            <div className="mt-6 flex items-center justify-end gap-3">
              <button
                onClick={() => setModalOpen(false)}
                disabled={saving}
                className="px-4 py-2 rounded-lg bg-white/5 hover:bg-white/10 border border-white/10 text-gray-200 text-sm transition disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-4 py-2 rounded-lg bg-earl-600 hover:bg-earl-500 text-white text-sm font-semibold transition disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {saving ? "Saving…" : editingId ? "Save Changes" : "Create Config"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-3 py-1.5 rounded-full text-sm font-medium transition border ${
        active
          ? "bg-earl-600/20 text-earl-400 border-earl-600/40"
          : "text-gray-400 hover:text-white hover:bg-white/5 border-white/10"
      }`}
    >
      {children}
    </button>
  );
}

function ContentField({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-4">
      <label className="block text-xs uppercase tracking-wider text-gray-500 mb-1.5">
        {label} {required && <span className="text-earl-400">*</span>}
      </label>
      {children}
      {hint && <p className="mt-1 text-xs text-gray-500">{hint}</p>}
    </div>
  );
}
