"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useSeo } from "@/components/Seo";
import { getTeamLogoUrl } from "@/lib/team_logos";

interface PublicArticle {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  published_at: string | null;
  author?: string;
  slug?: string | null;
  teams?: string[] | null;
}

interface GamePreview {
  writeup_id: number;
  slug: string | null;
  game_id: number;
  title: string;
  published_at: string | null;
  game_date: string | null;
  game_day: string; // YYYY-MM-DD key
  matchup: string;
  away_abbr: string | null;
  home_abbr: string | null;
  summary: string | null;
}

const SPORT_NAME: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

type Tab = "articles" | "previews";

function toDayKey(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  // Convert to US Eastern (America/New_York) calendar date, so games are grouped
  // by the day they're actually played (EDT/EST handled automatically).
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(d);
  const y = parts.find((p) => p.type === "year")?.value || "";
  const m = parts.find((p) => p.type === "month")?.value || "";
  const day = parts.find((p) => p.type === "day")?.value || "";
  return `${y}-${m}-${day}`;
}

function todayEastern(): string {
  // America/New_York date — matches the grouping key for game days.
  const now = new Date();
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(now);
  const y = parts.find((p) => p.type === "year")?.value || "";
  const m = parts.find((p) => p.type === "month")?.value || "";
  const d = parts.find((p) => p.type === "day")?.value || "";
  return `${y}-${m}-${d}`;
}

