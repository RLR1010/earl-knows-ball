"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

const SPORTS = ["nfl", "nba", "mlb"] as const;
type Sport = (typeof SPORTS)[number];

interface Feature {
  name: string;
  description: string | null;
  display_name: string | null;
  is_trainable: boolean;
  pick_card: boolean;
  pick_card_section: string | null;
  sort_order: number | null;
  current_ou: string | number | null;
  current_ats: string | number | null;
  created_at: string | null;
}

const PICK_CARD_SECTIONS = [
  "home_stats",
  "away_stats",
  "game_context",
  "betting_lines",
  "other",
] as const;
type PickCardSection = (typeof PICK_CARD_SECTIONS)[number];

const token = () => localStorage.getItem("earl_token");
const sportLabel = { nfl: "NFL", nba: "NBA", mlb: "MLB" };

export default function AdminFeatures() {
  useSeo({ title: "Features — Admin — Earl Knows Ball" });
  const [sport, setSport] = useState<Sport>("nfl");
  const [features, setFeatures] = useState<Feature[]>([]);
  const [loading, setLoading] = useState(true);
  // Track per-row edits keyed by feature name.
  const [edits, setEdits] = useState<Record<string, Partial<Feature>>>({});
  // Track which rows are currently saving (pending PATCH).
  const [saving, setSaving] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState("");

  const fetchFeatures = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch(`/api/admin/features/${sport}`, {
        headers: { Authorization: `Bearer ${token()}` },
      });
      if (res.ok) {
        const data = await res.json();
        setFeatures(data.features ?? []);
        setEdits({});
      } else {
        alert(`Failed to load features (HTTP ${res.status})`);
      }
    } catch (e: any) {
      alert(`Failed to load features: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }, [sport]);

  useEffect(() => { fetchFeatures(); }, [fetchFeatures]);

  const setEdit = (name: string, patch: Partial<Feature>) => {
    setEdits((prev) => ({
      ...prev,
      [name]: { ...(prev[name] ?? {}), ...patch },
    }));
  };

  // A row is dirty if any of its editable fields differ from the loaded value.
  const isDirty = (f: Feature) => {
    const e = edits[f.name];
    if (!e) return false;
    if (e.display_name !== undefined && e.display_name !== (f.display_name ?? "")) return true;
    if (e.description !== undefined && e.description !== (f.description ?? "")) return true;
    if (e.is_trainable !== undefined && e.is_trainable !== f.is_trainable) return true;
    if (e.pick_card !== undefined && e.pick_card !== f.pick_card) return true;
    if (e.pick_card_section !== undefined && e.pick_card_section !== (f.pick_card_section ?? "")) return true;
    return false;
  };

  const saveRow = async (f: Feature) => {
    const e = edits[f.name];
    if (!e) return;
    setSaving((prev) => ({ ...prev, [f.name]: true }));
    try {
      const res = await fetch(`/api/admin/features/${sport}/${encodeURIComponent(f.name)}`, {
        method: "PATCH",
        headers: {
          Authorization: `Bearer ${token()}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify(e),
      });
      if (!res.ok) {
        const err = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status} ${err}`);
      }
      setEdits((prev) => {
        const next = { ...prev };
        delete next[f.name];
        return next;
      });
      setFeatures((prev) =>
        prev.map((r) => (r.name === f.name ? { ...r, ...e } : r))
      );
    } catch (e2: any) {
      alert(`Failed to save "${f.name}": ${e2.message}`);
    } finally {
      setSaving((prev) => ({ ...prev, [f.name]: false }));
    }
  };

  // Compute a stable working order for the admin table from sort_order, falling
  // back to insertion order (features list) for rows the backfill hasn't ranked yet.
  const orderedRows = [...features].sort((a, b) => {
    const sa = a.sort_order ?? Number.MAX_SAFE_INTEGER;
    const sb = b.sort_order ?? Number.MAX_SAFE_INTEGER;
    if (sa !== sb) return sa - sb;
    // stable tie-break by name so the order doesn't jitter
    return (a.name ?? "").localeCompare(b.name ?? "");
  });

  // Swap the display order (sort_order) of two rows and persist both.
  const reorder = async (a: Feature, b: Feature) => {
    const aOrder = a.sort_order ?? 0;
    const bOrder = b.sort_order ?? 0;
    const patchA = { sort_order: bOrder };
    const patchB = { sort_order: aOrder };
    try {
      const body = { sport, a: a.name, aPatch: patchA, b: b.name, bPatch: patchB };
      const res = await fetch("/api/admin/features/reorder", {
        method: "POST",
        headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status} ${await res.text().catch(() => "")}`);
      // update local state
      setFeatures((prev) =>
        prev.map((r) => {
          if (r.name === a.name) return { ...r, ...patchA };
          if (r.name === b.name) return { ...r, ...patchB };
          return r;
        })
      );
      setEdits((prev) => {
        const n = { ...prev };
        delete n[a.name]; delete n[b.name];
        return n;
      });
    } catch (e: any) {
      alert(`Failed to reorder: ${e.message}`);
    }
  };

  const filtered = features.filter(
    (f) =>
      !search ||
      (f.name ?? "").toLowerCase().includes(search.toLowerCase()) ||
      (f.display_name ?? "").toLowerCase().includes(search.toLowerCase())
  );

  // Display rows in the admin-chosen sort_order so the order shown here matches
  // the Detailed Analysis Stats rendering. Filtering applied on top of ordering.
  const filteredOrdered = orderedRows.filter(
    (f) =>
      !search ||
      (f.name ?? "").toLowerCase().includes(search.toLowerCase()) ||
      (f.display_name ?? "").toLowerCase().includes(search.toLowerCase())
  );

  return (
    <div>
      <div className="mb-8">
        <h1 className="text-2xl font-bold text-white">Features</h1>
        <p className="text-gray-400 text-sm mt-1">
          Edit feature display names, descriptions, trainability, and pick card visibility
        </p>
      </div>

      {/* Sport selector */}
      <div className="flex flex-wrap items-center gap-3 mb-6">
        <div className="flex gap-2">
          {SPORTS.map((s) => (
            <button
              key={s}
              onClick={() => { setSport(s); setSearch(""); }}
              className={`px-5 py-2 rounded-lg text-sm font-semibold transition border ${
                sport === s
                  ? "bg-earl-600/20 text-earl-400 border-earl-600/30"
                  : "bg-white/5 text-gray-400 border-white/10 hover:text-white hover:bg-white/10"
              }`}
            >
              {sportLabel[s]}
            </button>
          ))}
        </div>
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search features…"
          className="ml-auto px-3 py-2 rounded-lg bg-white/5 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-earl-600/40"
        />
      </div>

      <div className="text-xs text-gray-500 mb-3">
        {loading ? "Loading…" : `${filtered.length} feature${filtered.length === 1 ? "" : "s"} (${sportLabel[sport].toUpperCase()})`}
      </div>

      {loading ? (
        <div className="text-gray-400">Loading features…</div>
      ) : (
        <div className="bg-white/[0.02] border border-white/10 rounded-lg overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs text-gray-500 uppercase tracking-wider border-b border-white/10">
                <th className="px-4 py-3">Name</th>
                <th className="px-4 py-3">Display Name</th>
                <th className="px-4 py-3">Description</th>
                <th className="px-4 py-3 text-center">Trainable</th>
                <th className="px-4 py-3 text-center">Pick Card</th>
                <th className="px-4 py-3">Pick Card Section</th>
                <th className="px-4 py-3 text-center">Order</th>
                <th className="px-4 py-3 text-center">Current</th>
                <th className="px-4 py-3 text-right">Save</th>
              </tr>
            </thead>
            <tbody>
              {filteredOrdered.map((f, i) => {
                const e = edits[f.name] ?? {};
                const displayName = (e.display_name !== undefined ? e.display_name : (f.display_name ?? "")) ?? "";
                const description = (e.description !== undefined ? e.description : (f.description ?? "")) ?? "";
                const trainable = e.is_trainable !== undefined ? e.is_trainable : f.is_trainable;
                const pickCard = e.pick_card !== undefined ? e.pick_card : f.pick_card;
                const pickCardSection =
                  (e.pick_card_section !== undefined ? e.pick_card_section : (f.pick_card_section ?? "")) ?? "";
                const dirty = isDirty(f);
                return (
                  <tr
                    key={f.name}
                    className={`border-b border-white/5 align-top ${dirty ? "bg-earl-600/[0.07]" : ""}`}
                  >
                    {/* Name (read-only) */}
                    <td className="px-4 py-3 font-mono text-xs text-blue-300 whitespace-nowrap">{f.name}</td>

                    {/* Display name */}
                    <td className="px-4 py-3">
                      <input
                        type="text"
                        value={displayName}
                        onChange={(ev) => setEdit(f.name, { display_name: ev.target.value })}
                        placeholder={f.name}
                        className="w-full px-2 py-1 rounded bg-white/5 border border-white/10 text-white text-xs focus:outline-none focus:border-earl-600/40"
                      />
                    </td>

                    {/* Description */}
                    <td className="px-4 py-3 min-w-[220px]">
                      <textarea
                        value={description}
                        onChange={(ev) => setEdit(f.name, { description: ev.target.value })}
                        rows={2}
                        placeholder="No description"
                        className="w-full px-2 py-1 rounded bg-white/5 border border-white/10 text-white text-xs resize-y focus:outline-none focus:border-earl-600/40"
                      />
                    </td>

                    {/* Trainable toggle */}
                    <td className="px-4 py-3 text-center">
                      <Toggle
                        checked={trainable}
                        onChange={(v) => setEdit(f.name, { is_trainable: v })}
                        label={trainable ? "Yes" : "No"}
                      />
                    </td>

                    {/* Pick card toggle */}
                    <td className="px-4 py-3 text-center">
                      <Toggle
                        checked={pickCard}
                        onChange={(v) => setEdit(f.name, { pick_card: v })}
                        label={pickCard ? "Yes" : "No"}
                      />
                    </td>

                    {/* Pick card section dropdown */}
                    <td className="px-4 py-3">
                      <select
                        value={pickCardSection}
                        onChange={(ev) => setEdit(f.name, { pick_card_section: ev.target.value })}
                        className={`w-full px-2 py-1 rounded bg-white/5 border text-xs focus:outline-none focus:border-earl-600/40 cursor-pointer ${
                          pickCardSection ? "text-white border-white/10" : "text-gray-500 border-white/10"
                        }`}
                      >
                        <option value="" disabled>
                          — select —
                        </option>
                        {PICK_CARD_SECTIONS.map((section) => (
                          <option key={section} value={section} className="text-gray-900">
                            {section.replace(/_/g, " ")}
                          </option>
                        ))}
                      </select>
                    </td>

                    {/* Order (up/down reorder within display order) */}
                    <td className="px-2 py-3 text-center whitespace-nowrap">
                      <div className="flex items-center justify-center gap-1">
                        <button
                          type="button"
                          disabled={i === 0}
                          onClick={() =>
                            i > 0 && reorder(f, filteredOrdered[i - 1])
                          }
                          title="Move up (earlier in Detailed Analysis)"
                          className="w-6 h-6 rounded bg-white/5 border border-white/10 text-xs text-gray-200 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          ↑
                        </button>
                        <button
                          type="button"
                          disabled={i === filteredOrdered.length - 1}
                          onClick={() =>
                            i < filteredOrdered.length - 1 &&
                            reorder(f, filteredOrdered[i + 1])
                          }
                          title="Move down (later in Detailed Analysis)"
                          className="w-6 h-6 rounded bg-white/5 border border-white/10 text-xs text-gray-200 hover:bg-white/10 disabled:opacity-30 disabled:cursor-not-allowed"
                        >
                          ↓
                        </button>
                      </div>
                    </td>

                    {/* Current (read-only context) */}
                    <td className="px-4 py-3 text-center whitespace-nowrap">
                      <div className="text-[10px] text-gray-500">ATS</div>
                      <div className="text-xs text-gray-300">{f.current_ats ?? "—"}</div>
                      <div className="text-[10px] text-gray-500 mt-1">OU</div>
                      <div className="text-xs text-gray-300">{f.current_ou ?? "—"}</div>
                    </td>

                    {/* Save */}
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => saveRow(f)}
                        disabled={!dirty || saving[f.name]}
                        className={`px-3 py-1.5 rounded-lg text-xs font-semibold transition border ${
                          !dirty
                            ? "text-gray-600 border-white/5 cursor-default"
                            : "bg-earl-600/20 text-earl-400 border-earl-600/30 hover:bg-earl-600/30"
                        } disabled:opacity-50`}
                      >
                        {saving[f.name] ? "Saving…" : dirty ? "Save" : "Saved"}
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {filtered.length === 0 && (
            <div className="px-4 py-8 text-center text-gray-500">No features found.</div>
          )}
        </div>
      )}
    </div>
  );
}

function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: string }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`inline-flex items-center gap-2 text-xs font-medium transition ${
        checked ? "text-green-400" : "text-gray-500"
      }`}
      title={checked ? "Click to disable" : "Click to enable"}
    >
      <span
        className={`relative inline-flex h-5 w-9 items-center rounded-full transition ${
          checked ? "bg-green-600/70" : "bg-white/15"
        }`}
      >
        <span
          className={`inline-block h-4 w-4 transform rounded-full bg-white transition ${
            checked ? "translate-x-4" : "translate-x-0.5"
          }`}
        />
      </span>
      {label}
    </button>
  );
}
