"use client";

import { useEffect, useState, useCallback } from "react";

interface PaymentRecord {
  id: string;
  user_id: string;
  user_email: string;
  user_name: string;
  subscription_id: string | null;
  amount_cents: number;
  currency: string;
  status: string;
  description: string | null;
  stripe_invoice_id: string | null;
  created_at: string | null;
}

interface PaymentListResponse {
  payments: PaymentRecord[];
  total: number;
  total_cents: number;
  page: number;
  page_size: number;
}

const token = () => localStorage.getItem("earl_token");

function formatCurrency(cents: number, currency: string = "usd"): string {
  const symbol = currency === "usd" ? "$" : currency.toUpperCase() + " ";
  return `${symbol}${(cents / 100).toFixed(2)}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function todayString(): string {
  const d = new Date();
  return d.toISOString().slice(0, 10); // YYYY-MM-DD
}

const STATUS_COLORS: Record<string, string> = {
  succeeded: "text-green-400",
  pending: "text-yellow-400",
  failed: "text-red-400",
  refunded: "text-purple-400",
};

export default function AdminPayments() {
  const [data, setData] = useState<PaymentListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  // Filters
  const [dateFrom, setDateFrom] = useState(todayString());
  const [dateTo, setDateTo] = useState(todayString());
  const [statusFilter, setStatusFilter] = useState("");
  const [page, setPage] = useState(1);

  const fetchPayments = useCallback(async (p: number) => {
    setLoading(true);
    setError("");
    try {
      const params = new URLSearchParams();
      if (dateFrom) params.set("date_from", dateFrom + "T00:00:00");
      if (dateTo) params.set("date_to", dateTo + "T23:59:59");
      if (statusFilter) params.set("status_filter", statusFilter);
      params.set("page", String(p));
      params.set("page_size", "50");

      const res = await fetch(`/api/admin/payments?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
    } catch (e: any) {
      console.error("Failed to load payments:", e);
      setError(e.message || "Failed to load payments");
    } finally {
      setLoading(false);
    }
  }, [dateFrom, dateTo, statusFilter]);

  useEffect(() => {
    fetchPayments(page);
  }, [page, fetchPayments]);

  const totalPages = data ? Math.ceil(data.total / data.page_size) : 0;

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-white">Payments</h1>
      </div>

      {/* Summary bar */}
      {data && !loading && (
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div className="bg-black/30 border border-white/10 rounded-lg p-4">
            <div className="text-gray-400 text-xs uppercase tracking-wider">Total Payments</div>
            <div className="text-white text-2xl font-bold mt-1">{data.total}</div>
          </div>
          <div className="bg-black/30 border border-white/10 rounded-lg p-4">
            <div className="text-gray-400 text-xs uppercase tracking-wider">Total Revenue</div>
            <div className="text-green-400 text-2xl font-bold mt-1">
              {formatCurrency(data.total_cents)}
            </div>
          </div>
          <div className="bg-black/30 border border-white/10 rounded-lg p-4">
            <div className="text-gray-400 text-xs uppercase tracking-wider">Status</div>
            <div className="text-white text-xl mt-1">Page {data.page} / {totalPages || 1}</div>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="bg-black/30 border border-white/10 rounded-lg p-4 mb-6">
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">From</label>
            <input
              type="date"
              value={dateFrom}
              onChange={(e) => { setDateFrom(e.target.value); setPage(1); }}
              className="bg-black/50 border border-white/20 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-earl-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">To</label>
            <input
              type="date"
              value={dateTo}
              onChange={(e) => { setDateTo(e.target.value); setPage(1); }}
              className="bg-black/50 border border-white/20 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-earl-500"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-400 mb-1 uppercase tracking-wider">Status</label>
            <select
              value={statusFilter}
              onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
              className="bg-black/50 border border-white/20 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-earl-500"
            >
              <option value="">All</option>
              <option value="succeeded">Succeeded</option>
              <option value="pending">Pending</option>
              <option value="failed">Failed</option>
              <option value="refunded">Refunded</option>
            </select>
          </div>
          <button
            onClick={() => fetchPayments(page)}
            className="px-5 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
          >
            Search
          </button>
          <button
            onClick={() => {
              setDateFrom(todayString());
              setDateTo(todayString());
              setStatusFilter("");
              setPage(1);
            }}
            className="px-4 py-2 bg-white/10 text-gray-300 rounded-lg text-sm hover:bg-white/20 transition"
          >
            Reset
          </button>
        </div>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-gray-400">Loading payments...</div>
      ) : error ? (
        <div className="text-red-400">{error}</div>
      ) : !data || data.payments.length === 0 ? (
        <div className="text-gray-500">No payments found in this date range.</div>
      ) : (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/10 text-gray-400 uppercase text-xs tracking-wider">
                  <th className="text-left py-3 px-3">Date</th>
                  <th className="text-left py-3 px-3">User</th>
                  <th className="text-left py-3 px-3">Email</th>
                  <th className="text-right py-3 px-3">Amount</th>
                  <th className="text-center py-3 px-3">Status</th>
                  <th className="text-left py-3 px-3">Description</th>
                  <th className="text-left py-3 px-3">Invoice #</th>
                </tr>
              </thead>
              <tbody>
                {data.payments.map((pmt) => (
                  <tr key={pmt.id} className="border-b border-white/5 hover:bg-white/5 transition">
                    <td className="py-3 px-3 text-gray-300 whitespace-nowrap">
                      {formatDate(pmt.created_at)}
                    </td>
                    <td className="py-3 px-3 text-white font-medium">
                      {pmt.user_name || pmt.user_email?.split("@")[0] || pmt.user_id?.slice(0, 8)}
                    </td>
                    <td className="py-3 px-3 text-gray-400">{pmt.user_email || "—"}</td>
                    <td className="py-3 px-3 text-right text-white font-mono font-semibold">
                      {formatCurrency(pmt.amount_cents, pmt.currency)}
                    </td>
                    <td className="py-3 px-3 text-center">
                      <span className={`text-xs font-medium uppercase ${STATUS_COLORS[pmt.status] || "text-gray-400"}`}>
                        {pmt.status}
                      </span>
                    </td>
                    <td className="py-3 px-3 text-gray-400 max-w-[200px] truncate">
                      {pmt.description || "—"}
                    </td>
                    <td className="py-3 px-3 text-gray-500 text-xs">
                      {pmt.stripe_invoice_id
                        ? pmt.stripe_invoice_id.slice(0, 20) + (pmt.stripe_invoice_id.length > 20 ? "…" : "")
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-6 pt-4 border-t border-white/10">
              <div className="text-sm text-gray-400">
                Showing {(page - 1) * data.page_size + 1}–{Math.min(page * data.page_size, data.total)} of {data.total}
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="px-3 py-1.5 rounded text-sm bg-white/10 text-gray-300 hover:bg-white/20 disabled:opacity-30 disabled:cursor-not-allowed transition"
                >
                  ← Prev
                </button>
                <span className="text-sm text-gray-400 px-2">
                  Page {page} of {totalPages}
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="px-3 py-1.5 rounded text-sm bg-white/10 text-gray-300 hover:bg-white/20 disabled:opacity-30 disabled:cursor-not-allowed transition"
                >
                  Next →
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
