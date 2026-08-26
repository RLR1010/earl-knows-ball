"use client";

import { useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useAuth } from "@/lib/auth-context";
import PremiumGate from "./PremiumGate";

interface ArticleContentProps {
  sport: string;
  articleId: number | string;
  visibility?: string;
  content: string | null;
  summary?: string | null;
  title: string;
}

/**
 * Renders an original article's markdown body with premium gating.
 *
 * Public articles render in full for everyone. Premium articles are
 * gated with the shared <PremiumGate /> (paywall for non-members). The
 * server pass uses the public tier so premium bodies never ship anonymously;
 * for subscribed members this component re-fetches the full body with
 * `?tier=premium` and renders it inside the gate.
 */
export default function ArticleContent({
  sport,
  articleId,
  visibility,
  content,
  summary,
  title,
}: ArticleContentProps) {
  const { user } = useAuth();
  const isPremiumMember =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";
  const isPremiumContent = visibility === "premium";

  const [fullContent, setFullContent] = useState<string | null>(null);
  useEffect(() => {
    if (!isPremiumContent || !isPremiumMember) {
      setFullContent(null);
      return;
    }
    let active = true;
    fetch(`/api/original-articles/${sport}/${articleId}?tier=premium`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const body = d?.article?.content ?? (d && typeof d.content === "string" ? d.content : null);
        if (active) setFullContent(body);
      })
      .catch(() => active && setFullContent(null));
    return () => {
      active = false;
    };
  }, [isPremiumContent, isPremiumMember, sport, articleId]);

  const body = fullContent ?? content;
  const renderBody = body ? (
    <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
  ) : (
    <p className="text-gray-400 italic">{summary || "Premium content."}</p>
  );

  if (!isPremiumContent) {
    return renderBody;
  }

  return (
    <PremiumGate
      title="Premium Article"
      message="Upgrade to Premium to read this full Daily Picks analysis."
    >
      {renderBody}
    </PremiumGate>
  );
}
