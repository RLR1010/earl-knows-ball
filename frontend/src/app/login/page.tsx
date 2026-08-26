"use client";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import { useAuth } from "@/lib/auth-context";
import { useSeo } from "@/components/Seo";

export default function LoginPage() {
  useSeo({
    title: "Log In — Earl Knows Ball",
    description: "Log in to Earl Knows Ball to access your AI handicapping chat, premium game picks, and personalized analysis.",
    keywords: "log in, Earl Knows Ball, sports betting, AI handicapping",
  });
  const router = useRouter();
  const { sendCode, verifyCode } = useAuth();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  // Where to send the user after a successful login. Priority:
  //   1. an explicit ?redirect= target (e.g. /chat, or the admin page they
  //      were trying to reach) — the page they intended to go to
  //   2. the previous page in history (they landed here from somewhere)
  //   3. the home page — NOT chat.
  // Reads window.location at submit-time (client event) so this page stays
  // statically prerenderable (no useSearchParams / Suspense needed).
  const resolveDestination = () => {
    const redirect = new URLSearchParams(window.location.search).get("redirect");
    if (redirect && redirect.startsWith("/") && !redirect.startsWith("//")) {
      return { to: redirect, back: false };
    }
    if (window.history.length > 1 && document.referrer) {
      return { to: null, back: true };
    }
    return { to: "/", back: false };
  };

  const handleSendCode = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setMessage("");
    setBusy(true);
    try {
      await sendCode(email);
      setStep("code");
      setMessage("Code sent! Check your email.");
    } catch (err: any) {
      setError(err?.message || "Failed to send code. Check your email address.");
    } finally {
      setBusy(false);
    }
  };

  const handleVerifyCode = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await verifyCode(email, code);
      const { to, back } = resolveDestination();
      if (back && !to) {
        router.back();
      } else {
        router.push(to ?? "/");
      }
    } catch (err: any) {
      setError(err?.message || "Invalid or expired code.");
    } finally {
      setBusy(false);
    }
  };

  const handleCodeInput = (value: string) => {
    setCode(value.replace(/\D/g, "").slice(0, 6));
  };

  return (
    <div className="max-w-md mx-auto pt-24 space-y-6">
      <div className="text-center space-y-2">
        <h1 className="font-display text-3xl font-bold">Sign In</h1>
        <p className="text-gray-400 text-sm">No password needed — we&apos;ll email you a code</p>
      </div>

      {step === "email" ? (
        <form onSubmit={handleSendCode} className="border border-white/10 rounded-xl p-6 bg-white/5 space-y-4">
          {error && (
            <div className="bg-red-900/40 border border-red-700 text-red-300 text-sm rounded px-3 py-2">
              {error}
            </div>
          )}

          <div>
            <label htmlFor="email" className="block text-sm font-medium text-gray-300 mb-1">
              Email
            </label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="you@example.com"
              autoFocus
              className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm focus:outline-none focus:border-earl-500"
            />
          </div>

          <button
            type="submit"
            disabled={busy}
            className="w-full py-2 rounded-lg bg-earl-600 text-white font-semibold hover:bg-earl-500 disabled:opacity-50 transition"
          >
            {busy ? "Sending code…" : "Send Login Code"}
          </button>
        </form>
      ) : (
        <form onSubmit={handleVerifyCode} className="border border-white/10 rounded-xl p-6 bg-white/5 space-y-4">
          {message && (
            <div className="bg-green-900/40 border border-green-700 text-green-300 text-sm rounded px-3 py-2">
              {message}
            </div>
          )}
          {error && (
            <div className="bg-red-900/40 border border-red-700 text-red-300 text-sm rounded px-3 py-2">
              {error}
            </div>
          )}

          <p className="text-sm text-gray-400">
            We sent a code to <strong className="text-white">{email}</strong>
          </p>

          <div>
            <label htmlFor="code" className="block text-sm font-medium text-gray-300 mb-1">
              Login Code
            </label>
            <input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => handleCodeInput(e.target.value)}
              required
              placeholder=""
              className="w-full px-4 py-2 rounded-lg bg-black/50 border border-white/10 text-sm text-center text-2xl tracking-[0.5em] focus:outline-none focus:border-earl-500"
              autoFocus
            />
          </div>

          <button
            type="submit"
            disabled={busy || code.length !== 6}
            className="w-full py-2 rounded-lg bg-earl-600 text-white font-semibold hover:bg-earl-500 disabled:opacity-50 transition"
          >
            {busy ? "Verifying…" : "Verify & Sign In"}
          </button>

          <button
            type="button"
            onClick={() => {
              setStep("email");
              setError("");
              setMessage("");
              setCode("");
            }}
            className="w-full text-sm text-gray-400 hover:text-white transition-colors"
          >
            ← Use a different email
          </button>
        </form>
      )}

      <p className="text-xs text-gray-500 text-center leading-relaxed">
        By signing in, you agree to our{" "}
        <Link href="/terms" className="text-earl-400 hover:text-earl-300 underline">
          Terms &amp; Conditions
        </Link>{" "}
        and{" "}
        <Link href="/privacy" className="text-earl-400 hover:text-earl-300 underline">
          Privacy Policy
        </Link>
        .
      </p>
    </div>
  );
}
