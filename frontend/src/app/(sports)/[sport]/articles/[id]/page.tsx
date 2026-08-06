"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface PublicArticle {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  content: string;
  published_at: string | null;
  author?: string;
}

export default function SportArticleDetailPage({
  params,
}: {
  params: Promise<{ sport: string; id: string }>;
}) {
  const [sport, setSport] = useState<string>("");
  const [article, setArticle] = useState<PublicArticle | null | "loading">("loading");

  useEffect(() => {
    let active = true;
    params
      .then(({ sport, id }) => {
        setSport(sport);
        return fetch(`/api/original-articles/${sport}/${id}`)
          .then((r) => (r.ok ? r.json() : { article: null }))
          .then((d) => active && setArticle(d.article ?? null))
          .catch(() => active && setArticle(null));
      })
      .catch(() => active && setArticle(null));
    return () => {
      active = false;
    };
  }, [params]);

  if (article === "loading") {
    return (
      <div className="max-w-4xl mx-auto px-4 py-24 text-center text-gray-500">Loading…</div>
    );
  }

  if (!article) {
    return (
      <div className="max-w-4xl mx-auto px-4 py-24 text-center text-gray-500">
        Article not found.
      </div>
    );
  }

  const dateStr = article.published_at
    ? new Date(article.published_at).toLocaleDateString(undefined, {
        year: "numeric",
        month: "long",
        day: "numeric",
      })
    : "";

  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <div className="mb-6 text-sm text-gray-400">
        <Link href={`/${sport}`} className="hover:text-earl-400 transition">
          ← {sport.toUpperCase()}
        </Link>
        <span className="mx-2 text-gray-600">/</span>
        <Link href={`/${sport}/articles`} className="hover:text-earl-400 transition">
          Articles
        </Link>
      </div>

      <article>
        <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-2">{article.title}</h1>
        {(article.author || dateStr) && (
          <div className="text-sm text-gray-500 mb-8">
            {article.author && <span className="text-gray-300">by {article.author}</span>}
            {article.author && dateStr && <span className="mx-2 text-gray-600">·</span>}
            {dateStr}
          </div>
        )}

        <div className="writeup-content">
          <div className="text-gray-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{article.content}</ReactMarkdown>
          </div>
        </div>
      </article>

      <div className="mt-12 pt-6 border-t border-white/10 text-center">
        <Link
          href={`/${sport}/articles`}
          className="text-sm text-earl-400 hover:text-earl-300 transition"
        >
          More {sport.toUpperCase()} articles →
        </Link>
      </div>
    </div>
  );
}
