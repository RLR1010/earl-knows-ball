"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useSeo } from "@/components/Seo";

interface PublicArticle {
  id: number;
  sport: string;
  title: string;
  summary: string | null;
  content: string;
  published_at: string | null;
  author?: string;
  slug?: string | null;
}

function setCanonical(url: string) {
  let link = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "canonical";
    document.head.appendChild(link);
  }
  link.href = url;
}

export default function SportArticleDetailPage({
  params,
}: {
  params: Promise<{ sport: string; slug: string }>;
}) {
  const [sport, setSport] = useState<string>("");
  const [article, setArticle] = useState<PublicArticle | null | "loading">("loading");

  const seoTitle = article && article !== "loading" ? `${article.title} — Earl Knows Ball` : `${sport.toUpperCase()} Article — Earl Knows Ball`;
  const seoDesc =
    article && article !== "loading"
      ? (article.summary || `${article.title} analysis and handicapping from Earl Knows Ball.`)
      : `Original handicapping article for the ${sport.toUpperCase()} — Earl Knows Ball.`;
  useSeo({
    title: seoTitle,
    description: seoDesc,
    keywords: `${sport} original article, ${sport} handicapping, ${sport} picks, betting analysis, Earl Knows Ball`,
  });

  useEffect(() => {
    let active = true;
    params
      .then(({ sport, slug }) => {
        setSport(sport);
        return fetch(`/api/original-articles/${sport}/${slug}`)
          .then((r) => (r.ok ? r.json() : { article: null }))
          .then((d) => active && setArticle(d.article ?? null))
          .catch(() => active && setArticle(null));
      })
      .catch(() => active && setArticle(null));
    return () => {
      active = false;
    };
  }, [params]);

  // Set the SEO canonical URL once we know the article's slug.
  useEffect(() => {
    if (article && article !== "loading" && article.slug && sport) {
      setCanonical(`https://earlknowsball.com/${sport}/articles/${article.slug}`);
    } else if (!article || article === null) {
      // Fall back to the URL slug if we couldn't resolve a stored slug.
      // (handled by params below)
      setCanonical("");
    }
  }, [article, sport]);

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
