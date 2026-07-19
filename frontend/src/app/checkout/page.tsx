"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

declare global {
  interface Window {
    Stripe: (key: string) => any;
  }
}

function CheckoutForm() {
  const searchParams = useSearchParams();
  const planId = searchParams.get("plan") || "monthly";
  const checkoutRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let destroyed = false;

    async function init() {
      const key = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
      if (!key) {
        setError("Stripe is not configured");
        return;
      }

      const token = localStorage.getItem("earl_token");
      if (!token) {
        window.location.href = `/auth?redirect=/checkout?plan=${planId}`;
        return;
      }

      // Create the checkout session
      const res = await fetch("/api/subscriptions/checkout", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          plan_id: planId,
          success_url: `${window.location.origin}/profile?subscription=success`,
          cancel_url: `${window.location.origin}/pricing`,
          ui_mode: "embedded_page",
        }),
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "Checkout failed");
      }

      const data = await res.json();
      if (!data.client_secret) {
        throw new Error("No client_secret returned");
      }

      // Load Stripe.js from CDN
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

      // Initialize Embedded Checkout — Stripe handles ALL the UI
      const stripe = window.Stripe(key);
      const checkout = await stripe.initEmbeddedCheckout({
        clientSecret: data.client_secret,
      });

      if (!destroyed && checkoutRef.current) {
        checkout.mount(checkoutRef.current);
      }
    }

    init().catch((err: any) => {
      if (!destroyed) setError(err.message || "Something went wrong");
    });

    return () => {
      destroyed = true;
    };
  }, [planId]);

  if (error) {
    return (
      <div className="min-h-screen bg-neutral-950 flex items-center justify-center">
        <div className="text-center max-w-md mx-auto p-8">
          <p className="text-red-400 mb-4">{error}</p>
          <a href="/pricing" className="text-earl-400 hover:underline">
            ← Back to pricing
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-neutral-950 flex items-start justify-center py-8">
      <div className="w-full max-w-2xl mx-auto px-4">
        <div ref={checkoutRef} />
      </div>
    </div>
  );
}

export default function CheckoutPage() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-neutral-950" />}>
      <CheckoutForm />
    </Suspense>
  );
}
