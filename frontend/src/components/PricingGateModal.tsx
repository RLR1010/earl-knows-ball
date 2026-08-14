"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { X, Sparkles } from "lucide-react";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "@/components/LoginModal";

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

interface PricingGateModalProps {
  open: boolean;
  onClose: () => void;
}

/**
 * Modal shown in place of the game chat when the user is logged in but NOT a
 * premium member. Displays the actual pricing content (live plans from the
 * subscriptions API) so they can upgrade without leaving the page — same card
 * UI + subscribe routing as /pricing.
 */
export default function PricingGateModal({ open, onClose }: PricingGateModalProps) {
  const router = useRouter();
  const { user } = useAuth();
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loadingPlans, setLoadingPlans] = useState(true);
  const [loginOpen, setLoginOpen] = useState(false);

  useEffect(() => {
    if (!open) return;
    setLoadingPlans(true);
    fetch("/api/subscriptions/plans")
      .then((r) => r.json())
      .then((data) =>
        setPlans((data as Plan[]).sort((a, b) => a.sort_order - b.sort_order))
      )
      .catch(console.error)
      .finally(() => setLoadingPlans(false));
  }, [open]);

  // Close on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const handleSubscribe = (planId: string) => {
    const token = localStorage.getItem("earl_token");
    if (!token) {
      router.push(`/auth?redirect=/pricing&plan=${planId}`);
      return;
    }
    router.push(`/checkout?plan=${planId}`);
  };

  const activePlans = plans.filter((p) => p.is_active !== false);

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-[90] bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        aria-hidden="true"
      />
      {/* Panel */}
      <div
        className="fixed inset-0 z-[95] flex items-center justify-center p-6 pointer-events-none"
        role="dialog"
        aria-modal="true"
      >
        <div
          data-game-chat-modal
          className="pointer-events-auto w-full max-w-lg max-h-[88vh] flex flex-col bg-[#0b1220] border border-white/10 rounded-2xl shadow-2xl shadow-black/60 overflow-hidden"
        >
          {/* Header */}
          <div className="flex items-center justify-between gap-3 px-5 py-4 border-b border-white/10 bg-gradient-to-r from-earl-600/30 to-earl-500/10">
            <div className="flex items-center gap-2.5">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-earl-500 to-earl-700 text-white shadow-lg shadow-earl-600/30">
                <Sparkles className="h-4 w-4" />
              </span>
              <div>
                <div className="text-sm font-bold text-white">Earl Chat is Premium</div>
                <div className="text-[11px] text-earl-300/80 font-medium">
                  Upgrade to unlock game chat, picks & write-ups
                </div>
              </div>
            </div>
            <button
              onClick={onClose}
              aria-label="Close"
              className="shrink-0 flex h-8 w-8 items-center justify-center rounded-full text-gray-400 hover:text-white hover:bg-white/10 transition"
            >
              <X className="h-5 w-5" />
            </button>
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-5 py-5">
            <div className="text-center mb-5">
              <h2 className="text-xl font-bold text-white">
                Go{" "}
                <span className="bg-gradient-to-r from-earl-400 to-amber-300 bg-clip-text text-transparent">
                  Premium
                </span>
              </h2>
              <p className="text-gray-400 text-sm mt-1">
                Unlock the full power of Earl Knows Ball — game picks, AI analysis,
                write-ups, and Earl&apos;s chat.
              </p>
            </div>

            {loadingPlans ? (
              <div className="flex items-center justify-center py-10">
                <div className="text-earl-400 text-sm animate-pulse">Loading plans...</div>
              </div>
            ) : (
              <div className="grid gap-4">
                {activePlans.map((plan) => {
                  const isAnnual = plan.interval === "year";
                  const { amount, period } = formatPrice(
                    plan.price_cents,
                    plan.currency,
                    plan.interval
                  );
                  const monthlyEquiv = isAnnual
                    ? `$${(plan.price_cents / 100 / 12).toFixed(2)}/mo`
                    : null;

                  return (
                    <div
                      key={plan.id}
                      className={`relative rounded-xl border ${
                        isAnnual
                          ? "border-earl-400 bg-neutral-900"
                          : "border-neutral-700 bg-neutral-900/70"
                      } p-5 flex flex-col`}
                    >
                      {isAnnual && (
                        <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                          <span className="bg-earl-400 text-black text-xs font-bold px-4 py-1 rounded-full">
                            BEST VALUE
                          </span>
                        </div>
                      )}
                      <div className="mb-4">
                        <h3 className="text-lg font-bold text-white">{plan.name}</h3>
                        <p className="text-gray-400 text-sm">{plan.description}</p>
                      </div>
                      <div className="mb-4">
                        <span className="text-3xl font-bold text-white">{amount}</span>
                        <span className="text-gray-400 text-lg ml-1">{period}</span>
                        {monthlyEquiv && (
                          <div className="text-earl-400 text-sm mt-1 font-medium">
                            {monthlyEquiv} — save ~30%
                          </div>
                        )}
                      </div>
                      <ul className="space-y-2.5 mb-5 flex-1">
                        {(plan.features || []).map((feature, i) => (
                          <li key={i} className="flex items-start gap-2 text-gray-300 text-sm">
                            <svg
                              className="w-5 h-5 text-earl-400 shrink-0 mt-0.5"
                              fill="none"
                              viewBox="0 0 24 24"
                              stroke="currentColor"
                            >
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                strokeWidth={2}
                                d="M5 13l4 4L19 7"
                              />
                            </svg>
                            {feature}
                          </li>
                        ))}
                      </ul>
                      <button
                        onClick={() => handleSubscribe(plan.id)}
                        className="w-full py-3 rounded-lg font-semibold text-sm transition bg-earl-400 text-black hover:bg-amber-400"
                      >
                        Subscribe Now
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="border-t border-white/10 bg-[#0d1526] px-5 py-3 flex items-center justify-between">
            <Link
              href="/pricing"
              onClick={onClose}
              className="text-xs text-earl-400 hover:underline"
            >
              View full pricing page →
            </Link>
            <span className="text-[11px] text-gray-500">
              {user ? "Already a member?" : ""}{" "}
              <Link href="/profile" onClick={onClose}
                className="text-earl-400 hover:underline">
                Manage subscription
              </Link>
            </span>
          </div>
        </div>
      </div>

      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
    </>
  );
}
