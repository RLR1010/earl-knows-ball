"use client";

import { useState, useEffect } from "react";
import { useParams } from "next/navigation";
import ParlayBuilder from "@/components/ParlayBuilder";
import PricingGateModal from "@/components/PricingGateModal";
import LoginModal from "@/components/LoginModal";
import { useAuth } from "@/lib/auth-context";
import { useSeo } from "@/components/Seo";

type Sport = "mlb" | "nfl" | "nba";

const SPORT_NAMES: Record<Sport, string> = {
  mlb: "MLB",
  nfl: "NFL",
  nba: "NBA",
};

export default function ParlayPage() {
  const params = useParams<{ sport: string }>();
  const sport = (params?.sport === "nfl" || params?.sport === "nba"
    ? params.sport
    : "mlb") as Sport;
  const sportName = SPORT_NAMES[sport];

  useSeo({
    title: `${sportName} Parlay Builder — Earl Knows Ball`,
    description: `Build ${sportName} parlays with Earl's model-calibrated probabilities and EV. Mix picks across MLB, NFL, and NBA onto one ticket.`,
    keywords: `${sportName} parlay, parlay builder, sports betting, Earl Knows Ball, model picks`,
  });

  const { user, loading: authLoading } = useAuth();
  const [loginOpen, setLoginOpen] = useState(false);
  const [pricingOpen, setPricingOpen] = useState(false);

  const isPremium =
    user?.subscription_tier === "premium" || user?.subscription_tier === "premium_yearly";

  // Auth gate: parlay building is a premium feature.
  //  - not logged in      -> LoginModal
  //  - logged in, no prem -> PricingGateModal (upgrade in-place)
  //  - logged in, prem    -> ParlayBuilder
  useEffect(() => {
    if (authLoading) return;
    if (!user) {
      setLoginOpen(true);
      setPricingOpen(false);
    } else if (!isPremium) {
      setLoginOpen(false);
      setPricingOpen(true);
    } else {
      setLoginOpen(false);
      setPricingOpen(false);
    }
  }, [authLoading, user, isPremium]);

  return (
    <div className="min-h-screen bg-[#070d18] text-zinc-100">
      <div className="mx-auto max-w-6xl px-4 py-8">
        <div className="mb-6">
          <h1 className="text-2xl font-bold text-zinc-50">
            {sportName} Parlay Builder
          </h1>
          <p className="mt-1 text-sm text-zinc-400">
            Stack Earl&apos;s model-calibrated legs. Mix picks across MLB, NFL, and
            NBA onto one ticket.
          </p>
        </div>
        {user && isPremium && <ParlayBuilder sport={sport} />}
      </div>

      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
      <PricingGateModal open={pricingOpen} onClose={() => setPricingOpen(false)} />
    </div>
  );
}