function parseDayKey(key: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
  if (!match) return null;
  return new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

function formatDayKey(key: string): string {
  const d = parseDayKey(key);
  if (!d) return key;
  // The key is an Eastern-sourced YYYY-MM-DD string. Format in UTC so the
  // label shows the same calendar day (avoids the browser's local-tz shifting
  // the header back a day, e.g. a date that's really Aug 6 showing as Aug 5).
  return d.toLocaleDateString("en-US", {
    timeZone: "UTC",
    weekday: "short",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function parseAbbrs(w: Record<string, unknown>): { home: string | null; away: string | null } {
  // NBA provides home_abbr/away_abbr (and full names).
  const awayAbbr = typeof w.away_abbr === "string" ? w.away_abbr.trim() : "";
  const homeAbbr = typeof w.home_abbr === "string" ? w.home_abbr.trim() : "";
  if (awayAbbr && homeAbbr) return { home: homeAbbr, away: awayAbbr };

  // MLB/NFL: matchup is "AWAY @ HOME" using team abbreviations.
  const matchup = typeof w.matchup === "string" ? w.matchup.trim() : "";
  const m = /^([A-Za-z0-9]+)\s*@\s*([A-Za-z0-9]+)$/.exec(matchup);
  if (m) return { home: m[2].toUpperCase(), away: m[1].toUpperCase() };

  return { home: null, away: null };
}

function normalizeGamePreviews(data: unknown): GamePreview[] {
  // The three writeup list endpoints use slightly different shapes.
  // MLB (/mlb/list) & NBA (/nba/writeups) return a flat array.
  // NFL (/nfl/writeups) returns { items: [...] }.
  const raw = Array.isArray(data) ? data : (data as { items?: unknown[] })?.items ?? [];
  const items = raw.flatMap((r) => (Array.isArray(r) ? r : [r])) as Record<string, unknown>[];

  return items
    .map((w): GamePreview | null => {
      const id = Number(w.writeup_id ?? w.id ?? w.writeupId);
      const game_id = Number(w.game_id ?? w.gameId);
      if (!id) return null;

      const rawDate = w.game_date ?? w.date;
      const published = w.published_at ?? w.published;
      const gameDateStr = rawDate ? String(rawDate) : "";

      const matchup =
        (typeof w.matchup === "string" && w.matchup.trim()) ||
        (w.away && w.home ? `${w.away} @ ${w.home}` : "") ||
        (w.away_abbr && w.home_abbr ? `${w.away_abbr} @ ${w.home_abbr}` : "");
      const abbrs = parseAbbrs(w);

      return {
        writeup_id: id,
        slug: typeof w.slug === "string" && w.slug ? w.slug : null,
        game_id,
        title: typeof w.title === "string" ? w.title : `Game Preview`,
        published_at: published ? String(published) : null,
        game_date: gameDateStr,
        game_day: gameDateStr ? toDayKey(gameDateStr) : "",
        matchup,
        away_abbr: abbrs.away,
        home_abbr: abbrs.home,
        summary: (typeof w.summary === "string" && w.summary.trim()) ? w.summary.trim() : null,
      };
    })
    .filter((x): x is GamePreview => x !== null && Boolean(x.matchup));
}

function formatDate(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function formatGameTs(value: string | null): string {
  // Show game day/time in US Eastern so it matches the grouping.
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export default function SportArticlesPage({ params }: { params: Promise<{ sport: string }> }) {
  const [sport, setSport] = useState<string>("");
  const [articles, setArticles] = useState<PublicArticle[] | null>(null);
  const [previews, setPreviews] = useState<GamePreview[] | null>(null);

  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useRouter();

  const activeTab: Tab = searchParams.get("tab") === "previews" ? "previews" : "articles";
  const requestedDate = searchParams.get("date"); // YYYY-MM-DD or null

  useEffect(() => {
    let active = true;
    params.then(({ sport }) => {
      setSport(sport);

      fetch(`/api/original-articles/${sport}`)
        .then((r) => (r.ok ? r.json() : { articles: [] }))
        .then((d) => active && setArticles(d.articles ?? []))
        .catch(() => active && setArticles([]));

      // Game Previews are the public writeups for each sport. Fetch a large
      // window so the archive can be browsed by day.
      const listUrl =
        sport === "nfl"
          ? `/api/writeups/nfl/writeups?status=published&per_page=100`
          : sport === "nba"
          ? `/api/writeups/nba/writeups?status=published&limit=200`
          : `/api/writeups/mlb/list?status=published&limit=200`;

      fetch(listUrl)
        .then((r) => (r.ok ? r.json() : []))
        .then((d) => active && setPreviews(normalizeGamePreviews(d)))
        .catch(() => active && setPreviews([]));
    });
    return () => {
      active = false;
    };
  }, [params]);

  const name = SPORT_NAME[sport] || sport.toUpperCase();

  useSeo({
    title: `Articles & Game Previews — ${name} | Earl Knows Ball`,
    description: `Read ${name} original articles, game previews, analysis, and AI handicapping content from Earl Knows Ball.`,
    keywords: `${sport}, ${name}, sports betting, AI handicapping, picks, analysis, game previews, Earl Knows Ball`,
  });

  // Derive the sorted list of distinct days that have previews (archive cursor).
  const days = useMemo<string[]>(() => {
    const set = new Set<string>();
    (previews ?? []).forEach((p) => p.game_day && set.add(p.game_day));
    return Array.from(set).sort(); // ascending YYYY-MM-DD (string sort == chronological)
  }, [previews]);

  // Resolve the selected day: from URL, else today, else nearest available.
  const selectedDay = useMemo(() => {
    const todayKey = todayEastern();
    if (requestedDate && days.includes(requestedDate)) return requestedDate;
    if (days.includes(todayKey)) return todayKey;
    // Fall back to the closest day to today (prefer most recent on or before, else first).
    const nearest = days.reduce<string | null>((best, d) => {
      if (best === null) return d;
      return Math.abs(Date.parse(d) - Date.parse(todayKey)) <
        Math.abs(Date.parse(best) - Date.parse(todayKey))
        ? d
        : best;
    }, null);
    return nearest ?? todayKey;
  }, [requestedDate, days]);

  const dayPreviews = useMemo(() => {
    const list = (previews ?? []).filter((p) => p.game_day === selectedDay);
    // Sort within the day by game time.
    return list.sort((a, b) => Date.parse(a.game_date || "") - Date.parse(b.game_date || ""));
  }, [previews, selectedDay]);

  const prevDay = days[days.indexOf(selectedDay) - 1] || null;
  const nextDay = days[days.indexOf(selectedDay) + 1] || null;

  const setTab = (tab: Tab) => {
    const next = new URLSearchParams(searchParams.toString());
    if (tab === "previews") {
      next.set("tab", "previews");
      if (selectedDay) next.set("date", selectedDay);
    } else {
      next.delete("tab");
      next.delete("date");
    }
    const qs = next.toString();
    router.replace(`${pathname}${qs ? `?${qs}` : ""}`, { scroll: false });
  };

  const jumpToDay = (day: string | null) => {
    if (!day) return;
    const next = new URLSearchParams(searchParams.toString());
    next.set("tab", "previews");
    next.set("date", day);
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  };

  const tabClass = (active: boolean) =>
    `px-5 py-2.5 text-sm font-semibold rounded-lg transition ${
      active
        ? "bg-earl-500 text-white shadow"
        : "text-gray-400 hover:text-white hover:bg-white/[0.05]"
    }`;

  const navBtnClass =
    "px-3 py-2 text-sm font-medium rounded-lg border border-white/10 text-gray-300 hover:bg-white/[0.05] hover:text-white transition disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-gray-300";

  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <div className="mb-8">
        <h1 className="font-display text-4xl font-bold tracking-tight mb-2">
          {name} Content
        </h1>
        <p className="text-gray-400">
          Original articles and AI handicapping game previews from Earl Knows Ball.
        </p>

        <div className="mt-6 inline-flex rounded-lg border border-white/10 bg-white/[0.03] p-1">
          <button type="button" onClick={() => setTab("articles")} className={tabClass(activeTab === "articles")}>
            Articles
          </button>
          <button type="button" onClick={() => setTab("previews")} className={tabClass(activeTab === "previews")}>
            Game Previews
          </button>
        </div>
      </div>

      {activeTab === "articles" ? (
        articles === null ? (
          <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
            Loading…
          </div>
        ) : articles.length === 0 ? (
          <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
            No original articles published yet. Check back soon.
          </div>
        ) : (
          <ul className="divide-y divide-white/10 border border-white/10 rounded-lg bg-white/[0.02]">
            {articles.map((a) => (
              <li key={a.id}>
                <Link
                  href={`/${sport}/articles/${a.slug || a.id}`}
                  className="block px-5 py-4 hover:bg-white/[0.04] transition group"
                >
                  {/* Team logos — horizontal row above the article, left to right */}
                  {Array.isArray(a.teams) && a.teams.length > 0 && (
                    <div className="flex items-center gap-1.5 mb-2">
                      {a.teams.slice(0, 4).map((abbr) => (
                        <img
                          key={abbr}
                          src={getTeamLogoUrl(abbr, sport) || ""}
                          alt={abbr}
                          loading="lazy"
                          className="h-7 w-7 object-contain"
                        />
                      ))}
                    </div>
                  )}
                  <div className="text-sm text-gray-500 mb-1">
                    {formatDate(a.published_at)}
                    {a.author ? <span> · by {a.author}</span> : null}
                  </div>
                  <div className="text-lg font-semibold group-hover:text-earl-400 transition">
                    {a.title}
                  </div>
                  {a.summary && (
                    <p className="text-sm text-gray-400 mt-1 line-clamp-2">{a.summary}</p>
                  )}
                </Link>
              </li>
            ))}
          </ul>
        )
      ) : previews === null ? (
        <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
          Loading…
        </div>
      ) : (
        <div>
          {/* Day navigation (archive browsing) */}
          <div className="mb-6 flex items-center justify-between border border-white/10 rounded-lg bg-white/[0.02] px-4 py-3">
            <button
              type="button"
              className={navBtnClass}
              disabled={!prevDay}
              onClick={() => jumpToDay(prevDay)}
            >
              ← Previous
            </button>
            <div className="text-center">
              <div className="text-sm font-semibold text-white">{formatDayKey(selectedDay)}</div>
              <div className="text-xs text-gray-500">
                {dayPreviews.length} game preview{dayPreviews.length === 1 ? "" : "s"}
                {selectedDay === todayEastern() ? " · Today" : ""}
              </div>
            </div>
            <button
              type="button"
              className={navBtnClass}
              disabled={!nextDay}
              onClick={() => jumpToDay(nextDay)}
            >
              Next →
            </button>
          </div>

          {dayPreviews.length === 0 ? (
            <div className="text-center py-16 text-gray-500 border border-white/10 rounded-lg">
              No game previews for this date.
            </div>
          ) : (
            <ul className="divide-y divide-white/10 border border-white/10 rounded-lg bg-white/[0.02]">
              {dayPreviews.map((p) => (
                <li key={p.writeup_id}>
                  <Link
                    href={`/${sport}/articles/previews/${p.slug || p.writeup_id}`}
                    className="block px-5 py-4 hover:bg-white/[0.04] transition group"
                  >
                    <div className="flex flex-col sm:flex-row sm:items-center gap-3">
                      {/* Team logos — left on desktop, top on mobile */}
                      <div className="flex items-center gap-2 sm:flex-col sm:gap-1 sm:shrink-0">
                        {(p.away_abbr || p.home_abbr) && (
                          <>
                            {p.away_abbr && (
                              <img
                                src={getTeamLogoUrl(p.away_abbr, sport) || ""}
                                alt={p.away_abbr}
                                loading="lazy"
                                className="h-8 w-8 sm:h-9 sm:w-9 object-contain"
                              />
                            )}
                            <span className="text-sm font-bold text-gray-400">vs</span>
                            {p.home_abbr && (
                              <img
                                src={getTeamLogoUrl(p.home_abbr, sport) || ""}
                                alt={p.home_abbr}
                                loading="lazy"
                                className="h-8 w-8 sm:h-9 sm:w-9 object-contain"
                              />
                            )}
                          </>
                        )}
                      </div>

                      {/* Preview info */}
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold text-earl-400">{p.matchup}</div>
                        <div className="text-xs text-gray-500 mt-0.5">
                          {formatGameTs(p.game_date) || formatDate(p.published_at) || "Recently"}
                          <span className="text-gray-600"> · by Earl</span>
                        </div>
                        <div className="text-lg font-semibold mt-1.5 group-hover:text-earl-400 transition">
                          {p.title}
                        </div>
                        {p.summary && (
                          <p className="text-sm text-gray-400 mt-1.5 line-clamp-2 leading-relaxed">
                            {p.summary}
                          </p>
                        )}
                      </div>
                    </div>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
