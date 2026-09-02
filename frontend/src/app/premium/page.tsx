"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "@/components/LoginModal";

const FEATURES = [
  {
    title: "AI Handicapping Chat",
    desc: "Ask Earl anything about a game, team, or player across NFL, MLB, and NBA. Get sharp, data-backed answers in plain English — not vague guru talk.",
  },
  {
    title: "Model Picks with Probabilities",
    desc: "Every pick comes with a real probability, not a gut feel. Moneyline, run line, and over/under picks backed by machine-learning models.",
  },
  {
    title: "Advanced Stats, Demystified",
    desc: "Rolling and cumulative advanced metrics (Ortg, Drtg, Pace, eFG%, EV, win probability) that go deeper than the box score — and our stats are accurate to basketball-reference standards.",
  },
  {
    title: "Daily Best Bets",
    desc: "Fresh picks every day for every game that has lines. See exactly what the model likes and why.",
  },
  {
    title: "Parlay EV Guardrail",
    desc: "Know the truth before you bet. We expose expected value and call out compounded vig, so you never fool yourself into a sucker parlay.",
  },
  {
    title: "A Track Record You Can Audit",
    desc: "Your picks are timestamped and immutable. See the real record — wins, losses, ROI — before you trust a single bet.",
  },
];

const FAQ = [
  {
    q: "What does the $1.95 trial include?",
    a: "Full Premium access for 2 days — every sport, every pick, every stat. No limits during the trial.",
  },
  {
    q: "What happens after the trial?",
    a: "You convert automatically to Premium at $29.95/month. The $1.95 is a one-time trial fee — the $29.95 charges only after your 2 days end.",
  },
  {
    q: "Can I cancel?",
    a: "Absolutely. Cancel anytime — before the trial ends and you're never charged $29.95. No contracts, no lock-in.",
  },
  {
    q: "Do you guarantee wins?",
    a: "No one can guarantee wins, and we won't pretend to. We give you probabilities, honesty, and a verifiable record — that's what actually helps you bet smarter.",
  },
];

