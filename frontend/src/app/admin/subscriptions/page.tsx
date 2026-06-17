"use client";

import { useEffect, useState, useCallback } from "react";

interface Subscription {
  id: string;
  user_id: string;
  user_email: string;
  user_name: string;
  plan_id: string | null;
  plan_name: string;
  status: string;
  current_period_start: string | null;
  current_period_end: string | null;
  canceled_at: string | null;
  trial_end: string | null;
  stripe_subscription_id: string | null;
  created_at: string | null;
}

const STATUS_COLORS: Record<string, string> = {
  active: "bg-green-900/30 text-green-400",
  trialing: "bg-blue-900/30 text-blue-400",
  past_due: "bg-yellow-900/30 text-yellow-400",
  canceled: "bg-red-900/30 text-red-400",
  incomplete: "bg-gray-800 text-gray-400",
  incomplete_expired: "bg-gray-800 text-gray-500",
  unpaid: "bg-orange-900/30 text-orange-400",
};

const token = () => localStorage.getItem("earl_token");

export default function AdminSubscriptions() {
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedSub, setSelectedSub] = useState<Subscription | null>(null);

  const fetchSubscriptions = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (statusFilter) params.set("status_filter", statusFilter);
      const res = await fetch(`/api/admin/subscriptions?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setSubscriptions(await res.json());
    } catch (e: any) {
      console.error("Failed to load subscriptions:", e);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => { fetchSubscriptions(); }, [fetchSubscriptions]);

  const handleUpdateStatus = async (subId: string, newStatus: string) => {
    try {
      const res = await fetch(`/api/admin/subscriptions/${subId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fetchSubscriptions();
      setSelectedSub(null);
    } catch (e: any) {
      alert(`Failed to update: ${e.message}`);
    }
  };

  const formatDate = (d: string | null) => d ? new Date(d).toLocaleDateString() : "—";
  const formatPrice = (cents: number) => `$${(cents / 100).toFixed(2)}`;

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Subscriptions</h1>
        <p className="text-gray-400 text-sm mt-1">Track all user subscriptions</p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="bg-white/5 border border-white/10 rounded-lg px-4 py-2 text-sm text-white focus:outline-none focus:border-earl-600"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="trialing">Trialing</option>
          <option value="past_due">Past Due</option>
          <option value="canceled">Canceled</option>
          <option value="incomplete">Incomplete</option>
        </select>
        <button onClick={fetchSubscriptions} className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition">
          Refresh
        </button>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-gray-400">Loading subscriptions...</div>
      ) : subscriptions.length === 0 ? (
        <div className="text-gray-500">No subscriptions found.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-white/10 text-left text-gray-400 text-xs uppercase tracking-wider">
                <th className="pb-3 pr-4 font-semibold">User</th>
                <th className="pb-3 pr-4 font-semibold">Plan</th>
                <th className="pb-3 pr-4 font-semibold">Status</th>
                <th className="pb-3 pr-4 font-semibold">Start</th>
                <th className="pb-3 pr-4 font-semibold">End</th>
                <th className="pb-3 pr-4 font-semibold">Stripe</th>
                <th className="pb-3 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {subscriptions.map((sub) => (
                <tr key={sub.id} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="py-3 pr-4">
                    <div className="text-white">{sub.user_email}</div>
                    {sub.user_name && <div className="text-xs text-gray-500">{sub.user_name}</div>}
                  </td>
                  <td className="py-3 pr-4">
                    <span className="text-gray-300">{sub.plan_name || "—"}</span>
                  </td>
                  <td className="py-3 pr-4">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[sub.status] || "bg-gray-800 text-gray-400"}`}>
                      {sub.status}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-xs text-gray-400">{formatDate(sub.current_period_start)}</td>
                  <td className="py-3 pr-4 text-xs text-gray-400">{formatDate(sub.current_period_end)}</td>
                  <td className="py-3 pr-4">
                    {sub.stripe_subscription_id ? (
                      <span className="text-xs text-gray-500 font-mono truncate max-w-[100px] inline-block">
                        {sub.stripe_subscription_id.slice(0, 16)}...
                      </span>
                    ) : (
                      <span className="text-xs text-gray-600">—</span>
                    )}
                  </td>
                  <td className="py-3">
                    <button
                      onClick={() => setSelectedSub(sub)}
                      className="text-xs text-earl-400 hover:text-earl-300 transition"
                    >
                      Details
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Detail Modal */}
      {selectedSub && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setSelectedSub(null)}>
          <div className="bg-[#1a1a2e] border border-white/10 rounded-xl p-6 w-[480px] max-w-full" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-white mb-4">Subscription Details</h2>

            <div className="space-y-2 text-sm">
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">User</span>
                <span className="text-white">{selectedSub.user_email}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Plan</span>
                <span className="text-white">{selectedSub.plan_name || "—"}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Status</span>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[selectedSub.status] || "bg-gray-800 text-gray-400"}`}>
                  {selectedSub.status}
                </span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Period</span>
                <span className="text-gray-300">{formatDate(selectedSub.current_period_start)} → {formatDate(selectedSub.current_period_end)}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Created</span>
                <span className="text-gray-300">{formatDate(selectedSub.created_at)}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Canceled</span>
                <span className="text-gray-300">{formatDate(selectedSub.canceled_at)}</span>
              </div>
              <div className="flex justify-between py-1 border-b border-white/5">
                <span className="text-gray-500">Stripe Sub</span>
                <span className="text-gray-300 font-mono text-xs">{selectedSub.stripe_subscription_id || "—"}</span>
              </div>
            </div>

            {/* Quick status change */}
            <div className="mt-6">
              <label className="block text-xs text-gray-500 mb-2">Change Status</label>
              <div className="flex flex-wrap gap-2">
                {["active", "canceled", "past_due", "trialing"].map((s) => (
                  <button
                    key={s}
                    onClick={() => handleUpdateStatus(selectedSub.id, s)}
                    disabled={s === selectedSub.status}
                    className={`px-3 py-1.5 rounded-lg text-xs font-medium transition ${
                      s === selectedSub.status
                        ? "bg-gray-800 text-gray-600 cursor-not-allowed"
                        : "bg-white/5 text-gray-300 hover:bg-white/10 hover:text-white"
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex justify-end mt-4">
              <button onClick={() => setSelectedSub(null)} className="px-4 py-2 text-sm text-gray-400 hover:text-white transition">
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
