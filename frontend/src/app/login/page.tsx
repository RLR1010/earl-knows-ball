"use client";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { useAuth } from "@/lib/auth-context";

export default function LoginPage() {
  const router = useRouter();
  const { sendCode, verifyCode } = useAuth();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

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
      router.push("/chat");
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
    </div>
  );
}
