"use client";

import { useState, type ReactNode } from "react";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "./LoginModal";

interface PremiumGateProps {
  children: ReactNode;
  /** Optional title for the gate card. Defaults to "Premium Content" */
  title?: string;
  /** Optional message. Auto-generated if omitted */
  message?: string;
}

export default function PremiumGate({ children, title, message }: PremiumGateProps) {
  const { user, loading } = useAuth();
  const [loginModalOpen, setLoginModalOpen] = useState(false);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16">
        <div className="w-6 h-6 border-2 border-gray-500 border-t-white rounded-full animate-spin" />
      </div>
    );
  }

  const isPremium = user?.subscription_tier === "premium" || user?.subscription_tier === "ultimate";

  if (!user || !isPremium) {
    return (
      <>
        <div className="flex items-center justify-center py-16 px-6">
          <div className="w-full max-w-sm bg-white/5 rounded-2xl p-8 border border-white/10 text-center">
            <h2 className="text-lg font-bold text-gray-100 mb-2">
              {title || "Premium Content"}
            </h2>
            <div className="w-10 h-0.5 bg-earl-600 mx-auto my-3 rounded-full" />
            <p className="text-gray-300 text-sm mb-6">
              {message || (
                user
                  ? "Upgrade to Premium to access Earl's Picks, Detailed Analysis, and Detailed Stats."
                  : "Sign in and upgrade to Premium to access Earl's Picks, Detailed Analysis, and Detailed Stats."
              )}
            </p>

            {user ? (
              <a
                href="/pricing"
                className="inline-block w-full py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
              >
                Upgrade to Premium
              </a>
            ) : (
              <button
                onClick={() => setLoginModalOpen(true)}
                className="w-full py-3 rounded-xl bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
              >
                Sign In to Get Started
              </button>
            )}
          </div>
        </div>

        {!user && (
          <LoginModal open={loginModalOpen} onClose={() => setLoginModalOpen(false)} />
        )}
      </>
    );
  }

  return <>{children}</>;
}
