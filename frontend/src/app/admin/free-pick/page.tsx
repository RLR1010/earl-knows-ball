"use client";

import { useEffect, useState, useCallback } from "react";
import { useSeo } from "@/components/Seo";

const SPORTS = ["mlb", "nfl", "nba"] as const;
type Sport = (typeof SPORTS)[number];
const SPORT_LABEL: Record<Sport, string> = { mlb: "MLB", nfl: "NFL", nba: "NBA" };

/** Pick payload mirrors the backend free_pick router. */
interface Pick {
  sport: Sport;
  game_id: number;
  writeup_id: number;
  title: string | null;
  slug?: string | null;
  published_at?: string | null;
  is_free_feature?: boolean;
  free_featured_at?: string | null;
  preview_image?: string | null;
  seo_description?: string | null;
  social_caption?: string | null;
  has_premium?: boolean;
  game_date?: string | null;
  content?: {
    title?: string | null;
    content?: string | null;
    preview_image?: string | null;
  } | null;
}

const token = () => localStorage.getItem("earl_token");
const JSON_HEADERS = { "Content-Type": "application/json" };
const authHeaders = (extra: Record<string, string> = {}) => {
  const t = token();
  return { ...JSON_HEADERS, ...(t ? { Authorization: `Bearer ${t}` } : {}), ...extra };
};

function apiImage(src?: string | null): string | undefined {
  if (!src) return undefined;
  if (src.startsWith("http")) return src;
  return src.startsWith("/api/") ? src : `/api${src.startsWith("/") ? "" : "/"}${src}`;
}

