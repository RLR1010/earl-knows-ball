"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import CheckoutModal from "@/components/CheckoutModal";

interface Plan {
  id: string;
  name: string;
  slug: string;
  description: string | null;
  price_cents: number;
  currency: string;
  interval: string;
  trial_days: number;
  features: string[];
  is_active: boolean;
  sort_order: number;
}

const formatPrice = (cents: number, currency: string, interval: string) => {
  const amount = (cents / 100).toFixed(2);
  const symbol = currency === "usd" ? "$" : currency.toUpperCase() + " ";
  return { amount: `${symbol}${amount}`, period: interval === "month" ? "/mo" : "/yr" };
};

export default function PricingPage() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loadingPlans, setLoadingPlans] = useState(true);
  const [checkingOut, setCheckingOut] = useState<string | null>(null);
  const [checkoutUrl, setCheckoutUrl] = useState<string | null>(null);
  const [checkoutError, setCheckoutError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/subscriptions/plans")
      .then((r) => r.json())
      .then((data) => setPlans(data.sort((a: Plan, b: Plan) => a.sort_order - b.sort_order)))
      .catch(console.error)
      .finally(() => setLoadingPlans(false));
  }, []);

  const handleSubscribe = async (planId: string) => {
    setCheckingOut(planId);
    setCheckoutError(null);
    try {
      const token = localStorage.getItem("earl_token");
      if (!token) {
        window.location.href = `/auth?redirect=/pricing&plan=${planId}`;
        return;
      }

      const res = await fetch("/api/subscriptions/checkout", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          plan_id: planId,
          success_url: `${window.location.origin}/profile?subscription=success`,
          cancel_url: `${window.location.origin}/pricing`,
          ui_mode: "hosted",
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Checkout failed");
      }

      const data = await res.json();

      if (data.url) {
        setCheckoutUrl(data.url);
      } else {
        throw new Error(data.message || "No checkout session returned");
      }
    } catch (e: any) {
      setCheckoutError(e.message);
    } finally {
      setCheckingOut(null);
    }
  };

  const handleCheckoutClose = () => {
    setCheckoutUrl(null);
    setCheckoutError(null);
  };

  const handleCheckoutComplete = () => {
    setCheckoutUrl(null);
    window.location.href = "/profile";
  };

  if (loadingPlans) {
    return (
      <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
        <div className="text-earl-400 text-lg animate-pulse">Loading plans...</div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-neutral-950">
      {/* Checkout Modal */}
      {checkoutUrl && (
        <CheckoutModal
          checkoutUrl={checkoutUrl}
          onClose={handleCheckoutClose}
          onComplete={handleCheckoutComplete}
        />
      )}

      {/* Hero */}
      <div className="max-w-6xl mx-auto px-4 pt-20 pb-12 text-center">
        <h1 className="text-4xl md:text-5xl font-bold text-white mb-4">
          Go{" "}
          <span className="bg-gradient-to-r from-earl-400 to-amber-300 bg-clip-text text-transparent">
            Premium
          </span>
        </h1>
        <p className="text-gray-400 text-lg max-w-2xl mx-auto">
          Unlock the full power of Earl Knows Ball — game picks, AI analysis, write-ups,
          and Earl&apos;s chat. Your edge against the books starts here.
        </p>
      </div>

      {/* Plan Cards */}
      <div className="max-w-4xl mx-auto px-4 pb-24">
        <div className="grid md:grid-cols-2 gap-6">
          {plans.map((plan) => {
            const { amount, period } = formatPrice(plan.price_cents, plan.currency, plan.interval);
            const isAnnual = plan.interval === "year";
            const monthlyEquiv = isAnnual ? `$${(plan.price_cents / 100 / 12).toFixed(2)}/mo` : null;

            return (
              <div
                key={plan.id}
                className={`relative rounded-xl border ${
                  isAnnual
                    ? "border-earl-400 bg-neutral-900"
                    : "border-neutral-700 bg-neutral-900/70"
                } p-6 flex flex-col`}
              >
                {isAnnual && (
                  <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                    <span className="bg-earl-400 text-black text-xs font-bold px-4 py-1 rounded-full">
                      BEST VALUE
                    </span>
                  </div>
                )}

                <div className="mb-6">
                  <h2 className="text-xl font-bold text-white mb-1">{plan.name}</h2>
                  <p className="text-gray-400 text-sm">{plan.description}</p>
                </div>

                <div className="mb-6">
                  <span className="text-4xl font-bold text-white">{amount}</span>
                  <span className="text-gray-400 text-lg ml-1">{period}</span>
                  {monthlyEquiv && (
                    <div className="text-earl-400 text-sm mt-1 font-medium">
                      {monthlyEquiv} — save ~30%
                    </div>
                  )}
                </div>

                <ul className="space-y-3 mb-8 flex-1">
                  {plan.features.map((feature, i) => (
                    <li key={i} className="flex items-start gap-2 text-gray-300 text-sm">
                      <svg className="w-5 h-5 text-earl-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                      </svg>
                      {feature}
                    </li>
                  ))}
                </ul>

                <button
                  onClick={() => handleSubscribe(plan.id)}
                  disabled={checkingOut === plan.id}
                  className={`w-full py-3 rounded-lg font-semibold text-sm transition ${
                    isAnnual
                      ? "bg-earl-400 text-black hover:bg-amber-400 disabled:bg-earl-400/50"
                      : "bg-neutral-800 text-white hover:bg-neutral-700 disabled:bg-neutral-800/50"
                  }`}
                >
                  {checkingOut === plan.id ? "Opening checkout..." : "Subscribe Now"}
                </button>
              </div>
            );
          })}
        </div>

        {/* Error state */}
        {checkoutError && (
          <div className="text-center mt-4">
            <p className="text-red-400 text-sm">{checkoutError}</p>
          </div>
        )}

        {/* Already have an account? */}
        <p className="text-center text-gray-500 text-sm mt-8">
          Already a member?{" "}
          <Link href="/profile" className="text-earl-400 hover:underline">
            Manage your subscription
          </Link>
        </p>
      </div>
    </div>
  );
}
