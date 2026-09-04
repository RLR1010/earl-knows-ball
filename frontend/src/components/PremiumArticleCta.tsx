"use client";

import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "@/components/LoginModal";

/**
 * Upsell call-to-action shown to readers who are NOT premium members, placed
 * beneath the article body on the public preview page. Hidden entirely for
 * logged-in premium/yearly subscribers so they are never nagged.
 */
export default function PremiumArticleCta() {
  const { user, loading } = useAuth();
  const [loginOpen, setLoginOpen] = useState(false);

  // While auth is still resolving, don't flash the CTA to premium members.
  if (loading) return null;

  const isPremium =
    user?.subscription_tier === "premium" ||
    user?.subscription_tier === "premium_yearly";

  // Already a paying member — no upsell.
  if (isPremium) return null;

  return (
    <>
      <div className="mt-10 rounded-2xl border border-white/10 bg-white/[0.04] p-8 text-center">
        <div className="w-12 h-0.5 bg-earl-600 mx-auto mb-5 rounded-full" />
        <h2 className="text-xl md:text-2xl font-bold text-gray-100 mb-3">
          See our detailed analysis and picks for this game!
        </h2>
        <p className="text-gray-300 text-sm md:text-base mb-6 max-w-xl mx-auto">
          Become a Premium Member for full breakdowns, matchup edges, and
          betting analysis on every game — all season long.
        </p>
        {user ? (
          <a
            href="/pricing"
            className="inline-block w-full max-w-xs py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
          >
            Become a Premium Member
          </a>
        ) : (
          <button
            onClick={() => setLoginOpen(true)}
            className="w-full max-w-xs py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
          >
            Become a Premium Member
          </button>
        )}
      </div>
      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
    </>
  );
}
