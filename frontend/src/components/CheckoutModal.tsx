"use client";

import { useEffect, useState, useRef } from "react";

interface CheckoutModalProps {
  checkoutUrl: string;
  onClose: () => void;
  onComplete: () => void;
}

export default function CheckoutModal({
  checkoutUrl,
  onClose,
  onComplete,
}: CheckoutModalProps) {
  const [status, setStatus] = useState<"opening" | "waiting" | "complete" | "error">(
    "opening"
  );
  const popupRef = useRef<Window | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  useEffect(() => {
    // Open the Stripe checkout in a new centered popup window
    const width = 600;
    const height = 700;
    const left = Math.max(0, (window.screen.width - width) / 2);
    const top = Math.max(0, (window.screen.height - height) / 2);

    const popup = window.open(
      checkoutUrl,
      "stripe_checkout",
      `width=${width},height=${height},left=${left},top=${top},scrollbars=yes`
    );

    if (!popup || popup.closed) {
      setStatus("error");
      return;
    }

    popupRef.current = popup;
    setStatus("waiting");

    // Poll for the popup to close (user completed checkout or cancelled)
    pollRef.current = setInterval(() => {
      if (popup.closed) {
        clearInterval(pollRef.current);

        // Check if subscription was updated by checking URL params
        // (popup would have redirected to /profile?subscription=success)
        const params = new URLSearchParams(window.location.search);
        if (params.get("subscription") === "success") {
          setStatus("complete");
        } else {
          // Refresh subscription status from API
          checkSubscriptionStatus().then((isPremium) => {
            if (isPremium) {
              setStatus("complete");
            } else {
              // User might have cancelled or failed — just close
              onClose();
            }
          });
        }
      }
    }, 500);

    return () => {
      clearInterval(pollRef.current);
      if (popupRef.current && !popupRef.current.closed) {
        popupRef.current.close();
      }
    };
  }, [checkoutUrl]);

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="relative bg-neutral-900 rounded-2xl border border-neutral-700 shadow-2xl w-full max-w-md mx-4 p-8 text-center">
        {status === "opening" && (
          <>
            <div className="animate-spin rounded-full h-12 w-12 border-2 border-earl-400 border-t-transparent mx-auto mb-4" />
            <h3 className="text-lg font-semibold text-white mb-2">
              Opening secure checkout...
            </h3>
            <p className="text-gray-400 text-sm">
              A secure Stripe payment window will open shortly.
            </p>
          </>
        )}

        {status === "waiting" && (
          <>
            <div className="w-16 h-16 mx-auto mb-4 rounded-full bg-neutral-800 flex items-center justify-center">
              <svg className="w-8 h-8 text-earl-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
              </svg>
            </div>
            <h3 className="text-lg font-semibold text-white mb-2">
              Complete your payment
            </h3>
            <p className="text-gray-400 text-sm mb-6">
              Use the popup window to complete your secure payment with Stripe.
              <br />
              <span className="text-gray-500 text-xs">
                Don&apos;t see it? Check for popup blockers.
              </span>
            </p>
            <div className="flex gap-3 justify-center">
              <button
                onClick={() => {
                  // Re-open if popup was closed
                  if (popupRef.current?.closed) {
                    setStatus("opening");
                    const width = 600, height = 700;
                    const left = Math.max(0, (window.screen.width - width) / 2);
                    const top = Math.max(0, (window.screen.height - height) / 2);
                    popupRef.current = window.open(
                      checkoutUrl,
                      "stripe_checkout",
                      `width=${width},height=${height},left=${left},top=${top},scrollbars=yes`
                    );
                    if (popupRef.current && !popupRef.current.closed) {
                      setStatus("waiting");
                    }
                  } else {
                    popupRef.current?.focus();
                  }
                }}
                className="bg-earl-400 hover:bg-amber-400 text-black font-semibold px-6 py-2 rounded-lg text-sm transition"
              >
                Re-open window
              </button>
              <button
                onClick={onClose}
                className="text-gray-400 hover:text-white text-sm transition underline"
              >
                Cancel
              </button>
            </div>
          </>
        )}

        {status === "complete" && (
          <>
            <svg className="w-16 h-16 text-green-400 mx-auto mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <h3 className="text-xl font-bold text-white mb-2">
              Welcome to Premium! 🎉
            </h3>
            <p className="text-gray-400 text-sm mb-6">
              Your membership is now active. Enjoy the full Earl Knows Ball experience!
            </p>
            <button
              onClick={onComplete}
              className="bg-earl-400 hover:bg-amber-400 text-black font-semibold px-8 py-3 rounded-lg transition"
            >
              Go to Profile
            </button>
          </>
        )}

        {status === "error" && (
          <>
            <svg className="w-16 h-16 text-red-400 mx-auto mb-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
            </svg>
            <h3 className="text-lg font-semibold text-white mb-2">
              Popup was blocked
            </h3>
            <p className="text-gray-400 text-sm mb-6">
              Please allow popups for this site and try again, or click the button below to continue.
            </p>
            <button
              onClick={() => {
                // Fallback: redirect to Stripe directly
                window.location.href = checkoutUrl;
              }}
              className="bg-earl-400 hover:bg-amber-400 text-black font-semibold px-6 py-2 rounded-lg text-sm transition"
            >
              Continue in this tab
            </button>
          </>
        )}
      </div>
    </div>
  );
}

async function checkSubscriptionStatus(): Promise<boolean> {
  try {
    const token = localStorage.getItem("earl_token");
    if (!token) return false;
    const res = await fetch("/api/subscriptions/my", {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json();
    return data.has_active === true;
  } catch {
    return false;
  }
}
