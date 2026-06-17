"use client";

import { useEffect, useState } from "react";
import { useSearchParams, useRouter } from "next/navigation";

const SPORTS = ["nfl", "nba", "mlb"] as const;
type Sport = (typeof SPORTS)[number];

interface RSSFeed {
  name: string;
  url: string;
  team: string | null;
}

interface RSSFeedsResponse {
  sport: string;
  total: number;
  feeds: RSSFeed[];
  teams?: Record<string, RSSFeed[]>;
  general?: RSSFeed[];
  team?: string;
}

const token = () => localStorage.getItem("earl_token");

const sportLabel: Record<Sport, string> = {
  nfl: "NFL",
  nba: "NBA",
  mlb: "MLB",
};

const sportColors: Record<Sport, string> = {
  nfl: "bg-green-900/30 text-green-400 border-green-700/40",
  nba: "bg-red-900/30 text-red-400 border-red-700/40",
  mlb: "bg-blue-900/30 text-blue-400 border-blue-700/40",
};

function websiteUrl(url: string) {
  try {
    const u = new URL(url);
    return `${u.protocol}//${u.hostname}`;
  } catch {
    return url;
  }
}

export default function AdminRSSFeeds() {
  const searchParams = useSearchParams();
  const router = useRouter();

  const [sport, setSport] = useState<Sport>("nfl");
  const [data, setData] = useState<RSSFeedsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [teamFilter, setTeamFilter] = useState("");

  useEffect(() => {
    const fetchFeeds = async () => {
      setLoading(true);
      try {
        const url = `/api/admin/articles/${sport}/rss-feeds`;
        const res = await fetch(url, {
          headers: { Authorization: `Bearer ${token()}` },
        });
        if (res.ok) setData(await res.json());
      } catch {} finally {
        setLoading(false);
      }
    };
    fetchFeeds();
  }, [sport]);

  // Derive team list from feeds data
  const allFeeds = data?.feeds || [];
  const teamAbbrs = [...new Set(allFeeds.filter((f) => f.team).map((f) => f.team!))].sort();

  // Filter feeds based on team selection + search
  const feeds = teamFilter === "__general__"
    ? allFeeds.filter((f) => !f.team)
    : teamFilter
    ? allFeeds.filter((f) => f.team === teamFilter)
    : allFeeds;

  const filtered = search
    ? feeds.filter(
        (f) =>
          f.name.toLowerCase().includes(search.toLowerCase()) ||
          f.url.toLowerCase().includes(search.toLowerCase())
      )
    : feeds;

  const totalDisplay = teamFilter ? feeds.length : data?.total || 0;

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">RSS Feeds</h1>
        <p className="text-gray-400 text-sm mt-1">
          All configured RSS news sources for each sport
        </p>
      </div>

      {/* Sport selector + Team dropdown */}
      <div className="flex flex-wrap gap-3 mb-6 items-end">
        <div className="flex gap-2">
          {SPORTS.map((s) => {
            return (
              <button
                key={s}
                onClick={() => {
                  setSport(s);
                  setTeamFilter("");
                  setSearch("");
                }}
                className={`px-5 py-2 rounded-lg text-sm font-semibold transition border ${
                  sport === s
                    ? `${sportColors[s]} border`
                    : "bg-white/5 text-gray-400 border-white/10 hover:text-white hover:bg-white/10"
                }`}
              >
                {sportLabel[s]}
              </button>
            );
          })}
        </div>

        <div className="flex-1 min-w-[200px] max-w-xs">
          <select
            value={teamFilter}
            onChange={(e) => setTeamFilter(e.target.value)}
            className="w-full px-4 py-[9px] bg-black/40 border border-white/10 rounded-lg text-sm text-gray-300 focus:outline-none focus:border-earl-600/50 appearance-none cursor-pointer"
          >
            <option value="">All teams & general sources</option>
            <option value="__general__">General / League-Wide Sources</option>
            <optgroup label="Team-Specific Sources">
              {teamAbbrs.map((abbr) => {
                const count = allFeeds.filter((f) => f.team === abbr).length;
                return (
                  <option key={abbr} value={abbr}>
                    {abbr} — {count} feed{count !== 1 ? "s" : ""}
                  </option>
                );
              })}
            </optgroup>
          </select>
        </div>
      </div>

      {/* Count summary */}
      {data && (
        <div className="flex items-center gap-4 mb-4 text-xs text-gray-500">
          <span className="text-gray-400 font-semibold">{totalDisplay}</span> feeds total
          {data.team && (
            <span className="bg-earl-600/10 border border-earl-600/30 text-earl-400 px-2 py-0.5 rounded">
              Team: {data.team} ({data.team})
            </span>
          )}
        </div>
      )}

      {/* Search */}
      <div className="mb-6">
        <input
          type="text"
          placeholder="Search feeds by name or URL..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full max-w-md px-4 py-2.5 bg-black/40 border border-white/10 rounded-lg text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-600/50"
        />
      </div>

      {/* Feed list */}
      {loading ? (
        <div className="text-gray-400 text-sm">Loading feeds...</div>
      ) : filtered.length === 0 ? (
        <div className="text-gray-500 text-sm py-8 text-center">
          {search
            ? "No feeds match your search"
            : "No feeds configured for this sport"}
        </div>
      ) : (
        <div className="grid gap-2">
          {filtered.map((feed, i) => (
            <div
              key={feed.url + feed.team}
              className="flex items-center justify-between px-4 py-3 bg-white/[0.02] border border-white/10 rounded-lg hover:bg-white/[0.05] transition group"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className="text-gray-600 text-xs font-mono w-6 text-right shrink-0">
                  {i + 1}
                </span>
                <div className="min-w-0">
                  <div className="text-sm text-white font-medium truncate">
                    {feed.name}
                  </div>
                  <div className="text-xs text-gray-500 truncate mt-0.5 flex items-center gap-2">
                    <a
                      href={feed.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-earl-400/70 hover:text-earl-400 transition font-mono truncate max-w-[400px]"
                    >
                      {feed.url}
                    </a>
                    {feed.team && (
                      <span className="shrink-0 px-1.5 py-0.5 bg-white/5 rounded text-[10px] text-gray-400 uppercase">
                        {feed.team}
                      </span>
                    )}
                    {!feed.team && (
                      <span className="shrink-0 px-1.5 py-0.5 bg-purple-900/20 border border-purple-700/30 rounded text-[10px] text-purple-400">
                        General
                      </span>
                    )}
                  </div>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0 ml-4">
                <a
                  href={feed.url}
                  target="_blank"
                  rel="noreferrer"
                  className="hidden sm:inline-flex items-center gap-1 px-3 py-1.5 text-xs bg-earl-600/10 border border-earl-600/20 text-earl-400 rounded-lg hover:bg-earl-600/20 transition"
                  title="Open RSS feed XML"
                >
                  RSS ↗
                </a>
                <a
                  href={websiteUrl(feed.url)}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 px-3 py-1.5 text-xs bg-white/5 border border-white/10 text-gray-400 rounded-lg hover:text-white hover:bg-white/10 transition"
                  title="Visit website"
                >
                  Site ↗
                </a>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Summary footer */}
      {data && !loading && (
        <div className="mt-8 pt-6 border-t border-white/10 text-xs text-gray-500">
          <span className="font-semibold text-gray-400">{totalDisplay}</span>{" "}
          RSS feeds configured for {sportLabel[sport].toUpperCase()}.
          {teamAbbrs.length > 0 && (
            <span className="ml-2">
              {teamAbbrs.length} teams with dedicated team blogs,{" "}
              {allFeeds.filter((f) => !f.team).length} league-wide sources.
            </span>
          )}
          {search && filtered.length !== data.total && (
            <span className="ml-2">
              Showing {filtered.length} of {data.total} total.
            </span>
          )}
        </div>
      )}
    </div>
  );
}
