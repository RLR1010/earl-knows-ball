"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import type {
  ParlayCorrelation,
  ParlayGame,
  ParlayKind,
  ParlayLeg,
  ParlayLegInput,
  ParlayTicket,
  SavedParlayTicket,
} from "@/lib/api";
import { usePathname, useRouter } from "next/navigation";

// ─────────────────────────────────────────────────────────────────────────
// Parlay math (mirrors backend app/handicapping/parlay.py; pure, tiny, so we
// compute instantly on the client while typing). Keep in sync with the server.
// ─────────────────────────────────────────────────────────────────────────

const LEG_ML: ParlayKind = "ml";
const LEG_SPREAD: ParlayKind = "spread";
const LEG_TOTAL: ParlayKind = "total";

const KIND_LABEL: Record<ParlayKind, string> = {
  ml: "Moneyline",
  spread: "Spread",
  total: "Total",
};

function americanToDecimal(odds: number | null): number {
  if (odds == null) return 1;
  if (odds > 0) return 1 + odds / 100;
  return 1 + 100 / Math.abs(odds);
}

function decimalToAmerican(dec: number): number {
  if (dec <= 1) return 100;
  if (dec >= 2) return Math.round((dec - 1) * 100);
  return Math.round(-100 / (dec - 1));
}

function fmtOdds(odds: number | null): string {
  if (odds == null) return "n/a";
  const v = Math.round(odds);
  return v > 0 ? `+${v}` : String(v);
}

function fmtPct(n: number | null): string {
  if (n == null) return "–";
  return `${(n * 100).toFixed(1)}%`;
}

function fmtDollars(n: number | null): string {
  if (n == null) return "–";
  return `$${n.toFixed(2)}`;
}

function safeProb(p: number | null): number {
  if (p == null || Number.isNaN(p)) return 0.5;
  return Math.max(0.01, Math.min(0.99, p));
}

/** Opposite-sides check for the correlation guard (mirror server). */
function sidesOppose(a?: string | null, b?: string | null): boolean {
  if (!a || !b) return false;
  // compare the first token (team token) ignoring the line portion
  return String(a).split(" ")[0].toLowerCase() === String(b).split(" ")[0].toLowerCase();
}

/** Leg category for the empirical correlation lookup (mirror server _pair_keys). */
function legCat(leg: ParlayLegInput): string {
  const k = leg.kind;
  if (k === LEG_ML) {
    const side = String(leg.side || "").toLowerCase();
    const fav = String(leg.favorite_side || "").toLowerCase();
    return fav && side === fav ? "ml_fav" : "ml_dog";
  }
  if (k === LEG_TOTAL) {
    return String(leg.pick || "").toLowerCase() === "over" ? "total_over" : "total_under";
  }
  return "spread";
}

const CORR_WARN = 0.02;
const CORR_STRONG = 0.05;

function sameGame(a: ParlayLegInput, b: ParlayLegInput): boolean {
  // same-game only when both the sport AND game id match — never across sports,
  // since game_ids can collide between MLB / NFL / NBA.
  return (a.sport ?? "").toLowerCase() === (b.sport ?? "").toLowerCase() && a.game_id === b.game_id;
}

