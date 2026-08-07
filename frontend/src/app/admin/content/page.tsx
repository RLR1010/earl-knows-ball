"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter, useSearchParams, usePathname } from "next/navigation";
import { useSeo } from "@/components/Seo";

const SPORTS = ["mlb", "nfl", "nba"] as const;
type Sport = (typeof SPORTS)[number];

/* ─────────────────────────────────────────────
   Types
   ───────────────────────────────────────────── */

interface Game {
  id: number;
  date: string;
  home_team: string;
  away_team: string;
  venue: string;
  week?: number | null;
  season_type?: string | null;
  writeup_id: number | null;
  writeup_title: string | null;
  writeup_status: string | null;
  writeup_version: number | null;
}

function weekLabel(game: Pick<Game, "season_type" | "week">): string | null {
  const w = game.week;
  if (!w) return null;
  if (game.season_type === "PRE") return w >= 30 ? `PS Week ${w - 29}` : `PS Week ${w}`;
  if (w >= 19) {
    const labels: Record<number, string> = {
      19: "Wild Card",
      20: "Divisional",
      21: "Conf Champ",
      22: "Super Bowl",
    };
    return labels[w] || `Week ${w}`;
  }
  return `Week ${w}`;
}

interface WriteupSummary {
  id: number;
  game_id: number;
  title: string;
  status: string;
  version: number;
  is_historical: boolean;
  game_date: string;
  matchup: string;
}

/* ─────────────────────────────────────────────
   Helpers
   ───────────────────────────────────────────── */

const token = () => localStorage.getItem("earl_token");

const STATUS_COLORS: Record<string, string> = {
  published: "bg-green-500/20 text-green-400 border border-green-500/30",
};

const STATUS_LABELS: Record<string, string> = {
  published: "Published",
};

// All write-ups are live immediately — normalize every status to published.
const NORMALIZE_STATUS = (status: string | null | undefined): string | null =>
  status ? "published" : null;

function localDateStr(isoStr: string): string {
  return new Date(isoStr).toLocaleDateString("en-CA", {
    timeZone: "America/New_York",
  });
}

function formatDate(dateStr: string): string {
  // Date-only strings like "2026-07-11" are parsed as midnight UTC by
  // the spec, which shifts them to the previous day in America/New_York.
  // Add T12 to land in local noon — avoids the UTC-midnight rollover.
  const d = dateStr.includes("T")
    ? new Date(dateStr)
    : new Date(dateStr + "T12:00:00");
  return d.toLocaleDateString("en-US", {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "America/New_York",
  });
}

function formatTime(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    timeZone: "America/New_York",
  }) + " ET";
}

/* ─────────────────────────────────────────────
   Components
   ───────────────────────────────────────────── */

