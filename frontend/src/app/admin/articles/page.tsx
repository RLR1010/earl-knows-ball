"use client";

import { useEffect, useState, useCallback } from "react";

const SPORTS = ["nfl", "nba", "mlb"] as const;
type Sport = (typeof SPORTS)[number];

interface ArticleStats {
  total: number;
  embedded: number;
  unembedded: number;
  with_body: number;
  null_published_at: number;
  by_source: { source: string; count: number }[];
  by_year: { year: number; count: number }[];
}

interface Article {
  id: number;
  title: string;
  slug: string;
  excerpt: string | null;
  category: string | null;
  published_at: string | null;
  created_at: string | null;
  author: string | null;
  source_url: string | null;
  source_name: string | null;
  source_type: string | null;
  embedded_at: string | null;
}

const token = () => localStorage.getItem("earl_token");

function StatBox({ label, value, color }: { label: string; value: string | number; color?: string }) {
  return (
    <div className="bg-white/[0.03] border border-white/10 rounded-lg p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-0.5">{label}</div>
      <div className={`text-xl font-bold ${color || "text-white"}`}>{typeof value === "number" ? value.toLocaleString() : value}</div>
    </div>
  );
}

export default function AdminArticles() {
  const [sport, setSport] = useState<Sport>("nfl");
  const [stats, setStats] = useState<ArticleStats | null>(null);
  const [articles, setArticles] = useState<Article[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState("");
  const [categoryFilter, setCategoryFilter] = useState("");

  const fetchStats = useCallback(async () => {
    try {
      const res = await fetch(`/api/admin/articles/${sport}/stats`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) setStats(await res.json());
    } catch {}
  }, [sport]);

  const fetchArticles = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (sourceFilter) params.set("source", sourceFilter);
      if (categoryFilter) params.set("category", categoryFilter);
      params.set("limit", "50");
      const res = await fetch(`/api/admin/articles/${sport}?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) setArticles(await res.json());
    } catch {} finally {
      setLoading(false);
    }
  }, [sport, search, sourceFilter, categoryFilter]);

  useEffect(() => { fetchStats(); }, [fetchStats]);
  useEffect(() => { fetchArticles(); }, [fetchArticles]);

  const handleDelete = async (articleId: number, title: string) => {
    if (!confirm(`Delete article "${title.substring(0, 60)}..."? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/articles/${sport}/${articleId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fetchArticles();
      fetchStats();
    } catch (e: any) {
      alert(`Failed to delete: ${e.message}`);
    }
  };

  const sportLabel = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Articles</h1>
        <p className="text-gray-400 text-sm mt-1">Browse, search, and manage articles</p>
      </div>

      {/* Sport selector */}
      <div className="flex gap-2 mb-6">
        {SPORTS.map((s) => (
          <button
            key={s}
            onClick={() => { setSport(s); setSearch(""); setSourceFilter(""); setCategoryFilter(""); }}
            className={`px-5 py-2 rounded-lg text-sm font-semibold transition border ${
              sport === s
                ? "bg-earl-600/20 text-earl-400 border-earl-600/30"
                : "bg-white/5 text-gray-400 border-white/10 hover:text-white hover:bg-white/10"
            }`}
          >
            {sportLabel[s]}
          </button>
        ))}
      </div>

      {/* Stats row */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
          <StatBox label="Total" value={stats.total} color="text-blue-400" />
          <StatBox label="Embedded" value={stats.embedded} color="text-green-400" />
          <StatBox label="Unembedded" value={stats.unembedded} color="text-yellow-400" />
          <StatBox label="With Body" value={stats.with_body} color="text-purple-400" />
          <StatBox label="Null Pub Date" value={stats.null_published_at} color="text-red-400" />
        </div>
      )}

      {/* Source breakdown */}
      {stats && stats.by_source.length > 0 && (
        <details className="mb-6 bg-white/[0.02] border border-white/10 rounded-lg">
          <summary className="px-4 py-3 text-sm text-gray-400 cursor-pointer hover:text-white font-medium">
            Top Sources ({stats.by_source.length})
          </summary>
          <div className="px-4 pb-3 max-h-60 overflow-y-auto space-y-1">
            {stats.by_source.map((s) => (
              <button
                key={s.source}
                onClick={() => setSourceFilter(sourceFilter === s.source ? "" : s.source)}
                className={`w-full flex justify-between items-center px-3 py-1.5 rounded text-xs transition ${
                  sourceFilter === s.source ? "bg-earl-600/20 text-earl-400" : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
                }`}
              >
                <span>{s.source}</span>
                <span className="font-mono">{s.count.toLocaleString()}</span>
              </button>
            ))}
          </div>
        </details>
      )}

      {/* Search & filters */}
      <div className="flex flex-wrap gap-3 mb-6">
        <input
          type="text"
          placeholder="Search title, source, author..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="flex-1 min-w-[200px] px-4 py-2.5 bg-black/40 border border-white/10 rounded-lg text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-600/50"
        />
        <select
          value={categoryFilter}
          onChange={(e) => setCategoryFilter(e.target.value)}
          className="px-4 py-2.5 bg-black/40 border border-white/10 rounded-lg text-sm text-gray-300 focus:outline-none focus:border-earl-600/50"
        >
          <option value="">All categories</option>
          <option value="news">News</option>
          <option value="analysis">Analysis</option>
          <option value="game_preview">Game Preview</option>
          <option value="game_recap">Game Recap</option>
          <option value="fantasy_advice">Fantasy</option>
          <option value="betting_pick">Betting</option>
          <option value="team_analysis">Team Analysis</option>
          <option value="general">General</option>
        </select>
        {sourceFilter && (
          <span className="inline-flex items-center gap-1 px-3 py-2 bg-earl-600/10 border border-earl-600/30 rounded-lg text-xs text-earl-400">
            Source: {sourceFilter}
            <button onClick={() => setSourceFilter("")} className="ml-1 hover:text-white">&times;</button>
          </span>
        )}
      </div>

      {/* Article list */}
      {loading ? (
        <div className="text-gray-400 text-sm">Loading articles...</div>
      ) : articles.length === 0 ? (
        <div className="text-gray-500 text-sm py-8 text-center">No articles found</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-white/10 text-gray-500 text-xs uppercase tracking-wider">
                <th className="text-left py-3 px-3 font-semibold">ID</th>
                <th className="text-left py-3 px-3 font-semibold">Title</th>
                <th className="text-left py-3 px-3 font-semibold">Source</th>
                <th className="text-left py-3 px-3 font-semibold">Category</th>
                <th className="text-left py-3 px-3 font-semibold">Published</th>
                <th className="text-left py-3 px-3 font-semibold">Author</th>
                <th className="text-center py-3 px-3 font-semibold">Embedded</th>
                <th className="text-center py-3 px-3 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {articles.map((a) => (
                <tr key={a.id} className="border-b border-white/5 hover:bg-white/[0.02] transition">
                  <td className="py-2.5 px-3 text-gray-500 font-mono text-xs">{a.id}</td>
                  <td className="py-2.5 px-3 max-w-xs">
                    <div className="text-white truncate font-medium" title={a.title}>{a.title}</div>
                    {a.source_url && (
                      <a href={a.source_url} target="_blank" rel="noreferrer" className="text-[10px] text-gray-600 hover:text-earl-400 truncate block">
                        {a.source_url.replace(/^https?:\/\//, "")}
                      </a>
                    )}
                  </td>
                  <td className="py-2.5 px-3 text-gray-400 text-xs">{a.source_name || "—"}</td>
                  <td className="py-2.5 px-3">
                    <span className="px-2 py-0.5 bg-white/5 rounded text-[10px] text-gray-400 uppercase">{a.category || "—"}</span>
                  </td>
                  <td className="py-2.5 px-3 text-gray-400 text-xs whitespace-nowrap">
                    {a.published_at ? new Date(a.published_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="py-2.5 px-3 text-gray-400 text-xs truncate max-w-[100px]">{a.author || "—"}</td>
                  <td className="py-2.5 px-3 text-center">
                    {a.embedded_at ? (
                      <span className="text-green-500 text-xs">✅</span>
                    ) : (
                      <span className="text-gray-600 text-xs">—</span>
                    )}
                  </td>
                  <td className="py-2.5 px-3 text-center">
                    <button
                      onClick={() => handleDelete(a.id, a.title)}
                      className="text-xs text-red-500 hover:text-red-400 transition px-2 py-1 rounded hover:bg-red-900/20"
                    >
                      Delete
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Year breakdown */}
      {stats && stats.by_year.length > 0 && (
        <div className="mt-8 bg-white/[0.02] border border-white/10 rounded-lg p-4">
          <h3 className="text-sm text-gray-400 font-semibold mb-3">Articles by Year</h3>
          <div className="flex flex-wrap gap-2">
            {stats.by_year.map((y) => (
              <div key={y.year} className="px-3 py-2 bg-white/5 rounded-lg text-center min-w-[70px]">
                <div className="text-white font-bold text-sm">{y.count.toLocaleString()}</div>
                <div className="text-gray-500 text-[10px]">{y.year}</div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
