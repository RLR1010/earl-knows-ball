import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

interface GamePreviewDetail {
  id: number;
  slug?: string | null;
  game_id: number;
  title: string;
  content: string;
  matchup: string;
  status: string;
  version: number;
  is_historical: boolean;
  generated_by: string | null;
  published_at: string | null;
  created_at: string | null;
  game_date: string | null;
}

const VALID_SPORTS = ["nfl", "nba", "mlb"];

const BACKEND_BASE =
  process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || "http://localhost:8001";

async function fetchPreview(
  sport: string,
  identifier: string
): Promise<GamePreviewDetail | null> {
  try {
    const res = await fetch(`${BACKEND_BASE}/writeups/${sport}/${identifier}?tier=public`, {
      cache: "no-store",
    });
    if (!res.ok) return null;
    const data = await res.json();
    return (data?.writeup ?? data) || null;
  } catch {
    return null;
  }
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

export async function generateMetadata({
  params,
}: {
  params: Promise<{ sport: string; identifier: string }>;
}): Promise<Metadata> {
  const { sport, identifier } = await params;
  const preview = await fetchPreview(sport, identifier);

  if (!preview) {
    return {
      title: `Game Preview — ${sport.toUpperCase()} | Earl Knows Ball`,
      description: `AI handicapping game preview for the ${sport.toUpperCase()} — Earl Knows Ball.`,
    };
  }

  const title = preview.title?.trim()
    ? preview.title.trim()
    : preview.matchup
    ? `${preview.matchup} Game Preview`
    : `Game Preview`;
  const slug = preview.slug || identifier;
  const url = `https://earlknowsball.com/${sport}/articles/previews/${slug}`;

  return {
    title,
    description: `${preview.title?.trim() || preview.matchup || sport.toUpperCase()} game preview and AI handicapping analysis from Earl Knows Ball.`,
    alternates: { canonical: url },
    openGraph: {
      title,
      description: `${preview.matchup || sport.toUpperCase()} game preview and AI handicapping from Earl Knows Ball.`,
      url,
      siteName: "Earl Knows Ball",
      type: "article",
      publishedTime: preview.published_at || undefined,
    },
  };
}

export default async function GamePreviewDetailPage({
  params,
}: {
  params: Promise<{ sport: string; identifier: string }>;
}) {
  const { sport, identifier } = await params;
  const normalizedSport = sport.toLowerCase();

  if (!VALID_SPORTS.includes(normalizedSport)) {
    notFound();
  }

  const preview = await fetchPreview(normalizedSport, identifier);

  if (!preview) {
    notFound();
  }

  const title = preview.title?.trim() || (preview.matchup ? `${preview.matchup} Game Preview` : "Game Preview");
  const dateStr = formatDate(preview.game_date || preview.published_at);

  return (
    <div className="max-w-3xl mx-auto px-4 py-12">
      <div className="text-sm text-gray-500 mb-6">
        <Link href={`/${normalizedSport}`} className="hover:text-earl-400 transition">
          {normalizedSport.toUpperCase()}
        </Link>
        <span className="mx-2 text-gray-600">·</span>
        <Link href={`/${normalizedSport}/articles?tab=previews`} className="hover:text-earl-400 transition">
          Game Previews
        </Link>
      </div>

      <article>
        <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-2">{title}</h1>
        <div className="text-sm text-gray-500 mb-8">
          <span className="text-gray-300">by Earl</span>
          <span className="mx-2 text-gray-600">·</span>
          {dateStr}
        </div>

        <div className="writeup-content">
          <div className="text-gray-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content}</ReactMarkdown>
          </div>
        </div>
      </article>

      <div className="mt-12 pt-6 border-t border-white/10 text-center">
        <Link
          href={`/${normalizedSport}/articles?tab=previews`}
          className="text-sm text-earl-400 hover:text-earl-300 transition"
        >
          More {normalizedSport.toUpperCase()} game previews →
        </Link>
      </div>
    </div>
  );
}
