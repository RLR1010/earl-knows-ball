"use client";

import { useEffect, useRef, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, type PaymentRecord, type TokenUsageResponse } from "@/lib/api";
import { useRouter } from "next/navigation";
import { useSeo } from "@/components/Seo";

declare global {
  interface Window {
    Stripe: (key: string) => any;
  }
}

function formatCents(cents: number, currency: string) {
  // Backend stores currency in UPPERCASE (e.g. "USD", "EUR", "GBP") via .upper()
  const c = (currency || "usd").toLowerCase();
  const symbol = c === "usd" ? "$" : c === "eur" ? "€" : c === "gbp" ? "£" : `${c.toUpperCase()} `;
  return `${symbol}${(cents / 100).toFixed(2)}`;
}

function formatDate(iso: string | null) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

function statusBadge(status: string) {
  const colors: Record<string, string> = {
    active: "bg-green-600",
    trialing: "bg-blue-600",
    past_due: "bg-yellow-600",
    canceled: "bg-red-600",
    incomplete: "bg-gray-600",
    free: "bg-gray-600",
  };
  return (
    <span
      className={`inline-block px-2.5 py-0.5 rounded-full text-xs font-semibold text-white ${
        colors[status] || "bg-gray-600"
      }`}
    >
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

function paymentBadge(status: string) {
  const colors: Record<string, string> = {
    paid: "bg-green-600",
    completed: "bg-green-600",
    pending: "bg-yellow-600",
    failed: "bg-red-600",
    refunded: "bg-blue-600",
    void: "bg-gray-600",
  };
  return (
    <span
      className={`inline-block px-2 py-0.5 rounded text-xs font-medium text-white ${
        colors[status] || "bg-gray-600"
      }`}
    >
      {status.charAt(0).toUpperCase() + status.slice(1)}
    </span>
  );
}

export default function ProfilePage() {
  useSeo({
    title: "My Profile — Earl Knows Ball",
    description: "Manage your Earl Knows Ball membership, subscription, and payment history right from your profile.",
    keywords: "profile, account, subscription, payments, Earl Knows Ball",
  });
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [payments, setPayments] = useState<PaymentRecord[]>([]);
  const [paymentsLoading, setPaymentsLoading] = useState(true);
  const [paymentsError, setPaymentsError] = useState("");
  const [subscription, setSubscription] = useState<any>(null);
  const [subLoading, setSubLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [cancelMessage, setCancelMessage] = useState<string | null>(null);
  const [tokenUsage, setTokenUsage] = useState<TokenUsageResponse | null>(null);
  const [tokenUsageLoading, setTokenUsageLoading] = useState(false);
  const [buying, setBuying] = useState(false);
  const [buyMessage, setBuyMessage] = useState<string | null>(null);
  const [topupCheckoutOpen, setTopupCheckoutOpen] = useState(false);
  const [tokenTopup, setTokenTopup] = useState<string | null>(null); // token_topup=success query
  const checkoutRef = useRef<HTMLDivElement>(null);

  // React to a successful token top-up (redirect back from Stripe)
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    if (q.get("token_topup") === "success") {
      setTokenTopup(q.get("token_topup"));
      setBuyMessage("Your token top-up was successful! Extra tokens have been added to your balance.");
      // refresh token usage so the new balance shows
      if (user?.subscription_tier?.startsWith("premium")) {
        api.tokenUsage.my().then(setTokenUsage).catch(() => {});
      }
      // clean the query param
      window.history.replaceState({}, document.title, window.location.pathname);
    }
  }, [user]);

  useEffect(() => {
    if (!authLoading && !user) {
      router.push("/login");
    }
  }, [user, authLoading, router]);

  useEffect(() => {
    if (!user) return;
    setPaymentsLoading(true);
    api.subscriptions
      .payments({ limit: 50 })
      .then(setPayments)
      .catch((err) => setPaymentsError(err?.message || "Failed to load payment history"))
      .finally(() => setPaymentsLoading(false));
  }, [user]);

  useEffect(() => {
    if (!user) return;
    setSubLoading(true);
    api.subscriptions
      .my()
      .then((data) => setSubscription(data))
      .catch(() => setSubscription({ has_active: false, subscription: null }))
      .finally(() => setSubLoading(false));
  }, [user]);

  useEffect(() => {
    if (!user || !(user.subscription_tier?.startsWith("premium"))) return;
    setTokenUsageLoading(true);
    api.tokenUsage
      .my()
      .then(setTokenUsage)
      .catch(() => setTokenUsage(null))
      .finally(() => setTokenUsageLoading(false));
  }, [user]);

  const handleCancel = async () => {
    if (!window.confirm("Are you sure you want to cancel your subscription? You will retain access until the end of the current billing period.")) return;
    setCancelling(true);
    setCancelMessage(null);
    try {
      const result = await api.subscriptions.cancel();
      setCancelMessage("Subscription canceled. Access continues until the end of the current billing period.");
      // Refresh subscription status
      const data = await api.subscriptions.my();
      setSubscription(data);
    } catch (err: any) {
      setCancelMessage(err?.message || "Failed to cancel subscription");
    } finally {
      setCancelling(false);
    }
  };

  const handleBuyTokens = async () => {
    setBuying(true);
    setBuyMessage(null);
    try {
      const res = await api.subscriptions.tokenTopup({
        success_url: `${window.location.origin}/profile?token_topup=success`,
        cancel_url: `${window.location.origin}/profile`,
        ui_mode: "embedded_page",
      });
      if (res.mock) {
        setBuyMessage(res.message || "Stripe not configured — this would charge $19.95 one-time for 2,000,000 extra tokens.");
        return;
      }
      if (!res.client_secret) {
        setBuyMessage("Unable to start the token purchase. Please try again.");
        return;
      }

      const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
      if (!key) { setBuyMessage("Stripe is not configured"); return; }

      // Load Stripe.js from CDN (same as the membership checkout)
      if (!window.Stripe) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          script.src = "https://js.stripe.com/v3/";
          script.onload = () => resolve();
          script.onerror = () => reject(new Error("Failed to load Stripe.js"));
          document.head.appendChild(script);
        });
      }

      // Embedded Checkout — mounts in the modal, user never leaves the site
      setTopupCheckoutOpen(true);
      const stripe = window.Stripe(key);
      const checkout = await stripe.initEmbeddedCheckout({
        clientSecret: res.client_secret,
      });
      checkoutRef.current && (checkoutRef.current.innerHTML = "");
      checkout.mount(checkoutRef.current);
    } catch (err: any) {
      setBuyMessage(err?.message || "Failed to start token purchase");
    } finally {
      setBuying(false);
    }
  };

  const closeTopupCheckout = () => {
    setTopupCheckoutOpen(false);
    if (checkoutRef.current) checkoutRef.current.innerHTML = "";
  };

  if (authLoading) {
    return (
      <div className="min-h-screen bg-gray-950 flex items-center justify-center">
        <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-green-500" />
      </div>
    );
  }

  if (!user) return null;

  const tier = user.subscription_tier || "free";
  const isFree = tier === "free";

  return (
    <div className="min-h-screen bg-gray-950 text-white">
      {/* Header */}
      <header className="bg-gray-900 border-b border-gray-800">
        <div className="max-w-4xl mx-auto px-4 py-6">
          <h1 className="text-2xl font-bold">Profile</h1>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8 space-y-8">
        {/* Account Info */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4">Account</h2>
          <div className="space-y-3 text-sm">
            <div className="flex justify-between">
              <span className="text-gray-400">Email</span>
              <span className="text-white">{user.email}</span>
            </div>

            <div className="flex justify-between">
              <span className="text-gray-400">Member Since</span>
              <span className="text-white">{formatDate(user.created_at)}</span>
            </div>
          </div>
        </section>

        {/* Membership */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Membership</h2>
            {statusBadge(tier)}
          </div>

          {isFree ? (
            <div>
              <p className="text-sm text-gray-400 mb-4">
                You&apos;re on the <strong className="text-white">Free</strong> tier.
                Upgrade to Premium for access to all picks, write-ups, and the Earl AI handicapper.
              </p>
              <a
                href="/pricing"
                className="inline-block bg-green-600 hover:bg-green-500 text-white font-semibold px-5 py-2.5 rounded transition-colors"
              >
                Upgrade to Premium
              </a>
            </div>
          ) : (
            <div className="space-y-3 text-sm">
              <div className="flex justify-between">
                <span className="text-gray-400">Plan</span>
                <span className="text-white font-medium capitalize">{tier}</span>
              </div>
              <p className="text-sm text-green-400">
                ✓ Premium features unlocked
              </p>

              {/* Subscription plan details */}
              {subLoading ? (
                <div className="flex items-center gap-2 text-sm text-gray-400">
                  <div className="animate-spin rounded-full h-3 w-3 border-b-2 border-green-500" />
                  Loading subscription details…
                </div>
              ) : subscription?.subscription ? (
                <div className="space-y-2 pt-2 border-t border-gray-800">
                  <div className="flex justify-between">
                    <span className="text-gray-400">Renewal Date</span>
                    <span className="text-white">
                      {formatDate(subscription.subscription.current_period_end)}
                    </span>
                  </div>
                  {subscription.subscription.cancel_at_period_end && (
                    <div className="flex justify-between">
                      <span className="text-gray-400">Status</span>
                      <span className="text-yellow-400">Cancels on {formatDate(subscription.subscription.current_period_end)}</span>
                    </div>
                  )}
                  {subscription.subscription.cancel_at_period_end && (
                    <p className="text-xs text-gray-500 mt-1">
                      Your subscription will end at the close of the current billing period. No further charges.
                    </p>
                  )}
                </div>
              ) : null}

              {/* Cancel button */}
              {subscription?.has_active && !subscription?.subscription?.cancel_at_period_end && (
                <div className="pt-2">
                  <button
                    onClick={handleCancel}
                    disabled={cancelling}
                    className="text-sm text-red-400 hover:text-red-300 underline underline-offset-2 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {cancelling ? "Cancelling…" : "Cancel Subscription"}
                  </button>
                  {cancelMessage && (
                    <p className="text-sm text-yellow-400 mt-2">{cancelMessage}</p>
                  )}
                </div>
              )}
            </div>
          )}
        </section>

        {/* Token Usage */}
        {tokenUsageLoading && (
          <section className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <h2 className="text-lg font-semibold mb-4">Chat Token Usage</h2>
            <div className="flex justify-center py-4">
              <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-green-500" />
            </div>
          </section>
        )}
        {tokenUsage && tokenUsage.token_limit != null && (
          <section className="bg-gray-900 border border-gray-800 rounded-lg p-6">
            <h2 className="text-lg font-semibold mb-4">Chat Token Usage</h2>
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-gray-400">Current Period</span>
                <span className="text-white font-medium">
                  {tokenUsage.tokens_used.toLocaleString()} /{" "}
                  {tokenUsage.token_limit.toLocaleString()} tokens
                </span>
              </div>
              <div className="w-full bg-gray-800 rounded-full h-3 overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    (tokenUsage.percent_used || 0) >= 90
                      ? "bg-red-500"
                      : (tokenUsage.percent_used || 0) >= 70
                      ? "bg-yellow-500"
                      : "bg-green-500"
                  }`}
                  style={{
                    width: `${Math.min((tokenUsage.percent_used || 0), 100)}%`,
                  }}
                />
              </div>
              {tokenUsage.percent_used !== null && (
                <p className="text-xs text-gray-500">
                  {tokenUsage.percent_used.toFixed(1)}% used
                </p>
              )}
            </div>

            {/* Extra (one-time purchased) token bank — rolls over */}
            <div className="pt-3 mt-3 border-t border-gray-800">
              <div className="flex justify-between text-sm">
                <span className="text-gray-400">Extra Tokens</span>
                <span className="text-white font-medium">
                  {tokenUsage.extra_token_balance?.toLocaleString() ?? 0} available
                </span>
              </div>
              <p className="text-xs text-gray-500 mt-1">
                One-time purchased tokens. Used only after your monthly allotment is
                exhausted, and they roll over to future months if unused.
              </p>
              <div className="mt-3">
                <button
                  onClick={handleBuyTokens}
                  disabled={buying}
                  className="w-full sm:w-auto px-4 py-2 text-sm font-semibold text-gray-950 bg-green-500 hover:bg-green-400 rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {buying ? "Starting…" : "Buy 2,000,000 Extra Tokens — $19.95"}
                </button>
                {buyMessage && (
                  <p className="text-sm text-green-400 mt-2">{buyMessage}</p>
                )}
              </div>
            </div>
          </section>
        )}

        {/* Payment History */}
        <section className="bg-gray-900 border border-gray-800 rounded-lg p-6">
          <h2 className="text-lg font-semibold mb-4">Payment History</h2>

          {paymentsLoading ? (
            <div className="flex justify-center py-6">
              <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-green-500" />
            </div>
          ) : paymentsError ? (
            <p className="text-sm text-red-400">{paymentsError}</p>
          ) : payments.length === 0 ? (
            <p className="text-sm text-gray-400">No payments yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700 text-left">
                    <th className="pb-2 pr-4">Date</th>
                    <th className="pb-2 pr-4">Description</th>
                    <th className="pb-2 pr-4">Amount</th>
                    <th className="pb-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {payments.map((p) => (
                    <tr key={p.id} className="border-b border-gray-800">
                      <td className="py-3 pr-4 text-gray-300 whitespace-nowrap">
                        {formatDate(p.created_at)}
                      </td>
                      <td className="py-3 pr-4 text-gray-300">
                        {p.description || "Payment"}
                      </td>
                      <td className="py-3 pr-4 text-white whitespace-nowrap font-medium">
                        {formatCents(p.amount_cents, p.currency)}
                      </td>
                      <td className="py-3">{paymentBadge(p.status)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </main>

      {/* Embedded Stripe Checkout modal for token top-up (user stays on-site) */}
      {topupCheckoutOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4 overflow-hidden" onClick={closeTopupCheckout}>
          <div className="relative w-full max-w-2xl bg-white rounded-2xl shadow-2xl flex flex-col max-h-[90vh]" onClick={(e) => e.stopPropagation()}>
            <button
              onClick={closeTopupCheckout}
              aria-label="Close checkout"
              className="absolute top-2 right-2 z-10 w-8 h-8 flex items-center justify-center rounded-full bg-gray-200 hover:bg-gray-300 text-gray-700 text-lg font-bold"
            >
              ×
            </button>
            {/* Stripe embedded checkout scrolls internally so the Pay button stays reachable */}
            <div ref={checkoutRef} className="min-h-[540px] flex-1 overflow-y-auto" />
          </div>
        </div>
      )}
    </div>
  );
}
