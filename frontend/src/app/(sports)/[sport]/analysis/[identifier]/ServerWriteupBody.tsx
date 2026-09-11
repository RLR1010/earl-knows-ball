import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import GamePickCard from "@/components/GamePickCard";
import FreePickUpsellCta from "@/components/FreePickUpsellCta";
import type { WriteupContent } from "@/lib/seo-content";

const SPORT_NAMES: Record<string, string> = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

function formatDate(value?: string | null): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
    timeZone: "UTC",
  });
}

/**
 * SERVER-RENDERED free-feature writeup body.
 *
 * This exists purely for SEO: the free pick is the ONE class of writeup we want
 * an anonymous crawler to index, so its full body must be in the initial HTML
 * (first-wave crawl), not hydrated client-side. It mirrors the free-feature
 * branch of AnalysisClient.tsx exactly — same markup/classes — so there is no
 * visual difference between the SSR path and the client path.
 *
 * Rendered only when the server-side content fetch returned 200 (see
 * `writeupContent`). Interactive pieces (GamePickCard) remain client components.
 */
export default function ServerWriteupBody({
  sport,
  identifier,
  content,
}: {
  sport: string;
  identifier: string;
  content: WriteupContent;
}) {
  const data = content.data ?? {};
  const title =
    data.title?.trim() || (data.matchup ? `${data.matchup} Analysis` : "Analysis");
  const dateStr = formatDate(data.game_date || data.published_at);
  const body = data.content || data.body || "";

  return (
    <div className="max-w-4xl mx-auto px-4 py-12">
      <div className="text-sm text-gray-500 mb-6">
        <Link href={`/${sport}`} className="hover:text-earl-400 transition">
          {SPORT_NAMES[sport.toLowerCase()] ?? sport.toUpperCase()}
        </Link>
        <span className="mx-2 text-gray-600">·</span>
        <Link href={`/${sport}/analysis`} className="hover:text-earl-400 transition">
          Analysis
        </Link>
      </div>

      <article>
        <GamePickCard sport={sport} gameId={data.game_id} />
        <span className="mb-4 mt-6 inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-green-500 to-earl-500 px-3 py-1 text-xs font-black uppercase tracking-widest text-white shadow-lg">
          Free pick of the game — no subscription required
        </span>
        <h1 className="text-3xl md:text-4xl font-bold tracking-tight mb-2">{title}</h1>
        <div className="text-sm text-gray-500 mb-8">
          <span className="text-gray-300">by Earl</span>
          <span className="mx-2 text-gray-600">·</span>
          {dateStr}
        </div>
        <div className="writeup-content">
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              h3: ({ children }) => (
                <h3 className="mt-8 mb-2 text-xl font-bold text-white">{children}</h3>
              ),
              strong: ({ children }) => <strong className="text-white">{children}</strong>,
              li: ({ children }) => <li className="ml-4 list-disc my-1">{children}</li>,
              p: ({ children }) => <p className="my-3">{children}</p>,
            }}
          >
            {body}
          </ReactMarkdown>
        </div>
      </article>
      <FreePickUpsellCta />

      <div className="mt-12 pt-6 border-t border-white/10 text-center">
        <Link
          href={`/${sport}/analysis`}
          className="text-sm text-earl-400 hover:text-earl-300 transition"
        >
          More {SPORT_NAMES[sport.toLowerCase()] ?? sport.toUpperCase()} analysis →
        </Link>
      </div>
    </div>
  );
}
