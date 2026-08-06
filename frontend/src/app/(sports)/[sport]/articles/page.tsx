"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

interface PublicArticle {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  published_at: string | null;
  author?: string;
}

const SPORT_NAME: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

export default function SportArticlesPage({ params }: { params: Promise<{ sport: string }> }) {
  const [sport, setSport] = useState<string>("");
  const [articles, setArticles] = useState<PublicArticle[] | null>(null);

  useEffect(() => {
    let active = true;
    params.then(({ sport }) => {
      setSport(sport);
      fetch(`/api/original-articles/${sport}`)
        .then((r) => (r.ok ? r.json() : { articles: [] }))
        .then((d) => active && setArticles(d.articles ?? []))
        .catch(() => active && setArticles([]));
    });
    return () => {
      active = false;
    };
  }, [params]);

  const name = SPORT_NAME[sport] || sport.toUpperCase();

  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <div className="mb-8">
        <h1 className="font-display text-4xl font-bold tracking-tight mb-2">
          Original Articles — <span className="text-earl-400">{name}</span>
        </h1>
        <p className="text-gray-400">
          Independent analysis and commentary written by our handicapping engine.
        </p>
      </div>

      {articles === null ? (
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
                href={`/${sport}/articles/${a.id}`}
                className="block px-5 py-4 hover:bg-white/[0.04] transition group"
              >
                <div className="text-sm text-gray-500 mb-1">
                  {a.published_at
                    ? new Date(a.published_at).toLocaleDateString(undefined, {
                        year: "numeric",
                        month: "long",
                        day: "numeric",
                      })
                    : "Recently"}
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
      )}
    </div>
  );
}
