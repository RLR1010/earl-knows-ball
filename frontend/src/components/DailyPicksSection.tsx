"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";

interface DailyPickArticle {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  content?: string | null;
  slug?: string | null;
  published_at: string | null;
  visibility?: string;
}

function formatRelative(ts: string | null): string {
  if (!ts) return "";
  const then = new Date(ts);
  const now = new Date();
  if (Number.isNaN(then.getTime())) return "";
  const diffSec = Math.round((now.getTime() - then.getTime()) / 1000);
  if (diffSec < 60) return "just now";
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return diffDay === 1 ? "yesterday" : `${diffDay}d ago`;
}

/**
 * Home-page section that surfaces the most recent article tagged with the
 * `daily_picks` destination section. Fetches via the same public original
 * articles endpoint as the Articles page, filtered to `section=daily_picks`.
 *
 * When no daily-picks article exists yet, renders a friendly empty state so
 * the section is stable once auto-generation begins publishing.
 */
export default function DailyPicksSection({ sport }: { sport: string }) {
  const [article, setArticle] = useState<DailyPickArticle | null | undefined>(undefined);
  const { user } = useAuth();
  const isPremiumMember =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";

  useEffect(() => {
    let active = true;
    fetch(`/api/original-articles/${sport}?section=daily_picks&limit=1`)
      .then((r) => (r.ok ? r.json() : { articles: [] }))
      .then((d) => {
        const list = Array.isArray(d.articles) ? d.articles : Array.isArray(d) ? d : [];
        const sorted = [...list].sort((a, b) =>
          String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")),
        );
        if (active) setArticle(sorted[0] ?? null);
      })
      .catch(() => active && setArticle(null));
    return () => {
      active = false;
    };
  }, [sport]);

  const excerpt = article?.summary;
  const isPremium = article?.visibility === "premium";
  const detailHref = `/${sport}/articles/${article?.slug || article?.id}`;

  return (
    <section
      aria-label="Daily Picks"
      className="relative overflow-hidden rounded-2xl border border-amber-500/25 bg-gradient-to-br from-amber-500/10 via-transparent to-earl-500/5 p-6 sm:p-8"
    >
      {/* subtle label */}
      <div className="flex items-center gap-2 mb-4">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-amber-400">
          <span className="h-1.5 w-1.5 rounded-full bg-amber-400 animate-pulse" />
          Daily Picks
        </span>
        {isPremium && (
          <span className="inline-flex items-center gap-1 rounded-full bg-gradient-to-r from-amber-500/25 to-earl-500/25 border border-amber-500/40 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-300">
            <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
              <path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4-6.2-4.5-6.2 4.5 2.4-7.4L2 9.4h7.6z" />
            </svg>
            Premium
          </span>
        )}
      </div>

      {article ? (
        <Link href={detailHref} className="group block">
          <h2 className="font-display text-3xl font-bold text-white group-hover:text-amber-300 transition-colors">
            {article.title}
          </h2>
          {excerpt && (
            <p className="mt-3 text-gray-300 line-clamp-2 max-w-3xl">{excerpt}</p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-4">
            <span className="inline-flex items-center gap-2 rounded-full bg-amber-500/15 border border-amber-500/30 px-4 py-1.5 text-sm font-semibold text-amber-300">
              {isPremium && !isPremiumMember ? "Unlock today's picks" : "Read today's picks"}
              <span aria-hidden="true">→</span>
            </span>
            {formatRelative(article.published_at) && (
              <span className="text-xs text-gray-500">
                Updated {formatRelative(article.published_at)}
              </span>
            )}
          </div>
        </Link>
      ) : article === null ? (
        <p className="text-gray-400">Today&apos;s picks are on the way — check back soon.</p>
      ) : (
        <div className="h-10 w-2/3 animate-pulse rounded bg-white/10" />
      )}
    </section>
  );
}