function StatBox({
  label,
  value,
  color,
}: {
  label: string;
  value: string | number;
  color?: string;
}) {
  return (
    <div className="bg-white/[0.03] border border-white/10 rounded-lg p-4">
      <div className="text-xs text-gray-500 uppercase tracking-wider mb-0.5">
        {label}
      </div>
      <div className={`text-xl font-bold ${color || "text-white"}`}>
        {value}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string | null }) {
  const norm = NORMALIZE_STATUS(status);
  if (!norm) return null;
  const color = STATUS_COLORS[norm] || STATUS_COLORS.published;
  return (
    <span className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${color}`}>
      {STATUS_LABELS[norm] || norm}
    </span>
  );
}

function GameCard({
  game,
  onGenerate,
  onPreview,
  onEdit,
  generating,
}: {
  game: Game;
  onGenerate: (id: number) => void;
  onPreview: (id: number) => void;
  onEdit: (id: number) => void;
  generating: boolean;
}) {
  const today = new Date();
  // Compare local date strings so past/future respects the user's timezone
  const todayLocal = today.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const gameLocal = new Date(game.date).toLocaleDateString("en-CA", { timeZone: "America/New_York" });
  const isPast = gameLocal < todayLocal;

  return (
    <div
      className={`bg-white/[0.03] border border-white/10 rounded-xl p-4 transition hover:bg-white/[0.05] ${
        isPast ? "opacity-60" : ""
      }`}
    >
      <div className="flex items-start justify-between mb-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 mb-1">
            {(weekLabel(game) && game.season_type === "PRE") && (
              <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide bg-purple-500/20 text-purple-300 border border-purple-500/30">
                {weekLabel(game)}
              </span>
            )}
            <span className="font-semibold text-white text-sm">
              {game.away_team}
            </span>
            <span className="text-gray-500 text-xs">@</span>
            <span className="font-semibold text-white text-sm">
              {game.home_team}
            </span>
          </div>
          <div className="text-xs text-gray-500">
            {formatDate(game.date)} &middot; {formatTime(game.date)}
          </div>
          <div className="text-xs text-gray-600 mt-0.5">{game.venue}</div>
        </div>
        <div className="ml-3 flex-shrink-0">
          {game.writeup_status ? (
            <StatusBadge status={game.writeup_status} />
          ) : (
            <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-gray-500/20 text-gray-400 border border-gray-500/30">
              Missing
            </span>
          )}
        </div>
      </div>

      {game.writeup_title && (
        <div className="text-xs text-gray-400 mb-3 truncate">
          {game.writeup_title}
        </div>
      )}

      <div className="flex gap-2 mt-2">
        {game.writeup_status ? (
          <>
            <button
              onClick={() => onPreview(game.writeup_id!)}
              className="flex-1 px-3 py-1.5 text-xs font-medium rounded-lg border border-white/10 text-gray-300 hover:text-white hover:bg-white/5 transition"
            >
              Preview
            </button>
            <button
              onClick={() => onEdit(game.writeup_id!)}
              className="flex-1 px-3 py-1.5 text-xs font-medium rounded-lg bg-earl-600/20 text-earl-400 border border-earl-600/30 hover:bg-earl-600/30 transition"
            >
              Edit
            </button>
          </>
        ) : (
          <button
            onClick={() => onGenerate(game.id)}
            disabled={generating}
            className="flex-1 px-3 py-1.5 text-xs font-medium rounded-lg bg-blue-600/20 text-blue-400 border border-blue-600/30 hover:bg-blue-600/30 transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {generating ? "Generating..." : "Generate"}
          </button>
        )}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────
   Main Page
   ───────────────────────────────────────────── */

export default function AdminContent() {
  useSeo({ title: "Content — Admin — Earl Knows Ball" });
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  // URL is the source of truth: sport + day offset are encoded as query params.
  // This lets us leave the listing (e.g. open a writeup) and return to the exact
  // same sport tab + day position.
  const urlSport = searchParams.get("sport");
  const urlDays = searchParams.get("days");
  const [sport, setSport] = useState<Sport>(
    (SPORTS as readonly string[]).includes(urlSport ?? "") ? (urlSport as Sport) : "mlb"
  );
  const [daysOffset, setDaysOffset] = useState(() => {
    const d = Number(urlDays);
    return Number.isFinite(d) ? d : 0;
  });
  const [games, setGames] = useState<Game[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [generating, setGenerating] = useState<number | null>(null);
  const [generatingDay, setGeneratingDay] = useState<string | null>(null);
  const [stats, setStats] = useState({
    total: 0,
    with_writeup: 0,
    published: 0,
  });
  const [showHistorical, setShowHistorical] = useState(false);

  // ── Find nearest game date for smart navigation ─────────────────

  const fetchNearestGame = useCallback(async (targetDate: string, direction: 'next' | 'prev'): Promise<string | null> => {
    try {
      const res = await fetch(
        `/api/writeups/${sport}/nearest-game?date=${targetDate}`,
        { headers: { Authorization: `Bearer ${token()}` } }
      );
      if (!res.ok) return null;
      const data = await res.json();
      // prev_date is the most recent day before the target that has games
      // next_date is the next day after the target that has games
      if (direction === 'next') return data.next_date || null;
      return data.prev_date || null;
    } catch {
      return null;
    }
  }, [sport]);

  // ── Fetch games for the selected date range ─────────────────

  const fetchGames = useCallback(async () => {
    setLoading(true);
    setError(null);

    const start = new Date();
    start.setDate(start.getDate() + daysOffset);
    // API window: +2 days to catch evening games that start after midnight UTC
    const apiEnd = new Date(start);
    apiEnd.setDate(apiEnd.getDate() + 2);

    // Use local-date strings for the backend filter so the API
    // queries the correct UTC window around the user's local dates.
    const from = start.toLocaleDateString("en-CA", { timeZone: "America/New_York" });
    const to = apiEnd.toLocaleDateString("en-CA", { timeZone: "America/New_York" });

    try {
      const res = await fetch(
        `/api/writeups/${sport}/games?from=${from}&to=${to}`,
        {
          headers: { Authorization: `Bearer ${token()}` },
        }
      );
      if (!res.ok) throw new Error(`Failed to fetch: ${res.status}`);
      const data = await res.json();
      console.log("Games data:", data);

      // The API returns an array of game objects
      // We need to merge writeup status from the writeups endpoint
      const gameList: Game[] = (Array.isArray(data) ? data : data.games || []).map((g: any) => ({
        id: g.id,
        date: g.date,
        home_team: g.home_team || g.home_team_abbreviation || g.home_team_abbr,
        away_team: g.away_team || g.away_team_abbreviation || g.away_team_abbr,
        venue: g.venue || "",
        week: g.week ?? null,
        season_type: g.season_type ?? null,
        writeup_id: g.writeup_id || null,
        writeup_title: g.writeup_title || null,
        writeup_status: g.writeup_status || null,
        writeup_version: g.writeup_version || null,
      }));

      setGames(gameList);
      setStats({
        total: gameList.length,
        with_writeup: gameList.filter((g) => g.writeup_status).length,
        published: gameList.filter((g) => g.writeup_status).length,
      });
    } catch (e: any) {
      console.error("fetchGames error:", e);
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [daysOffset, sport]);

  // ── Keep URL in sync with sport + day offset (replace, not push) ──

  useEffect(() => {
    const params = new URLSearchParams();
    params.set("sport", sport);
    if (daysOffset !== 0) {
      params.set("days", String(daysOffset));
    }
    const next = `${pathname}?${params.toString()}`;
    if (next !== window.location.pathname + window.location.search) {
      router.replace(next);
    }
  }, [sport, daysOffset, pathname, router]);

  // ── On mount (fresh visit, no sport/days param): snap to nearest game ──

  useEffect(() => {
    // If a sport/day was provided in the URL (returning to a saved position),
    // honor it exactly instead of snapping to the nearest game.
    if (searchParams.has("days") || searchParams.has("sport")) return;
    if (sport === "nfl" || sport === "nba") {
      // Find the next upcoming game and snap to it
      const today = new Date().toLocaleDateString("en-CA");
      fetchNearestGame(today, "next").then((date) => {
        if (date) {
          const diff = Math.round(
            (new Date(date).getTime() - new Date(new Date().toDateString()).getTime()) /
              (1000 * 60 * 60 * 24)
          );
          setDaysOffset(diff);
        } else {
          // No next game — try previous
          fetchNearestGame(today, "prev").then((prevDate) => {
            if (prevDate) {
              const diff = Math.round(
                (new Date(prevDate).getTime() - new Date(new Date().toDateString()).getTime()) /
                  (1000 * 60 * 60 * 24)
              );
              setDaysOffset(diff);
            } else {
              setDaysOffset(0);
            }
          });
        }
      });
    } else {
      setDaysOffset(0);
    }
  }, [sport, fetchNearestGame]);

  // ── Refetch when daysOffset changes ──

  useEffect(() => {
    fetchGames();
  }, [fetchGames]);

  // ── Actions ────────────────────────────────────────────

  const handleGenerate = async (gameId: number) => {
    setGenerating(gameId);
    try {
      // Use the /api/writeups path so Caddy proxies DIRECTLY to the backend
      // (port 8001), bypassing the Next.js rewrite proxy (which has a ~30-60s
      // timeout and can abort long generations ~3-5min, killing the fetch while
      // the backend keeps running and still saves the write-up -> false
      // "Generation failed: Failed to fetch" alert).
      const res = await fetch(`/api/writeups/${sport}/generate/${gameId}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
      });

      if (!res.ok) {
        const errText = await res.text();
        throw new Error(errText);
      }
      // Refresh the list AFTER generation succeeds. Keep this outside the
      // generation try/catch so a refresh error is NOT mislabeled as a
      // generation failure (writeup is already saved by this point).
      try {
        await fetchGames();
      } catch (refreshErr: any) {
        console.error("Writeup generated but list refresh failed:", refreshErr);
      }
    } catch (e: any) {
      if (e.name === "AbortError") {
        console.error("Generate timeout:", e);
        alert("Generation timed out after 10 minutes.");
      } else {
        console.error("Generate error:", e);
        alert(`Generation failed: ${e.message}`);
      }
    } finally {
      setGenerating(null);
    }
  };

  const handlePreview = (writeupId: number) => {
    window.open(`/api/writeups/${sport}/${writeupId}?tier=premium`, "_blank");
  };

  const handleEdit = (writeupId: number) => {
    router.push(`/admin/content/${writeupId}?sport=${sport}&days=${daysOffset}`);
  };

  const handleGenerateAll = async () => {
    const missing = games.filter((g) => !g.writeup_status);
    if (missing.length === 0) {
      alert("All games already have write-ups!");
      return;
    }
    if (
      !confirm(
        `Generate write-ups for ${missing.length} games? This will take a while.`
      )
    )
      return;

    const failed: number[] = [];
    for (const game of missing) {
      setGenerating(game.id);
      try {
        const res = await fetch(`/api/writeups/${sport}/generate/${game.id}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token()}` },
        });
        if (!res.ok) {
          const errText = await res.text();
          throw new Error(errText || `HTTP ${res.status}`);
        }
      } catch (e: any) {
        console.error(`Failed for game ${game.id}:`, e);
        failed.push(game.id);
      }
    }
    setGenerating(null);
    await fetchGames();

    if (failed.length > 0) {
      alert(
        `⚠️ ${failed.length} of ${missing.length} write-ups failed: ${failed.join(", ")}\n\nClick Generate All again to retry the failed games.`
      );
    } else {
      alert(`✅ All ${missing.length} write-ups generated successfully.`);
    }
  };

  const handleGenerateDay = async (daysGames: Game[]) => {
    const missing = daysGames.filter((g) => !g.writeup_status);
    if (missing.length === 0) {
      alert("All games on this day already have write-ups!");
      return;
    }
    if (
      !confirm(
        `Generate write-ups for ${missing.length} games on this day?`
      )
    )
      return;

    const failed: number[] = [];
    for (const game of missing) {
      setGenerating(game.id);
      try {
        const res = await fetch(`/api/writeups/${sport}/generate/${game.id}`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token()}` },
        });
        if (!res.ok) {
          const errText = await res.text();
          throw new Error(errText || `HTTP ${res.status}`);
        }
      } catch (e: any) {
        console.error(`Failed for game ${game.id}:`, e);
        failed.push(game.id);
      }
    }
    setGenerating(null);
    await fetchGames();

    if (failed.length > 0) {
      alert(
        `⚠️ ${failed.length} of ${missing.length} write-ups failed: ${failed.join(", ")}\n\nClick Generate Day again to retry the failed games.`
      );
    } else {
      alert(`✅ All ${missing.length} write-ups generated successfully.`);
    }
  };

  // ── Render ─────────────────────────────────────────────

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-white">Content</h1>
          <p className="text-sm text-gray-500 mt-1">
            Manage game write-ups across all sports
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={handleGenerateAll}
            className="px-4 py-2 text-sm font-medium rounded-lg bg-blue-600/20 text-blue-400 border border-blue-600/30 hover:bg-blue-600/30 transition"
          >
            Generate All
          </button>
        </div>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-4 gap-4 mb-6">
        <StatBox label="Games" value={stats.total} color="text-white" />
        <StatBox
          label="Published"
          value={stats.with_writeup}
          color="text-green-400"
        />
      </div>

      {/* Controls */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-2">
          {SPORTS.map((s) => (
            <button
              key={s}
              onClick={() => setSport(s)}
              className={`px-4 py-2 text-sm font-medium rounded-lg border transition ${
                sport === s
                  ? "bg-earl-600/20 text-earl-400 border-earl-600/30"
                  : "bg-white/[0.03] text-gray-400 border-white/10 hover:text-white"
              }`}
            >
              {s.toUpperCase()}
            </button>
          ))}
          <label className="flex items-center gap-2 ml-4 text-sm text-gray-400 cursor-pointer">
            <input
              type="checkbox"
              checked={showHistorical}
              onChange={(e) => setShowHistorical(e.target.checked)}
              className="rounded border-gray-600 text-earl-500 bg-gray-800 focus:ring-earl-500"
            />
            Historical
          </label>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={async () => {
              if (sport === "nfl") {
                const today = new Date();
                const cur = new Date(today);
                cur.setDate(cur.getDate() + daysOffset);
                const ds = cur.toLocaleDateString("en-CA");
                const prev = await fetchNearestGame(ds, "prev");
                if (prev) {
                  const diff = Math.round(
                    (new Date(prev).getTime() - new Date(new Date().toDateString()).getTime()) / (1000 * 60 * 60 * 24)
                  );
                  setDaysOffset(diff);
                }
              } else {
                setDaysOffset((d) => d - 2);
              }
            }}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-white/[0.03] border border-white/10 text-gray-400 hover:text-white transition"
          >
            ← Earlier
          </button>
          <button
            onClick={() => setDaysOffset(0)}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-white/[0.03] border border-white/10 text-gray-400 hover:text-white transition"
          >
            Today
          </button>
          <button
            onClick={async () => {
              if (sport === "nfl") {
                const today = new Date();
                const cur = new Date(today);
                cur.setDate(cur.getDate() + daysOffset);
                const ds = cur.toLocaleDateString("en-CA");
                const nxt = await fetchNearestGame(ds, "next");
                if (nxt) {
                  const diff = Math.round(
                    (new Date(nxt).getTime() - new Date(new Date().toDateString()).getTime()) / (1000 * 60 * 60 * 24)
                  );
                  setDaysOffset(diff);
                }
              } else {
                setDaysOffset((d) => d + 2);
              }
            }}
            className="px-3 py-1.5 text-xs font-medium rounded-lg bg-white/[0.03] border border-white/10 text-gray-400 hover:text-white transition"
          >
            Later →
          </button>
        </div>
      </div>

      {/* Game grid */}
      {loading ? (
        <div className="text-center py-20">
          <div className="text-gray-400 text-lg animate-pulse">
            Loading games...
          </div>
        </div>
      ) : error ? (
        <div className="text-center py-20">
          <div className="text-red-400 text-lg mb-2">Failed to load</div>
          <div className="text-gray-500 text-sm">{error}</div>
          <button
            onClick={fetchGames}
            className="mt-4 px-4 py-2 text-sm rounded-lg bg-white/[0.03] border border-white/10 text-gray-400 hover:text-white transition"
          >
            Retry
          </button>
        </div>
      ) : games.length === 0 ? (
        <div className="text-center py-20">
          <div className="text-gray-500 text-lg">No games in this range</div>
        </div>
      ) : (
        <>
          {/* Day groups */}
          {groupByDay(games).map(({ date, games: dayGames }) => (
            <div key={date} className="mb-8">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-medium text-gray-400 uppercase tracking-wider">
                  {formatDate(date)}
                </h3>
                <button
                  onClick={() => handleGenerateDay(dayGames)}
                  className="px-3 py-1 text-xs font-medium rounded-lg bg-blue-600/20 text-blue-400 border border-blue-600/30 hover:bg-blue-600/30 transition"
                >
                  Generate Day
                </button>
              </div>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {dayGames.map((game) => (
                  <GameCard
                    key={game.id}
                    game={game}
                    onGenerate={handleGenerate}
                    onPreview={handlePreview}
                    onEdit={handleEdit}
                    generating={generating === game.id}
                  />
                ))}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

/* ─────────────────────────────────────────────
   Helpers
   ───────────────────────────────────────────── */

function groupByDay(games: Game[]): { date: string; games: Game[] }[] {
  const groups: Record<string, Game[]> = {};
  for (const g of games) {
    const day = localDateStr(g.date);
    if (!groups[day]) groups[day] = [];
    groups[day].push(g);
  }
  return Object.entries(groups)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, games]) => ({ date, games }));
}
