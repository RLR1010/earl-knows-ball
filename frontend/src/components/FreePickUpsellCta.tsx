"use client";

import { useAuth } from "@/lib/auth-context";

/**
 * Post-article membership upsell.
 *
 * Shown ONLY to readers who are NOT already Premium members (guests +
 * free/trial users). Pushes the $1.95 / 2-day trial checkout. Hidden for
 * logged-in premium & yearly subscribers so they are never nagged.
 *
 * Two copy variants (same look, same gating):
 *  - "freepick"  (default): end of the Free Pick giveaway article.
 *  - "winners"            : end of Earl's Winners recap articles.
 */
export default function FreePickUpsellCta({
  variant = "freepick",
}: {
  variant?: "freepick" | "winners";
} = {}) {
  const { user, loading } = useAuth();

  // Let auth resolve first — never flash an upsell to a paying member.
  if (loading) return null;

  const isPremium =
    user?.subscription_tier === "premium" ||
    user?.subscription_tier === "premium_yearly";
  if (isPremium) return null;

  const headline =
    variant === "winners" ? (
      <>
        Earl cashed these winners. <br className="hidden sm:block" />
        Get his picks before every game.
      </>
    ) : (
      <>
        Loved this free pick? <br className="hidden sm:block" />
        Get Earl&rsquo;s picks on every game.
      </>
    );

  const body =
    variant === "winners" ? (
      <>
        Those were Earl&rsquo;s calls &mdash; see the full breakdown behind every
        pick and get his best bets for the entire season. Start your 2-day
        trial and see &mdash;{" "}
        <span className="font-bold text-emerald-400">just $1.95.</span>
      </>
    ) : (
      <>
        Unlock full betting breakdowns, matchup edges, and Earl&rsquo;s best
        bets for the entire season. Start your 2-day trial and see &mdash;{" "}
        <span className="font-bold text-emerald-400">just $1.95.</span>
      </>
    );

  return (
    <div className="mt-12 overflow-hidden rounded-2xl border border-emerald-500/30 bg-gradient-to-br from-[#08291b] via-[#071f2b] to-[#0a0d12] p-8 text-center shadow-2xl">
      <div className="inline-flex items-center gap-2 rounded-full bg-emerald-500/15 px-3 py-1 text-xs font-black uppercase tracking-widest text-emerald-400">
        Premium Membership
      </div>
      <h2 className="mt-4 text-2xl md:text-3xl font-bold text-white">
        {headline}
      </h2>
      <p className="mx-auto mt-3 max-w-xl text-sm text-gray-300 md:text-base">
        {body}
      </p>
      <a
        href="/premium"
        className="mt-6 inline-block w-full max-w-xs rounded-xl bg-emerald-500 py-3.5 text-center font-black text-white shadow-lg transition hover:bg-emerald-400"
      >
        Start Your $1.95 Trial
      </a>
      <p className="mt-3 text-xs text-gray-500">
        Premium analysis for every MLB, NFL &amp; NBA game. Cancel anytime.
      </p>
    </div>
  );
}
