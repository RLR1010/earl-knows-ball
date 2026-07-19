"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function CheckoutSuccessPage() {
  const router = useRouter();

  useEffect(() => {
    // Signal the parent window that checkout was successful
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage({ type: "stripe_checkout_complete" }, "*");
    }

    // Try to close this popup window after a brief pause
    const timeout = setTimeout(() => {
      window.close();
    }, 2000);

    // If window.close() doesn't work (some browsers block it on redirect),
    // show a fallback
    return () => clearTimeout(timeout);
  }, []);

  return (
    <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
      <div className="text-center max-w-md mx-auto p-8">
        <svg className="w-20 h-20 text-green-400 mx-auto mb-6" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
        <h1 className="text-2xl font-bold text-white mb-3">
          Payment Successful! 🎉
        </h1>
        <p className="text-gray-400 mb-6">
          Welcome to Earl Knows Ball Premium!
        </p>
        <p className="text-gray-500 text-sm">
          This window will close automatically.
          <br />
          If it doesn&apos;t, you can close it manually and refresh the pricing page.
        </p>
      </div>
    </div>
  );
}