function computeCorrelation(
  legs: ParlayLegInput[],
  correlations: Record<string, ParlayCorrelation> | null,
): { blocks: string[]; warnings: string[]; independentNote?: string } {
  const blocks: string[] = [];
  const warnings: string[] = [];
  let sameGameTotals = 0; // for the honest "no material correlation" note
  for (let i = 0; i < legs.length; i++) {
    for (let j = i + 1; j < legs.length; j++) {
      const a = legs[i];
      const b = legs[j];
      if (!sameGame(a, b)) continue; // cross-game / cross-sport: independent
      const ka = a.kind;
      const kb = b.kind;

      // 1) Structural block: same-game ML + spread on the SAME team.
      if ((ka === LEG_ML && kb === LEG_SPREAD) || (ka === LEG_SPREAD && kb === LEG_ML)) {
        const ml = ka === LEG_ML ? a : b;
        const sp = ka === LEG_SPREAD ? a : b;
        if (ml.side && sp.side && sidesOppose(ml.side, sp.side)) {
          blocks.push("ML + spread on the same team are near-duplicates (blocked)");
        }
        continue; // don't also run the empirical lookup for the blocked pair
      }

      // 2) Data-driven same-game correlation for every OTHER same-game pair
      //    (ML+total, spread+total). Uses the empirical table, not heuristics.
      //    Near-zero corr => honest "no material correlation"
      if (correlations) {
        const [ca, cb] = [legCat(a), legCat(b)];
        const rec =
          correlations[`${ca}:${cb}`] ?? correlations[`${cb}:${ca}`] ?? null;
        sameGameTotals++;
        if (rec && Math.abs(rec.corr) >= CORR_WARN && rec.n >= 30) {
          const dir = rec.corr > 0 ? "correlated" : "negatively correlated";
          const str = Math.abs(rec.corr) >= CORR_STRONG ? "strongly" : "mildly";
          warnings.push(
            `Same-game ${dir} (${str}): historical joint-hit runs ${(
              Math.abs(rec.corr) * 100
            ).toFixed(1)}pp vs independence (n=${rec.n}) — fair price may understate the true vig`,
          );
        }
      }
    }
  }
  const independentNote =
    sameGameTotals > 0 && warnings.length === 0
      ? "Same-game ML/total legs showed no material historical correlation."
      : undefined;
  return { blocks, warnings, independentNote };
}

function computeTicket(
  legs: ParlayLegInput[],
  correlations: Record<string, ParlayCorrelation> | null,
): ParlayTicket {
  const probs = legs.map((l) => safeProb(l.prob));
  let fairProb = probs.reduce((acc, p) => acc * p, 1);
  fairProb = Math.max(1e-6, fairProb);
  const fairDecimal = 1 / fairProb;
  const bookDecimal = legs.reduce((acc, l) => acc * americanToDecimal(l.odds), 1);
  const combinedImplied = bookDecimal > 0 ? 1 / bookDecimal : 1;
  const { blocks, warnings, independentNote } = computeCorrelation(legs, correlations);
  return {
    n_legs: legs.length,
    fair_probability: fairProb,
    fair_decimal: fairDecimal,
    fair_american: decimalToAmerican(fairDecimal),
    book_decimal: bookDecimal,
    book_american: decimalToAmerican(bookDecimal),
    combined_implied: combinedImplied,
    vig_drag: combinedImplied - fairProb,
    ev_pct: (bookDecimal / fairDecimal - 1) * 100,
    ev_dollars: (bookDecimal / fairDecimal - 1) * 100,
    correlation_warnings: warnings,
    correlation_blocks: blocks,
    independent_note: independentNote,
    legs: [] as ParlayLeg[],
  };
}

// dedupe key: a game can only contribute one leg of each kind. Include the
// sport so legs from MLB / NFL / NBA never collide even when game_ids overlap.
function legKey(leg: { game_id: number; kind: ParlayKind; sport?: string }) {
  return `${leg.sport ?? ""}:${leg.game_id}:${leg.kind}`;
}

// ─────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────

