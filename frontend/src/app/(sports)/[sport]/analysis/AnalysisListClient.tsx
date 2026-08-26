"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useSeo } from "@/components/Seo";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "@/components/LoginModal";
import TeamLogo from "@/components/TeamLogo";

interface GamePreview {
  writeup_id: number;
  slug: string | null;
  game_id: number;
  title: string;
  slugTitle: string;
  published_at: string | null;
  game_date: string | null;
  game_day: string; // YYYY-MM-DD key
  matchup: string;
  away_abbr: string | null;
  home_abbr: string | null;
}

const SPORT_NAME: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

function toDayKey(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
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
  return d.toLocaleDateString("en-US", {
    timeZone: "UTC",
    weekday: "short",
    month: "long",
    day: "numeric",
    year: "numeric",
  });
}

function parseTeamAbbrs(s: string | null): string | null {
  if (!s) return null;
  const t = s.trim();
  return t ? t.toUpperCase() : null;
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

      const title = typeof w.title === "string" && w.title.trim() ? w.title : "Game Preview";

      // MLB/NFL: matchup is "AWAY @ HOME"; NBA provides home_abbr/away_abbr.
      const matchup =
        (typeof w.matchup === "string" && w.matchup.trim()) ||
        (w.away_abbr && w.home_abbr ? `${w.away_abbr} @ ${w.home_abbr}` : "");
      const m = /^([A-Za-z0-9]+)\s*@\s*([A-Za-z0-9]+)$/.exec(matchup);

      return {
        writeup_id: id,
        slug: typeof w.slug === "string" && w.slug ? w.slug : null,
        game_id,
        title,
        slugTitle: w.slug_title?.toString() || title,
        published_at: published ? String(published) : null,
        game_date: gameDateStr,
        game_day: gameDateStr ? toDayKey(gameDateStr) : "",
        matchup,
        away_abbr: parseTeamAbbrs((w.away_abbr as string) ?? (m?.[1] ?? null)),
        home_abbr: parseTeamAbbrs((w.home_abbr as string) ?? (m?.[2] ?? null)),
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

export default function SportAnalysisPage({ params }: { params: Promise<{ sport: string }> }) {
  const [sport, setSport] = useState<string>("");
  const [previews, setPreviews] = useState<GamePreview[] | null>(null);
  const [loginModalOpen, setLoginModalOpen] = useState(false);

  const { user } = useAuth();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const router = useRouter();

  const requestedDate = searchParams.get("date"); // YYYY-MM-DD or null

  useEffect(() => {
    let active = true;
    params.then(({ sport }) => {
      setSport(sport);
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
  const isPremium = user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";

  useSeo({
    title: `Analysis — ${name} | Earl Knows Ball`,
    description: `Premium AI handicapping analysis for ${name} from Earl Knows Ball — detailed game breakdowns and picks for premium members.`,
  });

  const days = useMemo<string[]>(() => {
    const set = new Set<string>();
    (previews ?? []).forEach((p) => p.game_day && set.add(p.game_day));
    return Array.from(set).sort();
  }, [previews]);

  const selectedDay = useMemo(() => {
    const todayKey = todayEastern();
    if (requestedDate && days.includes(requestedDate)) return requestedDate;
    if (days.includes(todayKey)) return todayKey;
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
    return list.sort((a, b) => Date.parse(a.game_date || "") - Date.parse(b.game_date || ""));
  }, [previews, selectedDay]);

  const prevDay = days[days.indexOf(selectedDay) - 1] || null;
  const nextDay = days[days.indexOf(selectedDay) + 1] || null;

  const jumpToDay = (day: string | null) => {
    if (!day) return;
    const next = new URLSearchParams(searchParams.toString());
    next.set("date", day);
    router.replace(`${pathname}?${next.toString()}`, { scroll: false });
  };

  const navBtnClass =
    "px-3 py-2 text-sm font-medium rounded-lg border border-white/10 text-gray-300 hover:bg-white/[0.05] hover:text-white transition disabled:opacity-40 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-gray-300";

  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <div className="mb-8">
        <h1 className="font-display text-4xl font-bold tracking-tight mb-2">
          {name} Analysis
        </h1>
        <p className="text-gray-400">
          Premium AI handicapping breakdowns for every {name} game — exclusive to Earl Knows Ball members.
        </p>
      </div>

      {previews === null ? (
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
                {dayPreviews.length} game{dayPreviews.length === 1 ? "" : "s"}
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
              No analysis for this date.
            </div>
          ) : (
            <ul className="divide-y divide-white/10 border border-white/10 rounded-lg bg-white/[0.02]">
              {dayPreviews.map((p) => (
                <li key={p.writeup_id}>
                  <AnalysisItem
                    preview={p}
                    sport={sport}
                    isPremium={isPremium}
                    loggedIn={!!user}
                    onOpenLogin={() => setLoginModalOpen(true)}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {!user && (
        <LoginModal open={loginModalOpen} onClose={() => setLoginModalOpen(false)} />
      )}
    </div>
  );
}

interface AnalysisItemProps {
  preview: GamePreview;
  sport: string;
  isPremium: boolean;
  loggedIn: boolean;
  onOpenLogin: () => void;
}

function AnalysisItem({ preview, sport, isPremium, loggedIn, onOpenLogin }: AnalysisItemProps) {
  const href = `/${sport}/analysis/${preview.slug || preview.writeup_id}`;

  // Title replacement based on auth state.
  let title: string;
  let titleStyle = "text-lg font-semibold";
  if (isPremium) {
    title = preview.title;
    titleStyle += " group-hover:text-earl-400 transition";
  } else if (!loggedIn) {
    title = "Premium Content — Sign In To Get Started";
    titleStyle += " text-gray-200";
  } else {
    title = "Premium Content — Upgrade Now";
    titleStyle += " text-gray-200";
  }

  // Logos row — logos on the side (left) for the listing.
  const showLogos = preview.away_abbr || preview.home_abbr;
  const body = (
    <div className="flex flex-col sm:flex-row sm:items-center gap-3">
      <div className="flex items-center gap-2 sm:flex-col sm:gap-1 sm:shrink-0">
        {showLogos && (
          <>
            {preview.away_abbr && (
              <TeamLogo abbr={preview.away_abbr} sport={sport} size={34} />
            )}
            <span className="text-sm font-bold text-gray-400">vs</span>
            {preview.home_abbr && (
              <TeamLogo abbr={preview.home_abbr} sport={sport} size={34} />
            )}
          </>
        )}
      </div>

      <div className="min-w-0">
        <div className="text-sm font-semibold text-earl-400 mb-1">{preview.matchup}</div>
        <div className={titleStyle}>{title}</div>
        <div className="text-sm text-gray-500 mt-1">
          {formatGameTs(preview.game_date) || formatDate(preview.published_at) || "Recently"}
        </div>
      </div>
    </div>
  );

  // For non-premium users, clicking must trigger the right CTA instead of loading content.
  const className = "block px-5 py-4 hover:bg-white/[0.04] transition group";

  if (isPremium) {
    return (
      <li>
        <Link href={href} className={className}>
          {body}
        </Link>
      </li>
    );
  }

  if (!loggedIn) {
    return (
      <li>
        <button type="button" onClick={onOpenLogin} className={`${className} w-full text-left`}>
          {body}
        </button>
      </li>
    );
  }

  // Logged in but not premium → go to pricing.
  return (
    <li>
      <Link href={`/pricing`} className={className}>
        {body}
      </Link>
    </li>
  );
}
