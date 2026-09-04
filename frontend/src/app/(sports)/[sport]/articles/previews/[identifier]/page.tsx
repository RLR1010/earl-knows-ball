import Link from "next/link";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { COMPUTE_URL } from "@/lib/backend-url";
import PremiumArticleCta from "@/components/PremiumArticleCta";

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
  preview_image?: string | null;
  social_caption?: string | null;
  team_cards?: null | {
    as_of: string | null;
    away: TeamCard;
    home: TeamCard;
  };
};

type TeamCard = {
  abbr: string;
  name: string;
  meta: string;
  ra5: string;
  rs5: string;
  avg10: string;
  logo_url?: string | null;
};

const VALID_SPORTS = ["nfl", "nba", "mlb"];

const BACKEND_BASE =
  process.env.BACKEND_URL || process.env.NEXT_PUBLIC_BACKEND_URL || COMPUTE_URL;

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

  // Social/og card: absolute URL only when the backend supplied preview_image.
  const ogImage = preview.preview_image
    ? `https://earlknowsball.com${preview.preview_image}`
    : null;

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
      ...(ogImage ? { images: [{ url: ogImage, width: 1600, height: 900, alt: title }] } : {}),
    },
    ...(ogImage
      ? { twitter: { card: "summary_large_image", title, images: [ogImage] } }
      : {}),
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
    <div className="max-w-6xl mx-auto px-4 py-12">
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
        {/* Title + byline block with small Earl pic to its right (hidden on small screens) */}
        <div className="flex flex-row items-center justify-between gap-6">
          <div className="min-w-0">
            <h1 className="text-3xl md:text-4xl font-bold tracking-tight">{title}</h1>
            <div className="mt-3 text-sm text-gray-500">
              <span className="text-gray-300">by Earl</span>
              <span className="mx-2 text-gray-600">·</span>
              {dateStr}
            </div>
          </div>
          {/* Earl pix to the right of the title + byline block (desktop only) */}
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src="/earl-card.png"
            alt="Earl Knows Ball"
            className="hidden md:block h-28 w-auto shrink-0 select-none rounded-lg shadow-lg"
          />
        </div>

        {preview.team_cards && (
          <div className="mt-8">
            {/* three columns: Away-team card | @ | Home-team card (stack on small screens) */}
            <div className="sc-matchup">
              <div className="sc-cell">
                <div className="sc-hdr">
                  {preview.team_cards.away.logo_url ? (
                    <div className="sc-logo-backer">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={preview.team_cards.away.logo_url} alt={`${preview.team_cards.away.name} logo`} className="sc-logo" />
                    </div>
                  ) : null}
                  <div className="min-w-0">
                    <div className="sc-name">{preview.team_cards.away.name}</div>
                    <div className="sc-meta">{preview.team_cards.away.meta}</div>
                  </div>
                </div>
                <div className="sc-stats">
                  <div className="sc-srow">
                    <span className="sc-lbl">Runs Allowed/G Last 5</span>
                    <span className="sc-val">{preview.team_cards.away.ra5}</span>
                  </div>
                  <div className="sc-srow">
                    <span className="sc-lbl">Runs Scored/G Last 5</span>
                    <span className="sc-val">{preview.team_cards.away.rs5}</span>
                  </div>
                  <div className="sc-srow">
                    <span className="sc-lbl">AVG Last 10</span>
                    <span className="sc-val">{preview.team_cards.away.avg10}</span>
                  </div>
                </div>
              </div>

              <div className="sc-vs" aria-hidden="true">
                @
              </div>

              <div className="sc-cell">
                <div className="sc-hdr">
                  {preview.team_cards.home.logo_url ? (
                    <div className="sc-logo-backer">
                      {/* eslint-disable-next-line @next/next/no-img-element */}
                      <img src={preview.team_cards.home.logo_url} alt={`${preview.team_cards.home.name} logo`} className="sc-logo" />
                    </div>
                  ) : null}
                  <div className="min-w-0">
                    <div className="sc-name">{preview.team_cards.home.name}</div>
                    <div className="sc-meta">{preview.team_cards.home.meta}</div>
                  </div>
                </div>
                <div className="sc-stats">
                  <div className="sc-srow">
                    <span className="sc-lbl">Runs Allowed/G Last 5</span>
                    <span className="sc-val">{preview.team_cards.home.ra5}</span>
                  </div>
                  <div className="sc-srow">
                    <span className="sc-lbl">Runs Scored/G Last 5</span>
                    <span className="sc-val">{preview.team_cards.home.rs5}</span>
                  </div>
                  <div className="sc-srow">
                    <span className="sc-lbl">AVG Last 10</span>
                    <span className="sc-val">{preview.team_cards.home.avg10}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="writeup-content max-w-5xl">
          <div className="text-gray-300 leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{preview.content}</ReactMarkdown>
          </div>
        </div>

        {/* Premium upsell for non-members (hidden for logged-in premium/yearly) */}
        <PremiumArticleCta />
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