export default function PremiumPage() {
  const router = useRouter();
  const { user } = useAuth();
  const [loading, setLoading] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);

  const isPremium =
    user?.subscription_tier === "premium" ||
    user?.subscription_tier === "premium_yearly";

  const startTrial = () => {
    setLoading(true);
    if (!user) {
      // Not logged in: open the login modal, then continue to checkout on success.
      setLoginOpen(true);
      return;
    }
    router.push(`/checkout?plan=premium-trial`);
  };

  const handleTrialLoginSuccess = () => {
    setLoginOpen(false);
    setLoading(false);
    router.push(`/checkout?plan=premium-trial`);
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-earl-50 via-white to-white text-slate-800">
      {/* ── Hero ─────────────────────────────────────────────── */}
      <section className="relative overflow-hidden border-b border-earl-100 bg-gradient-to-br from-earl-900 via-earl-800 to-earl-950 text-white">
        <div className="mx-auto max-w-6xl px-6 py-20 text-center">
          <span className="mb-4 inline-block rounded-full bg-white/10 px-4 py-1.5 text-sm font-semibold tracking-wide text-earl-200 ring-1 ring-white/20">
            Try Earl Knows Ball — only $1.95 for 2 days
          </span>
          <h1 className="font-display text-4xl font-bold leading-tight sm:text-5xl md:text-6xl">
            Stop guessing.<br />
            <span className="text-earl-300">Let the model do the homework.</span>
          </h1>
          <p className="mx-auto mt-6 max-w-2xl text-lg text-earl-100/90">
            AI handicapping for NFL, MLB &amp; NBA. Every pick has a probability.
            Every pick is timestamped and auditable. Advanced stats on demand.
            <span className="font-semibold text-white"> No blurry gut feelings — just data you can verify.</span>
          </p>

          <div className="mt-10 flex flex-col items-center justify-center gap-4 sm:flex-row">
            {isPremium ? (
              <div className="rounded-xl bg-emerald-600 px-8 py-4 text-lg font-bold shadow-lg">
                ✅ You&apos;re already Premium
              </div>
            ) : (
              <button
                onClick={startTrial}
                disabled={loading}
                className="rounded-xl bg-emerald-500 px-8 py-4 text-lg font-bold text-white shadow-xl transition hover:bg-emerald-400 disabled:opacity-60"
              >
                {loading ? "Starting checked out…" : "Start 2-Day Trial — $1.95"}
              </button>
            )}
            <a
              href="#features"
              className="rounded-xl border border-white/30 bg-white/5 px-8 py-4 text-lg font-semibold text-white transition hover:bg-white/10"
            >
              See what&apos;s included
            </a>
          </div>

          <div className="mt-8 flex flex-wrap items-center justify-center gap-x-6 gap-y-2 text-sm text-earl-200/80">
            <span>No risk beyond $1.95</span>
            <span>·</span>
            <span>Cancel anytime</span>
            <span>·</span>
            <span>Converts to $29.95/mo after trial</span>
            <span>·</span>
            <span>21+ only · Bet responsibly</span>
          </div>
        </div>
      </section>

      {/* ── The problem / value hook ─────────────────────────── */}
      <section className="mx-auto max-w-5xl px-6 py-16">
        <div className="grid gap-8 md:grid-cols-3">
          {[
            { t: "Shiny object", d: "Most pick services sell you confidence. They can't show you a track record." },
            { t: "Vague picks", d: "Guru 'picks' with no probability = no way to know if they're good or lucky." },
            { t: "Hidden vig", d: "Parlays and books stack the odds against you — most bettors never see the edge." },
          ].map((c) => (
            <div key={c.t} className="rounded-2xl border border-earl-100 bg-white p-6 shadow-sm">
              <div className="text-sm font-bold uppercase tracking-wide text-earl-600">The problem → {c.t}</div>
              <p className="mt-2 text-slate-600">{c.d}</p>
            </div>
          ))}
        </div>
        <p className="mx-auto mt-8 max-w-2xl text-center text-lg text-slate-700">
          Earl flips all three. <span className="font-semibold text-earl-700">Receipts, probabilities, and honest math</span> — so you bet with your head, not your hopes.
        </p>
      </section>

      {/* ── Features grid ────────────────────────────────────── */}
      <section id="features" className="bg-white py-16">
        <div className="mx-auto max-w-6xl px-6">
          <h2 className="text-center font-display text-3xl font-bold text-earl-900 sm:text-4xl">
            Everything inside Premium
          </h2>
          <p className="mx-auto mt-3 max-w-xl text-center text-slate-600">
            One membership. Every sport. Every stat. Every pick.
          </p>
          <div className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((f) => (
              <div
                key={f.title}
                className="rounded-2xl border border-earl-100 bg-earl-50/50 p-6 transition hover:border-earl-300 hover:shadow-md"
              >
                <h3 className="mt-3 text-lg font-bold text-earl-900">{f.title}</h3>
                <p className="mt-1.5 text-sm text-slate-600">{f.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ── Pricing / offer ──────────────────────────────────── */}
      <section className="bg-gradient-to-b from-white to-earl-50 py-16">
        <div className="mx-auto max-w-4xl px-6">
          <div className="overflow-hidden rounded-3xl border border-earl-200 bg-white shadow-xl">
            <div className="bg-earl-900 px-8 py-6 text-center text-white">
              <span className="text-sm font-semibold uppercase tracking-widest text-earl-300">Get started</span>
              <h3 className="mt-1 font-display text-2xl font-bold">Premium 2-Day Trial</h3>
            </div>
            <div className="px-8 py-8">
              <div className="flex items-end justify-center gap-2">
                <span className="text-5xl font-bold text-earl-900">$1.95</span>
                <span className="pb-1.5 text-slate-500">for 2 days</span>
              </div>
              <p className="mt-1 text-center text-sm text-slate-500">
                then converts to <span className="font-semibold text-slate-700">$29.95/month</span> · cancel anytime
              </p>

              <ul className="mx-auto mt-6 max-w-md space-y-2.5">
                {[
                  "Full Premium access for 2 days",
                  "All sports: NFL, MLB & NBA",
                  "Model picks + probabilities + daily best bets",
                  "Advanced stats & AI chat — no limits",
                  "One-time $1.95 — no charge until trial ends",
                  "Cancel in 2 clicks, keep nothing",
                ].map((li) => (
                  <li key={li} className="flex items-start gap-2 text-slate-700">
                    <span className="mt-0.5 text-emerald-600">✓</span>
                    <span>{li}</span>
                  </li>
                ))}
              </ul>

              <div className="mt-8 text-center">
                {isPremium ? (
                  <div className="inline-block rounded-xl bg-emerald-600 px-8 py-4 text-lg font-bold text-white">
                    ✅ You&apos;re already Premium
                  </div>
                ) : (
                  <button
                    onClick={startTrial}
                    disabled={loading}
                    className="rounded-xl bg-emerald-500 px-10 py-4 text-lg font-bold text-white shadow-lg transition hover:bg-emerald-400 disabled:opacity-60"
                  >
                    {loading ? "Starting…" : "Start 2-Day Trial — $1.95"}
                  </button>
                )}
                <p className="mt-3 text-xs text-slate-400">
                  Secure checkout via Stripe · No hidden fees · Cancel anytime
                </p>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── FAQ ──────────────────────────────────────────────── */}
      <section className="mx-auto max-w-3xl px-6 py-16">
        <h2 className="text-center font-display text-3xl font-bold text-earl-900">
          Questions, answered
        </h2>
        <div className="mt-8 space-y-4">
          {FAQ.map((item) => (
            <div key={item.q} className="rounded-2xl border border-earl-100 bg-white p-5">
              <div className="font-semibold text-earl-900">{item.q}</div>
              <p className="mt-1.5 text-slate-600">{item.a}</p>
            </div>
          ))}
        </div>
        <p className="mt-8 text-center text-xs text-slate-400">
          Gambling problem? Call 1-800-GAMBLER. Must be 21+. Past performance is not a guarantee of future results.
        </p>
      </section>
      <LoginModal open={loginOpen} onClose={() => { setLoginOpen(false); setLoading(false); }} onSuccess={handleTrialLoginSuccess} />
    </div>
  );
}
