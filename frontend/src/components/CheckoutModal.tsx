"use client";

import { useEffect, useRef, useState } from "react";
import { loadStripe, Stripe } from "@stripe/stripe-js";

interface CheckoutModalProps {
  clientSecret: string;
  onClose: () => void;
  onComplete: () => void;
}

export default function CheckoutModal({
  clientSecret,
  onClose,
  onComplete,
}: CheckoutModalProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const checkoutRef = useRef<any>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "complete" | "error">(
    "loading"
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;

    async function mountCheckout() {
      const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
      if (!key) {
        setError("Stripe is not configured");
        return;
      }

      const stripe = await loadStripe(key);
      if (!stripe || !containerRef.current || !mounted) return;

      // Create the embedded checkout page
      checkoutRef.current = await stripe.createEmbeddedCheckoutPage({
        clientSecret,
        onComplete: () => {
          if (mounted) {
            // Unmount the checkout before showing success
            try {
              checkoutRef.current?.unmount();
            } catch {}
            setStatus("complete");
          }
        },
      });

      // Mount it into the container
      checkoutRef.current.mount(containerRef.current);
      if (mounted) setStatus("ready");
    }

    mountCheckout();

    return () => {
      mounted = false;
      try {
        checkoutRef.current?.destroy();
      } catch {}
    };
  }, [clientSecret]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="relative bg-neutral-900 rounded-2xl border border-neutral-700 shadow-2xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-neutral-700">
          <h2 className="text-lg font-semibold text-white">Premium Checkout</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white transition p-1"
          >
            <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="p-6 overflow-y-auto max-h-[calc(90vh-64px)]">
          {status === "loading" && (
            <div className="flex flex-col items-center justify-center py-16">
              <div className="animate-spin rounded-full h-10 w-10 border-2 border-earl-400 border-t-transparent mb-4" />
              <p className="text-gray-400 text-sm">Loading secure checkout...</p>
            </div>
          )}

          {status === "ready" && (
            <div ref={containerRef} className="min-h-[450px]" />
          )}

          {status === "complete" && (
            <div className="flex flex-col items-center justify-center py-16">
              <svg className="w-16 h-16 text-green-400 mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <h3 className="text-xl font-bold text-white mb-2">
                Welcome to Premium! 🎉
              </h3>
              <p className="text-gray-400 text-sm mb-6 text-center">
                Your membership is now active. Enjoy the full Earl Knows Ball experience!
              </p>
              <button
                onClick={onComplete}
                className="bg-earl-400 hover:bg-amber-400 text-black font-semibold px-8 py-3 rounded-lg transition"
              >
                Go to Profile
              </button>
            </div>
          )}

          {status === "error" && (
            <div className="flex flex-col items-center justify-center py-16">
              <p className="text-red-400 mb-4">{error}</p>
              <button
                onClick={onClose}
                className="bg-neutral-800 hover:bg-neutral-700 text-white px-6 py-2 rounded-lg transition"
              >
                Close
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
