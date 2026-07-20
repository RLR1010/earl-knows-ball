"use client";

import { useEffect, useState, useCallback } from "react";

interface User {
  id: string;
  email: string;
  display_name: string | null;
  subscription_tier: string;
  is_active: boolean;
  is_admin: boolean;
  email_verified: boolean;
  created_at: string | null;
  last_login_at: string | null;
  monthly_token_limit: number | null;
  tokens_used: number;
}

const token = () => localStorage.getItem("earl_token");

export default function AdminUsers() {
  const [users, setUsers] = useState<User[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [tierFilter, setTierFilter] = useState("");
  const [editingUser, setEditingUser] = useState<User | null>(null);

  const fetchUsers = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (tierFilter) params.set("tier", tierFilter);
      const res = await fetch(`/api/admin/users?${params.toString()}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setUsers(await res.json());
    } catch (e: any) {
      console.error("Failed to load users:", e);
    } finally {
      setLoading(false);
    }
  }, [search, tierFilter]);

  useEffect(() => { fetchUsers(); }, [fetchUsers]);

  const handleUpdate = async (userId: string, data: any) => {
    try {
      const res = await fetch(`/api/admin/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${token()}` },
        body: JSON.stringify(data),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setEditingUser(null);
      fetchUsers();
    } catch (e: any) {
      alert(`Failed to update user: ${e.message}`);
    }
  };

  const handleDelete = async (userId: string, email: string) => {
    if (!confirm(`Delete user ${email}? This cannot be undone.`)) return;
    try {
      const res = await fetch(`/api/admin/users/${userId}`, {
        method: "DELETE",
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      fetchUsers();
    } catch (e: any) {
      alert(`Failed to delete user: ${e.message}`);
    }
  };

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Users</h1>
        <p className="text-gray-400 text-sm mt-1">Manage user accounts and permissions</p>
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 mb-6">
        <input
          type="text"
          placeholder="Search by email or name..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="bg-white/5 border border-white/10 rounded-lg px-4 py-2 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-600 w-64"
        />
        <select
          value={tierFilter}
          onChange={(e) => setTierFilter(e.target.value)}
          className="bg-white/5 border border-white/10 rounded-lg px-4 py-2 text-sm text-white focus:outline-none focus:border-earl-600"
        >
          <option value="">All Tiers</option>
          <option value="free">Free</option>
          <option value="premium">Premium</option>
          <option value="premium_yearly">Premium Yearly</option>
        </select>
        <button onClick={fetchUsers} className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition">
          Refresh
        </button>
      </div>

      {/* Table */}
      {loading ? (
        <div className="text-gray-400">Loading users...</div>
      ) : users.length === 0 ? (
        <div className="text-gray-500">No users found.</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-white/10 text-left text-gray-400 text-xs uppercase tracking-wider">
                <th className="pb-3 pr-4 font-semibold">Email</th>
                <th className="pb-3 pr-4 font-semibold">Name</th>
                <th className="pb-3 pr-4 font-semibold">Tier</th>
                <th className="pb-3 pr-4 font-semibold">Status</th>
                <th className="pb-3 pr-4 font-semibold">Admin</th>
                <th className="pb-3 pr-4 font-semibold">Tokens</th>
                <th className="pb-3 pr-4 font-semibold">Joined</th>
                <th className="pb-3 font-semibold">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id} className="border-b border-white/5 hover:bg-white/[0.02]">
                  <td className="py-3 pr-4 text-white">{user.email}</td>
                  <td className="py-3 pr-4 text-gray-300">{user.display_name || "—"}</td>
                  <td className="py-3 pr-4">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                      user.subscription_tier === "free" ? "bg-gray-800 text-gray-400" :
                      user.subscription_tier === "premium" ? "bg-earl-600/20 text-earl-400" :
                      "bg-purple-900/30 text-purple-400"
                    }`}>
                      {user.subscription_tier}
                    </span>
                  </td>
                  <td className="py-3 pr-4">
                    <span className={`inline-block w-2 h-2 rounded-full ${user.is_active ? "bg-green-500" : "bg-red-500"}`} />
                  </td>
                  <td className="py-3 pr-4">
                    {user.is_admin ? <span className="text-earl-400">✓</span> : "—"}
                  </td>
                  <td className="py-3 pr-4 text-xs">
                    <span className={user.monthly_token_limit && user.tokens_used > user.monthly_token_limit ? "text-red-400" : "text-gray-400"}>
                      {user.tokens_used}{user.monthly_token_limit ? ` / ${user.monthly_token_limit}` : ""}
                    </span>
                  </td>
                  <td className="py-3 pr-4 text-gray-400 text-xs">
                    {user.created_at ? new Date(user.created_at).toLocaleDateString() : "—"}
                  </td>
                  <td className="py-3">
                    <div className="flex gap-2">
                      <button
                        onClick={() => setEditingUser(user)}
                        className="text-xs text-earl-400 hover:text-earl-300 transition"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleDelete(user.id, user.email)}
                        className="text-xs text-red-400 hover:text-red-300 transition"
                      >
                        Delete
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Edit Modal */}
      {editingUser && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50" onClick={() => setEditingUser(null)}>
          <div className="bg-[#1a1a2e] border border-white/10 rounded-xl p-6 w-96 max-w-full" onClick={(e) => e.stopPropagation()}>
            <h2 className="text-lg font-semibold text-white mb-4">Edit User</h2>
            <div className="text-sm text-gray-400 mb-4">{editingUser.email}</div>

            <label className="block text-xs text-gray-500 mb-1">Display Name</label>
            <input
              type="text"
              defaultValue={editingUser.display_name || ""}
              id="edit-name"
              className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600"
            />

            <label className="block text-xs text-gray-500 mb-1">Subscription Tier</label>
            <select
              defaultValue={editingUser.subscription_tier}
              id="edit-tier"
              className="w-full bg-white/5 border border-white/10 rounded-lg px-3 py-2 text-sm text-white mb-3 focus:outline-none focus:border-earl-600"
            >
              <option value="free">Free</option>
              <option value="premium">Premium</option>
              <option value="premium_yearly">Premium Yearly</option>
            </select>

            <label className="block text-xs text-gray-500 mb-1 mt-3">Monthly Token Limit</label>
            <input
              type="number"
              defaultValue={editingUser.monthly_token_limit ?? ""}
              id="edit-token-limit"
              min="0"
              className="w-full px-3 py-2 rounded bg-gray-900 border border-white/10 text-white text-sm mb-4 focus:outline-none focus:border-earl-400 placeholder-gray-600"
              placeholder="Unlimited"
            />

            <div className="flex items-center gap-4 mb-4">
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" defaultChecked={editingUser.is_active} id="edit-active" className="rounded" />
                Active
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" defaultChecked={editingUser.is_admin} id="edit-admin" className="rounded" />
                Admin
              </label>
              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" defaultChecked={editingUser.email_verified} id="edit-verified" className="rounded" />
                Verified
              </label>
            </div>

            <div className="flex gap-3 justify-end">
              <button onClick={() => setEditingUser(null)} className="px-4 py-2 text-sm text-gray-400 hover:text-white transition">
                Cancel
              </button>
              <button
                onClick={() => {
                  const name = (document.getElementById("edit-name") as HTMLInputElement).value;
                  const tier = (document.getElementById("edit-tier") as HTMLSelectElement).value;
                  const active = (document.getElementById("edit-active") as HTMLInputElement).checked;
                  const admin = (document.getElementById("edit-admin") as HTMLInputElement).checked;
                  const verified = (document.getElementById("edit-verified") as HTMLInputElement).checked;
                  const tokenLimitEl = document.getElementById("edit-token-limit") as HTMLInputElement;
                  const tokenLimit = tokenLimitEl.value ? parseInt(tokenLimitEl.value) : null;
                  handleUpdate(editingUser.id, {
                    display_name: name || null,
                    subscription_tier: tier,
                    is_active: active,
                    is_admin: admin,
                    email_verified: verified,
                    monthly_token_limit: tokenLimit,
                  });
                }}
                className="px-4 py-2 bg-earl-600 text-white rounded-lg text-sm hover:bg-earl-500 transition"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
