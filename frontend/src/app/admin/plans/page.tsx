"use client";

import { useEffect, useState, useCallback } from "react";

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
  stripe_price_id: string | null;
  stripe_product_id: string | null;
  monthly_token_limit: number | null;
  created_at: string | null;
}

const emptyPlan = {
  name: "", slug: "", description: "", price_cents: 999, currency: "usd",
  interval: "month", trial_days: 0, features: [], is_active: true, sort_order: 0,
  stripe_price_id: "", stripe_product_id: "", monthly_token_limit: null,
};

const token = () => localStorage.getItem("earl_token");

export default function AdminPlans() {
  const [plans, setPlans] = useState<Plan[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState<Plan | "new" | null>(null);

  const fetchPlans = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/admin/plans", {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setPlans(await res.json());
    } catch (e: any) {
      console.error("Failed to load plans:", e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchPlans(); }, [fetchPlans]);

  const handleSave = async (planData: any) => {
    try {
      const isNew = editing === "new";
      const url = isNew ? "/api/admin/plans" : `/api/admin/plans/${(editing as Plan).id}`;
      const res = await fetch(url, {
        method: isNew ? "POST" : "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
        body: JSON.stringify(planData),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setEditing(null);
      fetchPlans();
    } catch (e: any) {
      alert(`Failed to save plan: ${e.message}`);
    }
  };

  const handleDelete = async (plan: Plan) => {
    if (!confirm(`Delete plan "${plan.name}"? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/plans/${plan.id}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fetchPlans();
    } catch (e: any) {
      alert(`Failed to delete plan: ${e.message}`);
    }
  };

  const formatPrice = (cents: number, currency: string, interval: string) => {
    const amount = (cents / 100).toFixed(2);
    const symbol = currency === "usd" ? "$" : currency.toUpperCase() + " ";
    return `${symbol}${amount}/${interval}`;
  };

  if (loading) return <div className="text-gray-400">Loading plans...</div>;

  return (
    <div>
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold text-white">Subscription Plans</h1>
          <p className="text-gray-400 text-sm mt-1">Manage pricing tiers and features</p>
        </div>
        <button
          onClick={() => setEditing("new")}
          className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
        >
          + New Plan
        </button>
      </div>

      {plans.length === 0 ? (
        <div className="text-gray-500">No plans configured yet. Create your first subscription plan.</div>
      ) : (
        <div className="grid gap-4">
          {plans.map((plan) => (
            <div key={plan.id} className="bg-white/[0.03] border border-white/10 rounded-xl p-5 hover:bg-white/[0.05] transition">
              <div className="flex items-start justify-between">
                <div>
                  <div className="flex items-center gap-3">
                    <h3 className="text-lg font-semibold text-white">{plan.name}</h3>
                    {!plan.is_active && (
                      <span className="px-2 py-0.5 bg-yellow-900/30 text-yellow-400 rounded-full text-xs font-medium">Inactive</span>
                    )}
                    {plan.stripe_price_id && (
                      <span className="px-2 py-0.5 bg-blue-900/30 text-blue-400 rounded-full text-xs font-medium">Stripe</span>
                    )}
                  </div>
                  <div className="text-sm text-gray-400 mt-1">
                    {formatPrice(plan.price_cents, plan.currency, plan.interval)}
                    {plan.trial_days > 0 && ` · ${plan.trial_days}-day trial`}
                    {plan.monthly_token_limit != null && (
                      <span className="ml-2 text-gray-400">· {plan.monthly_token_limit.toLocaleString()} tokens/mo</span>
                    )}
                  </div>
                  {plan.description && (
                    <div className="text-xs text-gray-500 mt-1">{plan.description}</div>
                  )}
                  {plan.features.length > 0 && (
                    <div className="flex flex-wrap gap-1.5 mt-2">
                      {plan.features.map((f, i) => (
                        <span key={i} className="px-2 py-0.5 bg-white/5 rounded text-xs text-gray-400">{f}</span>
                      ))}
                    </div>
                  )}
                </div>
                <div className="flex gap-2">
                  <button onClick={() => setEditing(plan)} className="text-xs text-earl-400 hover:text-earl-300 transition px-2 py-1">
                    Edit
                  </button>
                  <button onClick={() => handleDelete(plan)} className="text-xs text-red-400 hover:text-red-300 transition px-2 py-1">
                    Delete
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Edit/Create Modal */}
      {editing && <PlanFormModal plan={editing === "new" ? null : editing} onSave={handleSave} onClose={() => setEditing(null)} />}
    </div>
  );
}

function PlanFormModal({ plan, onSave, onClose }: { plan: Plan | null; onSave: (data: any) => void; onClose: () => void }) {
  const isNew = !plan;

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-[#1a1a2e] border border-white/10 rounded-xl p-6 w-[500px] max-w-full max-h-[90vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold text-white mb-4">{isNew ? "Create Plan" : "Edit Plan"}</h2>

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Name *</label>
            <input id="f-name" defaultValue={plan?.name || ""} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Slug *</label>
            <input id="f-slug" defaultValue={plan?.slug || ""} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
        </div>

        <label className="block text-xs text-gray-500 mb-1">Description</label>
        <textarea id="f-desc" defaultValue={plan?.description || ""} rows={2} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />

        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Price (cents)</label>
            <input id="f-price" type="number" defaultValue={plan?.price_cents || 999} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Interval</label>
            <select id="f-interval" defaultValue={plan?.interval || "month"} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600">
              <option value="month">Monthly</option>
              <option value="year">Yearly</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Trial Days</label>
            <input id="f-trial" type="number" defaultValue={plan?.trial_days || 0} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
        </div>

        <label className="block text-xs text-gray-500 mb-1">Features (one per line)</label>
        <textarea
          id="f-features"
          defaultValue={(plan?.features || []).join("\n")}
          rows={3}
          className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600 font-mono"
          placeholder={"AI Chat Access\nAdvanced Stats\nNo Ads"}
        />

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className="block text-xs text-gray-500 mb-1">Stripe Price ID</label>
            <input id="f-spid" defaultValue={plan?.stripe_price_id || ""} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
          <div>
            <label className="block text-xs text-gray-500 mb-1">Sort Order</label>
            <input id="f-order" type="number" defaultValue={plan?.sort_order || 0} className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
          </div>
        </div>

        <div>
          <label className="block text-xs text-gray-500 mb-1">Monthly Token Limit</label>
          <input id="f-tokens" type="number" min="0" defaultValue={plan?.monthly_token_limit ?? ""} placeholder="Unlimited" className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600" />
        </div>

        <div className="flex items-center gap-4 mb-4">
          <label className="flex items-center gap-2 text-sm text-gray-300">
            <input type="checkbox" defaultChecked={plan?.is_active ?? true} id="f-active" className="rounded" />
            Active
          </label>
        </div>

        <div className="flex gap-3 justify-end">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-400 hover:text-white transition">Cancel</button>
          <button
            onClick={() => {
              const get = (id: string) => (document.getElementById(id) as HTMLInputElement)?.value || "";
              const getCheck = (id: string) => (document.getElementById(id) as HTMLInputElement)?.checked || false;
              const featuresRaw = (document.getElementById("f-features") as HTMLTextAreaElement)?.value || "";
              const features = featuresRaw.split("\n").map(s => s.trim()).filter(Boolean);
              onSave({
                name: get("f-name"),
                slug: get("f-slug"),
                description: get("f-desc") || null,
                price_cents: parseInt(get("f-price")) || 0,
                currency: "usd",
                interval: get("f-interval") || "month",
                trial_days: parseInt(get("f-trial")) || 0,
                features,
                is_active: getCheck("f-active"),
                sort_order: parseInt(get("f-order")) || 0,
                monthly_token_limit: get("f-tokens") ? parseInt(get("f-tokens")) : null,
                stripe_price_id: get("f-spid") || null,
                stripe_product_id: plan?.stripe_product_id || null,
              });
            }}
            className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
          >
            {isNew ? "Create" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