function fmtPct(s?: string | null): string {
  if (!s) return "";
  try {
    const d = new Date(s);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

export default function FreePickAdminPage() {
  useSeo({ title: "Free Pick Admin | Earl Knows Ball" });
  const [sportFilter, setSportFilter] = useState<Sport | "all">("all");
  const [cands, setCands] = useState<Pick[]>([]);
  const [current, setCurrent] = useState<Pick | null>(null);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      // current active free pick
      const cur = await fetch("/api/writeups/free-pick", { cache: "no-store" });
      if (cur.ok) setCurrent(await cur.json());
      else setCurrent(null);
    } catch {
      setCurrent(null);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    let active = true;
    const sp = sportFilter === "all" ? "" : `?sport=${sportFilter}&limit=60`;
    fetch(`/api/writeups/admin/free-pick/candidates${sp}`, {
      headers: authHeaders(),
      cache: "no-store",
    })
      .then((r) => (r.ok ? r.json() : []))
      .then((d) => active && setCands(d || []))
      .catch(() => active && setCands([]));
    return () => {
      active = false;
    };
  }, [sportFilter]);

  async function publish(p: Pick) {
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await fetch(
        `/api/writeups/admin/free-pick/${p.sport}/${p.game_id}?force_regenerate_card=true`,
        { method: "POST", headers: authHeaders() }
      );
      if (!r.ok) {
        const t = await r.text();
        setErr(`Publish failed (${r.status}): ${t.slice(0, 300)}`);
        return;
      }
      const data = await r.json();
      if (data?.content) setCurrent(data);
      await refresh();
      setMsg(`Now live as Free Pick: ${p.sport.toUpperCase()} "${p.title}"`);
    } catch (e: any) {
      setErr(`Publish error: ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  async function retract() {
    if (!current) return;
    setBusy(true);
    setErr(null);
    setMsg(null);
    try {
      const r = await fetch(
        `/api/writeups/admin/free-pick/${current.sport}/${current.game_id}`,
        { method: "DELETE", headers: authHeaders() }
      );
      if (!r.ok) {
        const t = await r.text();
        setErr(`Retract failed (${r.status}): ${t.slice(0, 300)}`);
        return;
      }
      setCurrent(null);
      await refresh();
      setMsg("Free Pick retracted. The homepage section is now hidden.");
    } catch (e: any) {
      setErr(`Retract error: ${e?.message || e}`);
    } finally {
      setBusy(false);
    }
  }

  const curImg = apiImage(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (current as any)?.content?.premium_social_card ||
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      (current as any)?.premium_social_card ||
      current?.content?.preview_image ||
      current?.preview_image
  );

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="font-display text-2xl font-black text-white">Free Pick of the Season</h1>
          <p className="text-sm text-gray-400">
            Feature one premium game write-up and make it available to everyone (paywall waived for that
            game only). Only one can be live at a time.
          </p>
        </div>
        <span className="rounded-full bg-earl-600/20 px-3 py-1 text-xs font-bold text-earl-300">
          {current ? "● LIVE" : "○ none"}
        </span>
      </div>

      {msg ? (
        <div className="mb-4 rounded-lg border border-green-500/40 bg-green-500/10 px-4 py-2 text-sm text-green-200">
          {msg}
        </div>
      ) : null}
      {err ? (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-2 text-sm text-red-200">
          {err}
        </div>
      ) : null}

      {/* Current active */}
      <section className="mb-10 rounded-2xl border border-gray-800 p-6">
        <h2 className="mb-4 text-sm font-black uppercase tracking-widest text-gray-400">
          Currently featured
        </h2>
        {current ? (
          <div className="grid gap-6 md:grid-cols-[1fr_340px]">
            <div>
              <div className="mb-2 flex items-center gap-2">
                <span className="rounded bg-green-500/20 px-2 py-0.5 text-xs font-black text-green-300">
                  {SPORT_LABEL[current.sport]}
                </span>
                <span className="text-xs text-gray-500">
                  featured {fmtPct(current.free_featured_at)}
                </span>
              </div>
              <h3 className="font-display text-2xl font-black text-white">{current.title}</h3>
              <p className="mt-2 line-clamp-3 text-sm text-gray-400">
                {current.social_caption || current.seo_description || "—"}
              </p>
              {current.slug ? (
                <p className="mt-2 text-xs text-gray-500">
                  <span className="text-gray-400">Runs at:</span>/{current.sport}/analysis/{current.slug}
                </p>
              ) : null}
              <button
                onClick={retract}
                disabled={busy}
                className="mt-4 rounded-lg bg-red-600 px-4 py-2 text-sm font-bold text-white transition hover:bg-red-500 disabled:opacity-50"
              >
                {busy ? "Working…" : "Retract (turn off)"}
              </button>
            </div>
            {curImg ? (
              <div className="overflow-hidden rounded-xl border border-gray-800">
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={curImg} alt={current.title || "free pick"} className="w-full" />
              </div>
            ) : null}
          </div>
        ) : (
          <p className="text-gray-500">
            No Free Pick is live. Select a write-up below to feature it.
          </p>
        )}
      </section>

      {/* Picker */}
      <section>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-black uppercase tracking-widest text-gray-400">
            Pick a premium write-up to give away
          </h2>
          <div className="flex gap-2">
            {(["all", ...SPORTS] as const).map((s) => (
              <button
                key={s}
                onClick={() => setSportFilter(s)}
                className={
                  "rounded-lg px-3 py-1.5 text-sm font-bold transition " +
                  (sportFilter === s
                    ? "bg-earl-600 text-white"
                    : "bg-gray-800 text-gray-300 hover:bg-gray-700")
                }
              >
                {s === "all" ? "All" : SPORT_LABEL[s]}
              </button>
            ))}
          </div>
        </div>

        <div className="space-y-2">
          {cands.length === 0 ? (
            <p className="py-8 text-center text-sm text-gray-500">
              No published write-ups found for this sport.
            </p>
          ) : (
            cands.map((p) => {
              const isCurrent = current?.game_id === p.game_id && current?.sport === p.sport;
              return (
                <div
                  key={`${p.sport}-${p.game_id}`}
                  className={
                    "flex items-center justify-between gap-4 rounded-xl border px-4 py-3 transition " +
                    (isCurrent ? "border-green-500/60 bg-green-500/5" : "border-gray-800 bg-gray-900/40")
                  }
                >
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="rounded bg-gray-700 px-2 py-0.5 text-[11px] font-black uppercase text-gray-200">
                        {SPORT_LABEL[p.sport]}
                      </span>
                      {p.has_premium && (
                        <span className="rounded bg-amber-500/20 px-2 py-0.5 text-[11px] font-bold text-amber-300">
                          premium
                        </span>
                      )}
                      {isCurrent && (
                        <span className="rounded bg-green-500/20 px-2 py-0.5 text-[11px] font-bold text-green-300">
                          ← live
                        </span>
                      )}
                    </div>
                    <p className="mt-1 truncate font-semibold text-white">{p.title}</p>
                    <p className="text-xs text-gray-500">
                      game {p.game_id}
                      {p.published_at ? ` · published ${fmtPct(p.published_at)}` : ""}
                    </p>
                  </div>
                  <button
                    onClick={() => publish(p)}
                    disabled={busy || isCurrent}
                    className={
                      "shrink-0 rounded-lg px-4 py-2 text-sm font-bold transition disabled:opacity-40 " +
                      (isCurrent
                        ? "bg-gray-700 text-gray-300"
                        : "bg-earl-600 text-white hover:bg-earl-500")
                    }
                  >
                    {isCurrent ? "Live" : busy ? "Working…" : "Feature →"}
                  </button>
                </div>
              );
            })
          )}
        </div>
      </section>
    </div>
  );
}