export default function ParlayBuilder({
  sport = "mlb",
  containerClassName = "max-w-6xl mx-auto px-4 py-8",
}: {
  sport?: "mlb" | "nfl" | "nba";
  containerClassName?: string;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const [games, setGames] = useState<ParlayGame[]>([]);
  const [correlations, setCorrelations] = useState<Record<string, ParlayCorrelation> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Working ticket is persisted to localStorage so picks survive navigation
  // across sports (/mlb/parlay -> /nfl/parlay -> /nba/parlay) without hitting
  // Save — the user can mix MLB + NFL + NBA legs on one running ticket.
  const LS_SELECTED = "parlay.selected.v1";
  const LS_NAME = "parlay.name.v1";
  const readLs = (key: string): string | null => {
    if (typeof window === "undefined") return null;
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  };
  const [selected, setSelected] = useState<ParlayLegInput[]>(() => {
    const raw = readLs(LS_SELECTED);
    if (!raw) return [];
    try {
      const arr = JSON.parse(raw);
      return Array.isArray(arr) ? (arr as ParlayLegInput[]) : [];
    } catch {
      return [];
    }
  });
  const [filterKind, setFilterKind] = useState<ParlayKind | "all">("all");
  const [savedTickets, setSavedTickets] = useState<SavedParlayTicket[]>([]);
  const [ticketName, setTicketName] = useState(() => readLs(LS_NAME) || "My Parlay");
  const [ticketDirty, setTicketDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [ticketsOpen, setTicketsOpen] = useState(false);
  const [ticketMsg, setTicketMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  const SPORT_LABEL: Record<string, string> = {
    mlb: "MLB",
    nfl: "NFL",
    nba: "NBA",
  };

  // load the user's saved tickets once (best-effort; silently ignore if not premium/auth'd)
  useEffect(() => {
    let active = true;
    api.parlay
      .listTickets()
      .then((res) => {
        if (active) setSavedTickets(res.tickets ?? []);
      })
      .catch(() => {
        /* not premium / not signed in — just hide the loader */
      });
    return () => {
      active = false;
    };
  }, [sport]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api.parlay
      .legs(sport)
      .then((res) => {
        if (cancelled) return;
        setGames(res.games ?? []);
        setCorrelations(res.correlations ?? null);
        // Re-validate legs for the CURRENT sport only. Cross-sport legs are
        // kept so a ticket can mix picks from MLB + NFL + NBA.
        setSelected((prev) =>
          prev.filter(
            (p) =>
              (p.sport ?? sport) !== sport || // not ours → keep (other-sport leg)
              (res.games?.some((g) => g.game_id === p.game_id) &&
                res.games?.find((g) => g.game_id === p.game_id)?.legs[p.kind]),
          ),
        );
      })
      .catch((e) => {
        if (!cancelled) {
          setError(e?.message || "Failed to load upcoming legs.");
          setGames([]);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sport]);

  const ticket = useMemo(() => computeTicket(selected, correlations), [selected, correlations]);
  const hasBlocks = ticket.correlation_blocks.length > 0;

  const toggleLeg = useCallback((game: ParlayGame, kind: ParlayKind) => {
    const leg = game.legs[kind];
    if (!leg) return;
    setSelected((prev) => {
      const key = legKey(leg);
      if (prev.some((p) => legKey(p) === key)) {
        return prev.filter((p) => legKey(p) !== key);
      }
      // one leg per (game, kind); build normalized input w/ favorite_side
      const input: ParlayLegInput = {
        ...leg,
        decimal: americanToDecimal(leg.odds),
      };
      return [...prev, input];
    });
  }, []);

  const isSelected = (game: ParlayGame, kind: ParlayKind) =>
    selected.some((p) => p.game_id === game.game_id && p.kind === kind);

  // Persist the running ticket so picks stay (and mix across sports) without
  // needing to hit Save. Keep the ticket name too.
  useEffect(() => {
    try {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(LS_SELECTED, JSON.stringify(selected));
        window.localStorage.setItem(LS_NAME, ticketName);
      }
    } catch {
      /* localStorage unavailable — working ticket just won't persist */
    }
  }, [selected, ticketName, LS_SELECTED, LS_NAME]);

  // ── Saved ticket persistence (premium, cross-sport) ────────────────────
  const clearTicketMsg = useCallback(() => setTicketMsg(null), []);

  const saveTicket = useCallback(async () => {
    if (selected.length === 0) return;
    setSaving(true);
    clearTicketMsg();
    try {
      const saved = await api.parlay.saveTicket({
        name: ticketName.trim() || "My Parlay",
        legs: selected as ParlayLegInput[],
      });
      setSavedTickets((prev) => {
        const rest = prev.filter((t) => t.id !== saved.id);
        return [saved, ...rest];
      });
      setTicketMsg({ kind: "ok", text: `Saved “${saved.name}” (${saved.legs.length} legs).` });
    } catch (e) {
      setTicketMsg({ kind: "err", text: "Couldn't save ticket — premium subscription required." });
    } finally {
      setSaving(false);
    }
  }, [selected, ticketName, clearTicketMsg]);

  const loadTicket = useCallback(
    async (t: SavedParlayTicket) => {
      clearTicketMsg();
      try {
        const fresh = await api.parlay.getTicket(t.id);
        // Restore cross-sport legs verbatim. Each kept leg references the sport
        // it belongs to, so a leftover leg from MLB renders even when viewing NFL.
        setSelected((fresh.legs ?? []) as ParlayLegInput[]);
        setTicketName(fresh.name ?? "My Parlay");
        setTicketMsg({ kind: "ok", text: `Loaded “${fresh.name}”.` });
      } catch (e) {
        setTicketMsg({ kind: "err", text: "Couldn't load that ticket." });
      }
    },
    [clearTicketMsg],
  );

  const deleteTicket = useCallback(
    async (id: number) => {
      clearTicketMsg();
      try {
        await api.parlay.deleteTicket(id);
        setSavedTickets((prev) => prev.filter((t) => t.id !== id));
        setTicketMsg({ kind: "ok", text: "Ticket deleted." });
      } catch (e) {
        setTicketMsg({ kind: "err", text: "Couldn't delete that ticket." });
      }
    },
    [clearTicketMsg],
  );

  const visibleGames = useMemo(() => {
    return games.filter((g) => {
      if (filterKind === "all") return true;
      return Boolean(g.legs[filterKind]);
    });
  }, [games, filterKind]);

  // navigation between sports (left nav already offers it; this is for the header pills)
  const sportTabs: { key: "mlb" | "nfl" | "nba"; label: string }[] = [
    { key: "mlb", label: "MLB" },
    { key: "nfl", label: "NFL" },
    { key: "nba", label: "NBA" },
  ];

  return (
    <div className={containerClassName}>
      <div className="flex flex-col gap-1 mb-6">
        <h1 className="text-2xl font-bold tracking-tight">
          Parlay Builder
        </h1>
        <p className="text-sm text-zinc-400 max-w-2xl">
          Stack Earl&apos;s model picks into a parlay. We show the model&apos;s true
          probability per leg, the fair price, and the <em>real</em> EV — including the
          compound vig the books hide on multi-leg tickets. Add legs from different
          games for independence; watch for same-game correlation flags.
        </p>

        {/* sport pills */}
        <div className="flex items-center gap-2 mt-2">
          {sportTabs.map((t) => {
            const active = pathname.startsWith(`/${t.key}/parlay`) || (sport === t.key);
            return (
              <button
                key={t.key}
                onClick={() => router.push(`/${t.key}/parlay`)}
                className={`px-3 py-1.5 rounded-full text-xs font-semibold uppercase transition ${
                  active
                    ? "bg-white text-black"
                    : "bg-white/5 text-zinc-300 hover:bg-white/10"
                }`}
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[1fr_340px] gap-6">
        {/* ── Leg picker ─────────────────────────────── */}
        <div className="order-last lg:order-first">
          {loading ? (
            <div className="text-zinc-400 text-sm py-10 text-center">Loading legs…</div>
          ) : error ? (
            <div className="text-red-400 text-sm py-10 text-center">{error}</div>
          ) : visibleGames.length === 0 ? (
            <div className="text-zinc-400 text-sm py-10 text-center">
              No upcoming {sport.toUpperCase()} games with predictions right now.
              Check back closer to game day.
            </div>
          ) : (
            <div className="space-y-4">
              {/* kind filter */}
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-xs text-zinc-500 uppercase">Bet type:</span>
                {(["all", "ml", "spread", "total"] as const).map((k) => (
                  <button
                    key={k}
                    onClick={() => setFilterKind(k)}
                    className={`px-2.5 py-1 rounded-md text-xs font-medium transition ${
                      filterKind === k
                        ? "bg-zinc-200 text-black"
                        : "bg-white/5 text-zinc-400 hover:bg-white/10"
                    }`}
                  >
                    {k === "all" ? "All" : KIND_LABEL[k]}
                  </button>
                ))}
              </div>

              {visibleGames.map((game) => (
                <div
                  key={game.game_id}
                  className="rounded-xl border border-white/10 bg-white/[0.03] overflow-hidden"
                >
                  <div className="flex items-center justify-between px-4 py-2.5 bg-white/[0.02] border-b border-white/5">
                    <div className="text-sm font-semibold text-zinc-200">
                      {game.game_label}
                    </div>
                    <div className="text-xs text-zinc-500">
                      {game.date.slice(0, 16).replace("T", " ")} CT
                    </div>
                  </div>
                  <div className="p-2">
                    {(Object.keys(game.legs) as ParlayKind[]).map((kind) => {
                      const leg = game.legs[kind];
                      if (!leg || (filterKind !== "all" && filterKind !== kind)) return null;
                      const sel = isSelected(game, kind);
                      return (
                        <button
                          key={kind}
                          onClick={() => toggleLeg(game, kind)}
                          className={`w-full flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg text-left transition border ${
                            sel
                              ? "border-emerald-400/60 bg-emerald-400/10"
                              : "border-transparent hover:bg-white/5"
                          }`}
                        >
                          <div className="flex items-center gap-3 min-w-0">
                            <span
                              className={`w-4 h-4 shrink-0 rounded border flex items-center justify-center text-[10px] ${
                                sel
                                  ? "bg-emerald-400 border-emerald-400 text-black"
                                  : "border-zinc-500 text-transparent"
                              }`}
                            >
                              ✓
                            </span>
                            <div className="min-w-0">
                              <div className="text-sm font-medium text-zinc-100 truncate">
                                {leg.label}
                              </div>
                              <div className="text-[11px] text-zinc-500">
                                {KIND_LABEL[kind]}
                              </div>
                            </div>
                          </div>
                          <div className="flex items-center gap-4 shrink-0">
                            <div className="text-right">
                              <div className="text-[11px] text-zinc-500">Model</div>
                              <div className="text-sm font-semibold text-emerald-300">
                                {fmtPct(leg.prob)}
                              </div>
                            </div>
                            <div className="text-right w-14">
                              <div className="text-[11px] text-zinc-500">Odds</div>
                              <div className="text-sm font-semibold text-zinc-200">
                                {fmtOdds(leg.odds)}
                              </div>
                            </div>
                            <div className="text-right w-16">
                              <div className="text-[11px] text-zinc-500">EV</div>
                              <div
                                className={`text-sm font-semibold ${
                                  leg.ev != null && leg.ev > 0
                                    ? "text-emerald-400"
                                    : "text-zinc-300"
                                }`}
                              >
                                {fmtDollars(leg.ev)}
                              </div>
                            </div>
                          </div>
                        </button>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── Ticket ────────────────────────────────── */}
        <div className="lg:sticky lg:top-20 self-start order-first lg:order-last">
          <div className="rounded-xl border border-white/10 bg-white/[0.04] p-4">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
                Parlay Ticket
              </h2>
              <div className="flex items-center gap-3">
                <span className="text-xs text-zinc-500">{ticket.n_legs} legs</span>
                <button
                  onClick={() => {
                    setSelected([]);
                    setTicketName("My Parlay");
                  }}
                  disabled={selected.length === 0}
                  className="text-xs px-2.5 py-1 rounded-md border border-white/10 text-zinc-300 hover:bg-white/5 hover:text-red-300 disabled:opacity-40 disabled:cursor-not-allowed transition"
                  title="Clear the ticket and start a new parlay"
                >
                  🗑️ Clear
                </button>
              </div>
            </div>

            {/* Save / Load controls (premium) */}
            <div className="mb-4 rounded-lg border border-white/10 bg-white/[0.03] p-2.5 space-y-2">
              <div className="flex items-center gap-2">
                <input
                  value={ticketName}
                  onChange={(e) => setTicketName(e.target.value)}
                  placeholder="Ticket name"
                  className="flex-1 min-w-0 bg-black/30 border border-white/10 rounded-md px-2 py-1.5 text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-emerald-400/50"
                />
                <button
                  onClick={saveTicket}
                  disabled={saving || selected.length === 0}
                  className="shrink-0 text-xs font-semibold px-3 py-1.5 rounded-md bg-emerald-500 text-black hover:bg-emerald-400 disabled:opacity-40 disabled:cursor-not-allowed transition"
                >
                  {saving ? "…" : "💾 Save"}
                </button>
                {savedTickets.length > 0 && (
                  <button
                    onClick={() => setTicketsOpen((o) => !o)}
                    className="shrink-0 text-xs px-3 py-1.5 rounded-md border border-white/10 text-zinc-300 hover:bg-white/5 transition"
                  >
                    📂 {ticketsOpen ? "Hide" : "My tickets"}
                  </button>
                )}
              </div>

              {ticketMsg && (
                <div
                  className={`text-[11px] ${
                    ticketMsg.kind === "ok" ? "text-emerald-400" : "text-red-400"
                  }`}
                >
                  {ticketMsg.text}
                </div>
              )}

              {ticketsOpen && savedTickets.length > 0 && (
                <div className="space-y-1 max-h-36 overflow-y-auto pr-1">
                  {savedTickets.map((t) => (
                    <div
                      key={t.id}
                      className="flex items-center justify-between gap-2 text-sm"
                    >
                      <button
                        onClick={() => loadTicket(t)}
                        className="flex-1 min-w-0 text-left text-zinc-200 hover:text-emerald-300 transition truncate"
                        title={`${t.legs?.length ?? 0} legs`}
                      >
                        {t.name}
                        <span className="text-[10px] text-zinc-500 ml-1">
                          {t.legs?.length ?? 0} legs
                        </span>
                      </button>
                      <button
                        onClick={() => deleteTicket(t.id)}
                        className="shrink-0 text-zinc-500 hover:text-red-400 text-xs"
                        aria-label="Delete ticket"
                      >
                        🗑
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {ticket.n_legs === 0 ? (
              <div className="text-sm text-zinc-500 py-6 text-center">
                Add a leg to see the combined fair value, vig, and EV.
              </div>
            ) : (
              <>
                {/* legs list */}
                <div className="space-y-1.5 mb-4 max-h-60 overflow-y-auto pr-1">
                  {selected.map((leg, i) => (
                    <div
                      key={legKey(leg)}
                      className="flex items-center justify-between gap-2 text-sm px-2 py-1.5 rounded bg-white/5"
                    >
                      <div className="min-w-0 flex items-center gap-2">
                        <span className="shrink-0 text-[9px] font-bold uppercase tracking-wide rounded px-1 py-0.5 bg-white/10 text-zinc-300">
                          {SPORT_LABEL[(leg.sport ?? sport).toLowerCase()] ?? (leg.sport ?? sport)}
                        </span>
                        <div className="min-w-0">
                          <div className="font-medium text-zinc-100 truncate">{leg.label}</div>
                          <div className="text-[10px] text-zinc-500">
                            {leg.game_label} · model {fmtPct(leg.prob)}
                          </div>
                        </div>
                      </div>
                      <button
                        onClick={() =>
                          setSelected((prev) => prev.filter((l, idx) => idx !== i))
                        }
                        className="text-zinc-500 hover:text-red-400 text-xs shrink-0"
                        aria-label="Remove leg"
                      >
                        ✕
                      </button>
                    </div>
                  ))}
                </div>

                {/* correlation flags */}
                {ticket.correlation_blocks.length > 0 && (
                  <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2">
                    <div className="text-xs font-semibold text-red-400 mb-1">
                      ⛔ Blocked — near-duplicate legs
                    </div>
                    {ticket.correlation_blocks.map((b, i) => (
                      <div key={i} className="text-[11px] text-red-300/90">
                        {b}
                      </div>
                    ))}
                    <div className="text-[10px] text-red-300/60 mt-1">
                      Remove the same-team ML/spread pair to proceed.
                    </div>
                  </div>
                )}
                {ticket.correlation_warnings.length > 0 && (
                  <div className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2">
                    <div className="text-xs font-semibold text-amber-400 mb-1">
                      ⚠️ Same-game correlation
                    </div>
                    {ticket.correlation_warnings.map((w, i) => (
                      <div key={i} className="text-[11px] text-amber-300/90">
                        {w}
                      </div>
                    ))}
                    <div className="text-[10px] text-amber-300/60 mt-1">
                      These legs move together — the fair price below may understate
                      the true vig.
                    </div>
                  </div>
                )}
                {ticket.independent_note && (
                  <div className="mb-4 rounded-lg border border-white/10 bg-white/[0.03] px-3 py-2">
                    <div className="text-[11px] text-zinc-400">
                      📊 {ticket.independent_note} We treat them as independent.
                    </div>
                  </div>
                )}

                {/* math summary */}
                <dl className="text-sm space-y-2 border-t border-white/10 pt-3">
                  <div className="flex justify-between">
                    <dt className="text-zinc-400">Model probability</dt>
                    <dd className="font-semibold text-zinc-100">
                      {fmtPct(ticket.fair_probability)}
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-zinc-400">Fair odds</dt>
                    <dd className="font-semibold text-zinc-100">
                      {fmtOdds(ticket.fair_american)} ({ticket.fair_decimal.toFixed(2)})
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-zinc-400">Book pays</dt>
                    <dd className="font-semibold text-zinc-100">
                      {fmtOdds(ticket.book_american)} ({ticket.book_decimal.toFixed(2)})
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-zinc-400">Book implied prob</dt>
                    <dd className="font-semibold text-zinc-100">
                      {fmtPct(ticket.combined_implied)}
                    </dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-zinc-400">Vig (book&apos;s edge)</dt>
                    <dd className="font-semibold text-amber-400">
                      {fmtPct(ticket.vig_drag)}
                    </dd>
                  </div>
                  <div className="flex justify-between border-t border-white/10 pt-2">
                    <dt className="text-zinc-300 font-medium">Parlay EV (on $100 stake)</dt>
                    <dd
                      className={`text-lg font-bold ${
                        ticket.ev_dollars >= 0 ? "text-emerald-400" : "text-red-400"
                      }`}
                    >
                      {ticket.ev_dollars >= 0 ? "+" : ""}
                      {fmtDollars(ticket.ev_dollars)}
                    </dd>
                  </div>
                </dl>

                <div className="mt-3 text-[11px] text-zinc-500 leading-relaxed">
                  Expected profit on a single $100 parlay stake.<br />
                  This is <b>not</b> the sum of the individual leg EV values — each leg EV is a
                  separate $100 bet on its own. A real parlay risks the whole $100 on all legs
                  hitting together, and the book&apos;s vig compounds too. A green EV here does
                  <b> not</b> mean parlays are always the smart play, especially when legs aren&apos;t
                  truly independent.
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
