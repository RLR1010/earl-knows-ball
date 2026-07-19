"use client";

import { useEffect, useRef, useState } from "react";

declare global {
  interface Window {
    Stripe: (key: string) => any;
  }
}

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
  const checkoutRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "complete" | "error">(
    "loading"
  );
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let destroyed = false;
    let checkoutInstance: any = null;

    async function init() {
      const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
      if (!key) {
        setError("Stripe is not configured");
        return;
      }

      // Wait for Stripe.js to load from CDN
      if (!window.Stripe) {
        await new Promise<void>((resolve, reject) => {
          const script = document.createElement("script");
          script.src = "https://js.stripe.com/v3/";
          script.onload = () => resolve();
          script.onerror = () => reject(new Error("Failed to load Stripe.js"));
          document.head.appendChild(script);
        });
      }

      if (destroyed) return;

      try {
        const stripe = window.Stripe(key);
        checkoutInstance = await stripe.initEmbeddedCheckout({
          clientSecret,
          onComplete: () => {
            if (!destroyed) {
              setStatus("complete");
              setTimeout(() => {
                window.location.href = "/profile?subscription=success";
              }, 2000);
            }
          },
        });

        if (!destroyed && checkoutRef.current) {
          checkoutInstance.mount(checkoutRef.current);
          setStatus("ready");
        }
      } catch (err: any) {
        if (!destroyed) {
          setError(err.message || "Failed to load checkout");
          setStatus("error");
        }
      }
    }

    init();

    return () => {
      destroyed = true;
      if (checkoutInstance) {
        try {
          checkoutInstance.destroy();
        } catch {}
      }
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
            <div ref={checkoutRef} className="min-h-[450px]" />
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
                onClick={() => {
                  window.location.href = "/profile?subscription=success";
                }}
                className="bg-earl-400 hover:bg-amber-400 text-black font-semibold px-8 py-3 rounded-lg transition"
              >
                Go to Profile
              </button>
            </div>
          )}

          {status === "error" && (
            <div className="flex flex-col items-center justify-center py-16">
              <svg className="w-16 h-16 text-red-400 mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
              </svg>
              <p className="text-red-400 mb-4">{error || "Failed to load checkout"}</p>
              <p className="text-gray-500 text-sm mb-4">
                Try the hosted checkout instead.
              </p>
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
