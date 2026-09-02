"use client";

import { useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "./LoginModal";
import UserMenu from "./UserMenu";

export default function HeaderUserArea() {
  const { user, loading } = useAuth();
  const [loginOpen, setLoginOpen] = useState(false);
  const router = useRouter();
  const pathname = usePathname();

  if (loading) {
    return <div className="w-6 h-6" />; // placeholder to prevent layout shift
  }

  if (user) {
    return <UserMenu />;
  }

  // On the /premium page itself, take the customer straight to checkout
  // instead of bouncing them back to the /premium page they're already on.
  const onPremiumPage = pathname === "/premium";
  const premiumHref = onPremiumPage ? "/checkout?plan=premium-trial" : "/premium";

  return (
    <>
      <a
        href={premiumHref}
        className="mr-2 hidden sm:inline-block rounded-md bg-emerald-500 px-3 py-1.5 text-sm font-semibold text-white transition hover:bg-emerald-400"
      >
        Get Premium · $1.95 trial
      </a>
      <button
        onClick={() => setLoginOpen(true)}
        className="text-gray-300 hover:text-white transition-colors p-1 rounded-full hover:bg-gray-700"
        aria-label="Sign In"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          className="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
          />
        </svg>
      </button>
      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
    </>
  );
}
