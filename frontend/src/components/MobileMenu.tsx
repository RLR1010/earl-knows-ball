"use client";

import { useState, useEffect, useRef } from "react";
import { useAuth } from "@/lib/auth-context";
import LoginModal from "./LoginModal";
import Link from "next/link";
import { usePathname } from "next/navigation";

type Sport = "nfl" | "nba" | "mlb";

const SPORTS: { key: Sport; label: string; emoji: string }[] = [
  { key: "nfl", label: "NFL", emoji: "🏈" },
  { key: "nba", label: "NBA", emoji: "🏀" },
  { key: "mlb", label: "MLB", emoji: "⚾" },
];

export default function MobileMenu() {
  const { user, loading, logout } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [loginOpen, setLoginOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);
  const pathname = usePathname();

  // Close menu on outside click
  useEffect(() => {
    const handleClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    if (menuOpen) document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [menuOpen]);

  // Close menu on route change
  useEffect(() => {
    setMenuOpen(false);
  }, [pathname]);

  // Prevent body scroll when menu is open
  useEffect(() => {
    if (menuOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [menuOpen]);

  return (
    <div className="relative" ref={menuRef}>
      {/* Hamburger toggle button */}
      <button
        onClick={() => setMenuOpen(!menuOpen)}
        className="text-gray-300 hover:text-white transition-colors p-2 rounded-md hover:bg-white/10"
        aria-label={menuOpen ? "Close menu" : "Open menu"}
      >
        {menuOpen ? (
          <svg xmlns="http://www.w3.org/2000/svg" className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        ) : (
          <svg xmlns="http://www.w3.org/2000/svg" className="h-6 w-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        )}
      </button>

      {/* Overlay */}
      {menuOpen && (
        <div
          className="fixed inset-0 bg-black/60 z-40 md:hidden"
          onClick={() => setMenuOpen(false)}
        />
      )}

      {/* Slide-in panel */}
      <div
        className={`fixed top-0 right-0 h-full w-72 max-w-[80vw] bg-gray-900 border-l border-gray-700 z-50 transform transition-transform duration-200 ease-in-out md:hidden ${
          menuOpen ? "translate-x-0" : "translate-x-full"
        }`}
      >
        <div className="flex flex-col h-full py-6">
          {/* Header */}
          <div className="flex items-center justify-between px-5 pb-4 border-b border-gray-700">
            <span className="text-sm font-semibold text-gray-400 uppercase tracking-wider">Menu</span>
            <button
              onClick={() => setMenuOpen(false)}
              className="text-gray-400 hover:text-white transition-colors p-1 rounded-md hover:bg-white/10"
              aria-label="Close menu"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>

          {/* Auth section — always at top */}
          <div className="px-5 py-4 border-b border-gray-700/60">
            {loading ? (
              <div className="h-10 flex items-center justify-center">
                <div className="w-5 h-5 border-2 border-gray-500 border-t-white rounded-full animate-spin" />
              </div>
            ) : user ? (
              <div className="space-y-1">
                <p className="text-sm text-gray-400 truncate mb-2">{user.display_name || user.email}</p>
                <Link
                  href="/profile"
                  onClick={() => setMenuOpen(false)}
                  className="flex items-center gap-3 w-full px-4 py-2.5 rounded-lg text-sm text-gray-200 hover:bg-white/10 transition-colors"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                  </svg>
                  Profile
                </Link>
                <button
                  onClick={() => {
                    logout();
                    setMenuOpen(false);
                  }}
                  className="flex items-center gap-3 w-full px-4 py-2.5 rounded-lg text-sm text-gray-200 hover:bg-white/10 transition-colors"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                  </svg>
                  Log Out
                </button>
              </div>
            ) : (
              <button
                onClick={() => {
                  setMenuOpen(false);
                  setLoginOpen(true);
                }}
                className="flex items-center gap-3 w-full px-4 py-2.5 rounded-lg text-sm font-semibold bg-earl-600 text-white hover:bg-earl-500 transition-colors"
              >
                <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                </svg>
                Sign In
              </button>
            )}
          </div>

          {/* Sport links */}
          <div className="flex-1 px-5 pt-4 space-y-1 overflow-y-auto">
            <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 px-1">Categories</p>
            {SPORTS.map((sport) => {
              const isActive = pathname.startsWith(`/${sport.key}`);
              return (
                <Link
                  key={sport.key}
                  href={`/${sport.key}`}
                  onClick={() => setMenuOpen(false)}
                  className={`flex items-center gap-3 w-full px-4 py-3 rounded-lg text-sm font-semibold transition-colors ${
                    isActive
                      ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
                      : "text-gray-300 hover:bg-white/5 hover:text-white"
                  }`}
                >
                  <span className="text-lg">{sport.emoji}</span>
                  {sport.label}
                </Link>
              );
            })}
          </div>
        </div>
      </div>

      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
    </div>
  );
}
