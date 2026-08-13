"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useAuth } from "@/lib/auth-context";

interface AllSportArticle {
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
 * Dedicated site-wide block on the Earl Knows Ball home page that surfaces
 * "All" (cross-sport / non-sport-specific) editorial articles. Only shows
 * articles with `sport === 'all'`. Public articles render title + teaser with
 * a link to read; premium articles additionally show a Premium badge + lock
 * and are gated on the article detail page for non-members.
 */
export default function SiteEditorialSection() {
  const [articles, setArticles] = useState<AllSportArticle[] | null | undefined>(undefined);
  const { user } = useAuth();
  const isPremiumMember =
    user?.subscription_tier === "premium" || user?.subscription_tier === "ultimate";

  useEffect(() => {
    let active = true;
    fetch(`/api/original-articles/all?limit=6`)
      .then((r) => (r.ok ? r.json() : { articles: [] }))
      .then((d) => {
        const list = Array.isArray(d.articles) ? d.articles : Array.isArray(d) ? d : [];
        const sorted = [...list].sort((a, b) =>
          String(b.published_at ?? "").localeCompare(String(a.published_at ?? "")),
        );
        if (active) setArticles(sorted);
      })
      .catch(() => active && setArticles(null));
    return () => {
      active = false;
    };
  }, []);

  return (
    <section aria-label="Latest Editorial" className="py-4">
      <div className="flex items-center justify-between mb-5">
        <h2 className="font-display text-2xl font-bold text-white tracking-tight flex items-center gap-2">
          <span className="inline-flex h-6 items-center rounded-md bg-gradient-to-r from-earl-600 to-earl-500 px-2 text-xs font-black uppercase tracking-widest text-white">
            All Sports
          </span>
          Latest Editorial
        </h2>
      </div>

      {articles === undefined ? (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {[0, 1, 2].map((i) => (
            <div key={i} className="h-28 animate-pulse rounded-xl bg-white/5" />
          ))}
        </div>
      ) : articles === null || articles.length === 0 ? (
        <div className="rounded-xl border border-white/10 bg-white/[0.03] p-8 text-center">
          <p className="text-gray-400">
            Editorial roundups across the NFL, NBA, and MLB are on the way — check back soon.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {articles.map((a) => {
            const isPremium = a.visibility === "premium" || a.visibility === "ultimate";
            const href = `/${a.sport}/articles/${a.slug || a.id}`;
            return (
              <Link
                key={a.id}
                href={href}
                className="group flex flex-col rounded-xl border border-white/10 bg-white/[0.03] p-5 transition hover:border-earl-500/40 hover:bg-white/[0.05]"
              >
                <div className="mb-2 flex items-center gap-2">
                  {isPremium && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-gradient-to-r from-amber-500/25 to-earl-500/25 border border-amber-500/40 px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider text-amber-300">
                      <svg width="9" height="9" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                        <path d="M12 2l2.4 7.4H22l-6.2 4.5 2.4 7.4-6.2-4.5-6.2 4.5 2.4-7.4L2 9.4h7.6z" />
                      </svg>
                      Premium
                    </span>
                  )}
                  {a.published_at && (
                    <span className="text-xs text-gray-500">{formatRelative(a.published_at)}</span>
                  )}
                </div>
                <h3 className="font-display text-lg font-semibold text-white group-hover:text-earl-300 transition-colors line-clamp-2">
                  {a.title}
                </h3>
                {a.summary && (
                  <p className="mt-2 text-sm text-gray-400 line-clamp-3">{a.summary}</p>
                )}
                <span className="mt-3 inline-flex items-center gap-1.5 text-sm font-semibold text-earl-400">
                  {isPremium && !isPremiumMember ? "Unlock with Premium" : "Read article"}
                  <span aria-hidden="true">→</span>
                </span>
              </Link>
            );
          })}
        </div>
      )}
    </section>
  );
}
