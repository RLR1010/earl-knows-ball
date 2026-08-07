import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
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
  slug?: string | null;
  seo_description?: string | null;
  seo_keywords?: string | null;
}

const VALID_SPORTS = ["nfl", "nba", "mlb"];

const BACKEND_BASE =
  process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8001";

async function fetchArticle(sport: string, slug: string): Promise<PublicArticle | null> {
  try {
    const res = await fetch(`${BACKEND_BASE}/original-articles/${sport}/${slug}`, {
      cache: "no-store",
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data?.article ?? null;
  } catch {
    return null;
  }
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ sport: string; slug: string }>;
}): Promise<Metadata> {
  const { sport, slug } = await params;
  const article = await fetchArticle(sport, slug);

  if (!article) {
    // Give crawlers a useful default title/description even for a missing article.
    return {
      title: `${sport.toUpperCase()} Article — Earl Knows Ball`,
      description: `Original handicapping article for the ${sport.toUpperCase()} — Earl Knows Ball.`,
    };
  }

  const brandTitle = `${article.title} — Earl Knows Ball`;
  const description =
    article.seo_description ||
    article.summary ||
    `${article.title} analysis and handicapping from Earl Knows Ball.`;
  const keywords = article.seo_keywords || `${sport} original article, ${sport} handicapping, ${sport} picks, betting analysis, Earl Knows Ball`;
  const url = `https://earlknowsball.com/${sport}/articles/${article.slug || slug}`;

  return {
    // Bare title; root layout template appends "| Earl Knows Ball".
    title: article.title,
    description,
    keywords,
    alternates: { canonical: url },
    openGraph: {
      title: brandTitle,
      description,
      url,
      siteName: "Earl Knows Ball",
      type: "article",
      publishedTime: article.published_at || undefined,
      authors: article.author ? [article.author] : undefined,
    },
  };
}

export default async function SportArticleDetailPage({
  params,
}: {
  params: Promise<{ sport: string; slug: string }>;
}) {
  const { sport, slug } = await params;
  const normalizedSport = sport.toLowerCase();

  if (!VALID_SPORTS.includes(normalizedSport)) {
    notFound();
  }

  const article = await fetchArticle(normalizedSport, slug);
  if (!article) {
    notFound();
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
        <Link href={`/${normalizedSport}`} className="hover:text-earl-400 transition">
          ← {normalizedSport.toUpperCase()}
        </Link>
        <span className="mx-2 text-gray-600">/</span>
        <Link
          href={`/${normalizedSport}/articles`}
          className="hover:text-earl-400 transition"
        >
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
          href={`/${normalizedSport}/articles`}
          className="text-sm text-earl-400 hover:text-earl-300 transition"
        >
          More {normalizedSport.toUpperCase()} articles →
        </Link>
      </div>
    </div>
  );
}
