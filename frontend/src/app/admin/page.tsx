"use client";

import { useEffect, useState } from "react";

interface DashboardStats {
  total_users: number;
  active_users: number;
  premium_users: number;
  monthly_revenue_cents: number;
  total_revenue_cents: number;
  users_today: number;
  users_this_week: number;
  subscriptions_active: number;
  subscriptions_canceled: number;
  failed_payments: number;
  plans_count: number;
}

const token = () => localStorage.getItem("earl_token");

function StatCard({ label, value, subtitle, color }: { label: string; value: string | number; subtitle?: string; color?: string }) {
  return (
    <div className="bg-white/[0.03] border border-white/10 rounded-xl p-6 hover:bg-white/[0.05] transition">
      <div className="text-xs text-gray-500 uppercase tracking-wider font-semibold mb-1">{label}</div>
      <div className={`text-3xl font-bold ${color || "text-white"}`}>
        {typeof value === "number" ? value.toLocaleString() : value}
      </div>
      {subtitle && <div className="text-xs text-gray-500 mt-1">{subtitle}</div>}
    </div>
  );
}

export default function AdminDashboard() {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchStats = async () => {
      try {
        const res = await fetch("/api/admin/stats", {
          headers: { Authorization: `Bearer ${token()}` },
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        setStats(data);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    };
    fetchStats();
  }, []);

  if (loading) return <div className="text-gray-400">Loading dashboard...</div>;
  if (error) return <div className="text-red-400">Error: {error}</div>;
  if (!stats) return null;

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Dashboard</h1>
        <p className="text-gray-400 text-sm mt-1">Overview of your premium subscription business</p>
      </div>

      {/* Main metrics */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        <StatCard label="Total Users" value={stats.total_users} subtitle={`${stats.users_today} today · ${stats.users_this_week} this week`} color="text-blue-400" />
        <StatCard label="Active Users" value={stats.active_users} subtitle={`${((stats.active_users / stats.total_users) * 100).toFixed(1)}% of total`} color="text-green-400" />
        <StatCard label="Premium Users" value={stats.premium_users} subtitle={`${((stats.premium_users / stats.total_users) * 100).toFixed(1)}% conversion`} color="text-earl-400" />
        <StatCard label="Active Subscriptions" value={stats.subscriptions_active} subtitle={`${stats.subscriptions_canceled} canceled`} color="text-purple-400" />
      </div>

      {/* Revenue & Plans */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mb-8">
        <StatCard
          label="Monthly Revenue"
          value={`$${(stats.monthly_revenue_cents / 100).toFixed(2)}`}
          subtitle="Current month"
          color="text-yellow-400"
        />
        <StatCard
          label="Total Revenue"
          value={`$${(stats.total_revenue_cents / 100).toFixed(2)}`}
          subtitle="All time"
          color="text-yellow-400"
        />
        <StatCard label="Plans" value={stats.plans_count} subtitle="Subscription tiers configured" />
      </div>

      {/* Alerts */}
      {stats.failed_payments > 0 && (
        <div className="bg-red-900/20 border border-red-800/30 rounded-xl p-4 text-red-300 text-sm">
          ⚠️ {stats.failed_payments} failed payment{stats.failed_payments !== 1 ? "s" : ""}. Check subscriptions page.
        </div>
      )}

      {/* Quick links */}
      <div className="mt-8">
        <h2 className="text-lg font-semibold text-white mb-4">Quick Actions</h2>
        <div className="flex flex-wrap gap-3">
          <a href="/admin/users" className="px-5 py-2.5 bg-white/5 border border-white/10 rounded-lg text-sm text-gray-300 hover:bg-white/10 hover:text-white transition">
            Manage Users
          </a>
          <a href="/admin/plans" className="px-5 py-2.5 bg-white/5 border border-white/10 rounded-lg text-sm text-gray-300 hover:bg-white/10 hover:text-white transition">
            Subscription Plans
          </a>
          <a href="/admin/subscriptions" className="px-5 py-2.5 bg-white/5 border border-white/10 rounded-lg text-sm text-gray-300 hover:bg-white/10 hover:text-white transition">
            View Subscriptions
          </a>
        </div>
      </div>
    </div>
  );
}
