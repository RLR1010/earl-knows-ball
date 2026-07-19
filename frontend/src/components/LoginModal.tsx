"use client";

import { useState, type FormEvent, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useAuth } from "@/lib/auth-context";

interface LoginModalProps {
  open: boolean;
  onClose: () => void;
}

export default function LoginModal({ open, onClose }: LoginModalProps) {
  const { sendCode, verifyCode } = useAuth();
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<"email" | "code">("email");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [mounted, setMounted] = useState(false);
  const codeInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setMounted(true);
  }, []);

  // Focus code input when step changes
  useEffect(() => {
    if (step === "code" && codeInputRef.current) {
      codeInputRef.current.focus();
    }
  }, [step]);

  // Reset when modal opens
  useEffect(() => {
    if (open) {
      setEmail("");
      setCode("");
      setStep("email");
      setError("");
      setBusy(false);
    }
  }, [open]);

  const handleSendCode = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      await sendCode(email);
      setStep("code");
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
      onClose();
    } catch (err: any) {
      setError(err?.message || "Invalid or expired code.");
    } finally {
      setBusy(false);
    }
  };

  const handleCodeInput = (value: string) => {
    // Only allow digits, max 6
    const digits = value.replace(/\D/g, "").slice(0, 6);
    setCode(digits);
  };

  const handleKeyDown = (e: KeyboardEvent) => {
    if (e.key === "Escape") onClose();
  };

  useEffect(() => {
    if (open) {
      document.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
    };
  }, [open]);

  if (!open || !mounted) return null;

  const modal = (
    <div
      className="fixed inset-0 z-[100] overflow-y-auto bg-black/60"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div className="flex flex-col items-center justify-start min-h-full px-4 pt-12 sm:pt-20 pb-8">
        <div
          className="w-full max-w-md"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="bg-gray-900 border border-gray-700 rounded-lg p-6 shadow-2xl">
            <div className="flex items-center justify-between mb-6">
              <h2 className="text-xl font-bold text-white">
                {step === "email" ? "Sign In" : "Check Your Email"}
              </h2>
              <button
                type="button"
                onClick={onClose}
                className="text-gray-400 hover:text-white"
              >
                <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            {step === "email" && (
              <form onSubmit={handleSendCode} className="space-y-4">
                <p className="text-sm text-gray-400">
                  Enter your email address and we&apos;ll send you a login code. No password needed.
                </p>

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
                    className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-white placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-green-500"
                    placeholder="you@example.com"
                    required
                    autoFocus
                  />
                </div>

                <button
                  type="submit"
                  disabled={busy}
                  className="w-full bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white font-semibold py-2 rounded transition-colors"
                >
                  {busy ? "Sending code…" : "Send Login Code"}
                </button>
              </form>
            )}

            {step === "code" && (
              <form onSubmit={handleVerifyCode} className="space-y-4">
                <p className="text-sm text-gray-400">
                  We sent a 6-digit code to <strong className="text-white">{email}</strong>.
                </p>

                {error && (
                  <div className="bg-red-900/40 border border-red-700 text-red-300 text-sm rounded px-3 py-2">
                    {error}
                  </div>
                )}

                <div>
                  <label htmlFor="code" className="block text-sm font-medium text-gray-300 mb-1">
                    Login Code
                  </label>
                  <input
                    ref={codeInputRef}
                    id="code"
                    type="text"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    value={code}
                    onChange={(e) => handleCodeInput(e.target.value)}
                    className="w-full bg-gray-800 border border-gray-600 rounded px-3 py-2 text-white text-center text-2xl tracking-[0.5em] placeholder-gray-500 focus:outline-none focus:ring-2 focus:ring-green-500"
                    placeholder="••••••"
                    required
                  />
                </div>

                <button
                  type="submit"
                  disabled={busy || code.length !== 6}
                  className="w-full bg-green-600 hover:bg-green-500 disabled:opacity-50 text-white font-semibold py-2 rounded transition-colors"
                >
                  {busy ? "Verifying…" : "Verify & Sign In"}
                </button>

                <button
                  type="button"
                  onClick={() => {
                    setStep("email");
                    setError("");
                    setCode("");
                  }}
                  className="w-full text-sm text-gray-400 hover:text-white transition-colors"
                >
                  ← Use a different email
                </button>
              </form>
            )}
          </div>
        </div>
      </div>
    </div>
  );

  return createPortal(modal, document.body);
}
