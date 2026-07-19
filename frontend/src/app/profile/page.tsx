"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { api, type PaymentRecord } from "@/lib/api";
import { useRouter } from "next/navigation";

function formatCents(cents: number, currency: string) {
  const symbol = currency === "usd" ? "$" : currency === "eur" ? "€" : "£";
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
  const { user, loading: authLoading } = useAuth();
  const router = useRouter();
  const [payments, setPayments] = useState<PaymentRecord[]>([]);
  const [paymentsLoading, setPaymentsLoading] = useState(true);
  const [paymentsError, setPaymentsError] = useState("");
  const [subscription, setSubscription] = useState<any>(null);
  const [subLoading, setSubLoading] = useState(true);
  const [cancelling, setCancelling] = useState(false);
  const [cancelMessage, setCancelMessage] = useState<string | null>(null);

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
    </div>
  );
}
