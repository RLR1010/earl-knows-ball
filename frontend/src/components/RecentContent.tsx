"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import TeamLogo from "@/components/TeamLogo";

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

function parseAbbrs(w: Record<string, unknown>): { home: string | null; away: string | null } {
  const awayAbbr = typeof w.away_abbr === "string" ? w.away_abbr.trim() : "";
  const homeAbbr = typeof w.home_abbr === "string" ? w.home_abbr.trim() : "";
  if (awayAbbr && homeAbbr) return { home: homeAbbr, away: awayAbbr };
  const matchup = typeof w.matchup === "string" ? w.matchup.trim() : "";
  const m = /^([A-Za-z0-9]+)\s*@\s*([A-Za-z0-9]+)$/.exec(matchup);
  if (m) return { home: m[2].toUpperCase(), away: m[1].toUpperCase() };
  return { home: null, away: null };
}

function normalizeGamePreviews(data: unknown): GamePreview[] {
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
      const isoTs = /^\d{4}-\d{2}-\d{2}T/.test(gameDateStr); // full timestamp
      const matchup =
        (typeof w.matchup === "string" && w.matchup.trim()) ||
        (w.away && w.home ? `${w.away} @ ${w.home}` : "") ||
        (w.away_abbr && w.home_abbr ? `${w.away_abbr} @ ${w.home_abbr}` : "");
      const abbrs = parseAbbrs(w);
      return {
        writeup_id: id,
        slug: typeof w.slug === "string" && w.slug ? w.slug : null,
        game_id,
        title: typeof w.title === "string" ? w.title : "Game Preview",
        published_at: published ? String(published) : null,
        game_date: gameDateStr,
        // Bucket games by their Eastern-time calendar day (matches the rest of
        // the app, e.g. the admin content page). Full timestamps are converted;
        // plain date-only strings are taken as-is.
        game_day: gameDateStr
          ? isoTs
            ? new Date(gameDateStr).toLocaleDateString("en-CA", {
                timeZone: "America/New_York",
              })
            : gameDateStr.slice(0, 10)
          : "",
        matchup,
        away_abbr: abbrs.away,
        home_abbr: abbrs.home,
        summary: typeof w.summary === "string" ? w.summary : null,
      };
    })
    .filter((p): p is GamePreview => p !== null);
}

function formatDate(value: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
}

function formatGameTs(value: string | null): string {
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

// Today's date as a YYYY-MM-DD key in Eastern time (matches game_day keys).
function toEasternDayKey(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

const SPORT_NAME: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

export default function RecentContent({ sport }: { sport: string }) {
  const [articles, setArticles] = useState<PublicArticle[] | null>(null);
  const [previews, setPreviews] = useState<GamePreview[] | null>(null);

  useEffect(() => {
    let active = true;

    // Original articles (newest first).
    fetch(`/api/original-articles/${sport}?limit=4`)
      .then((r) => (r.ok ? r.json() : { articles: [] }))
      .then((d) => {
        const list = Array.isArray(d.articles) ? d.articles : Array.isArray(d) ? d : [];
        const sorted = [...list].sort((a, b) =>
          String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")),
        );
        if (active) setArticles(sorted.slice(0, 4));
      })
      .catch(() => active && setArticles([]));

    // Game previews (public writeups), newest first.
    const listUrl =
      sport === "nfl"
        ? `/api/writeups/nfl/writeups?status=published&per_page=100`
        : sport === "nba"
        ? `/api/writeups/nba/writeups?status=published&limit=200`
        : `/api/writeups/mlb/list?status=published&limit=200`;

    fetch(listUrl)
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => {
        // Keep only previews whose game is on the previous day, today, or in the future.
        const cutoffMs = Date.parse(toEasternDayKey()) - 24 * 60 * 60 * 1000; // yesterday 00:00 ET
        const cutoff = new Date(cutoffMs).toISOString().slice(0, 10); // YYYY-MM-DD
        const list = normalizeGamePreviews(d)
          .filter((p) => !!p.game_day && p.game_day >= cutoff)
          .sort((a, b) =>
            String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")),
          );
        if (active) setPreviews(list.slice(0, 4));
      })
      .catch(() => active && setPreviews([]));

    return () => {
      active = false;
    };
  }, [sport]);

  const name = SPORT_NAME[sport] || sport.toUpperCase();

  return (
    <>
      {/* Recent Articles — shown only when there is at least one article. */}
      {articles && articles.length > 0 && (
      <section>
        <div className="flex items-center justify-between mb-6">
          <h2 className="font-display text-3xl font-bold">Recent Articles</h2>
          <Link href={`/${sport}/articles`} className="text-sm text-earl-400 hover:underline">
            All articles →
          </Link>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {articles.map((a) => (
              <Link
                key={a.id}
                href={`/${sport}/articles/${a.slug || a.id}`}
                className="block border border-white/10 rounded-xl p-4 bg-white/5 hover:bg-white/10 transition group"
              >
                {/* Team logos — horizontal row above the article, left to right */}
                {Array.isArray(a.teams) && a.teams.length > 0 && (
                  <div className="flex items-center gap-1.5 mb-2">
                    {a.teams.slice(0, 4).map((abbr) => (
                      <TeamLogo key={abbr} abbr={abbr} sport={sport} size={26} />
                    ))}
                  </div>
                )}
                <div className="text-xs text-gray-500 mt-1">
                  {formatDate(a.published_at)}
                  {a.author ? ` · by ${a.author}` : ""}
                </div>
                <h3 className="font-semibold text-white mt-1 line-clamp-2 group-hover:text-earl-400 transition">
                  {a.title}
                </h3>
                {a.summary && (
                  <p className="text-sm text-gray-400 mt-1 line-clamp-2">{a.summary}</p>
                )}
              </Link>
            ))}
          </div>
      </section>
      )}

      {/* Recent Game Previews — shown only when there is at least one qualifying preview
          (previous day, today, or future game). Hidden entirely when empty. */}
      {previews && previews.length > 0 && (
      <section>
        <div className="flex items-center justify-between mb-6">
          <h2 className="font-display text-3xl font-bold">Game Previews</h2>
          <Link
            href={`/${sport}/articles?tab=previews`}
            className="text-sm text-earl-400 hover:underline"
          >
            All previews →
          </Link>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {previews.map((p) => {
              return (
                <Link
                  key={p.writeup_id}
                  href={`/${sport}/articles/previews/${p.slug || p.writeup_id}`}
                  className="block border border-white/10 rounded-xl p-4 bg-white/5 hover:bg-white/10 transition"
                >
                  <div className="flex items-center gap-3">
                    <div className="flex items-center gap-1">
                      {p.away_abbr && (
                        <TeamLogo abbr={p.away_abbr} sport={sport} size={28} />
                      )}
                      {p.home_abbr && (
                        <TeamLogo abbr={p.home_abbr} sport={sport} size={28} />
                      )}
                    </div>
                    <div className="text-sm font-semibold text-white">
                      {p.away_abbr} @ {p.home_abbr}
                    </div>
                  </div>
                  <h3 className="font-semibold text-white mt-2 line-clamp-2">{p.title}</h3>
                  {p.summary && (
                    <p className="text-sm text-gray-400 mt-1 line-clamp-2">{p.summary}</p>
                  )}
                  <div className="text-xs text-gray-500 mt-3">
                    {formatGameTs(p.game_date) || formatDate(p.published_at)}
                  </div>
                </Link>
              );
            })}
          </div>
      </section>
      )}
    </>
  );
}
