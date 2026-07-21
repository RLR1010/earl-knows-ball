"use client";

import { useState, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
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
  const [mounted, setMounted] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    setMounted(true);
  }, []);

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

  // Close on Escape key
  useEffect(() => {
    if (!menuOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [menuOpen]);

  const linkClass = (isActive: boolean) =>
    `flex items-center gap-3 w-full px-4 py-2.5 rounded-lg text-sm font-semibold transition-colors ${
      isActive
        ? "bg-earl-600/20 text-earl-400 border border-earl-600/30"
        : "text-gray-300 hover:bg-white/5 hover:text-white"
    }`;

  return (
    <>
      {/* Hamburger toggle button (renders in-place in the header) */}
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

      {/* Portal to document.body — always mounted, just toggles visibility */}
      {mounted && createPortal(
        <div
          className={`md:hidden fixed inset-0 z-50 transition-opacity duration-200 ${
            menuOpen ? "pointer-events-auto opacity-100" : "pointer-events-none opacity-0"
          }`}
        >
          {/* Overlay */}
          <div
            className="absolute inset-0 bg-black/60"
            onClick={() => setMenuOpen(false)}
          />
          {/* Slide-in panel */}
          <div
            className={`absolute top-0 right-0 h-full w-64 max-w-[80vw] bg-[#111827] border-l border-gray-700 shadow-2xl transition-transform duration-200 ease-in-out ${
              menuOpen ? "translate-x-0" : "translate-x-full"
            }`}
          >
            <div className="flex flex-col h-full py-4">
              {/* Auth section */}
              <div className="px-4 pb-3 border-b border-gray-700/60">
                {loading ? (
                  <div className="h-10 flex items-center justify-center">
                    <div className="w-5 h-5 border-2 border-gray-500 border-t-white rounded-full animate-spin" />
                  </div>
                ) : user ? (
                  <>
                    <p className="text-sm text-gray-400 truncate mb-1 px-1">{user.display_name || user.email}</p>
                    <Link
                      href="/profile"
                      onClick={() => setMenuOpen(false)}
                      className={linkClass(pathname === "/profile")}
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                      </svg>
                      Profile
                    </Link>
                    <button
                      onClick={() => {
                        logout();
                        setMenuOpen(false);
                      }}
                      className="flex items-center gap-3 w-full px-4 py-2.5 rounded-lg text-sm font-semibold text-gray-300 hover:bg-white/5 hover:text-white transition-colors"
                    >
                      <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                      </svg>
                      Log Out
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => {
                      setMenuOpen(false);
                      setLoginOpen(true);
                    }}
                    className={linkClass(false)}
                  >
                    <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
                    </svg>
                    Sign In
                  </button>
                )}
              </div>

              {/* Sport links */}
              <div className="px-4 pt-3 space-y-1">
                {SPORTS.map((sport) => {
                  const isActive = pathname.startsWith(`/${sport.key}`);
                  return (
                    <Link
                      key={sport.key}
                      href={`/${sport.key}`}
                      onClick={() => setMenuOpen(false)}
                      className={linkClass(isActive)}
                    >
                      <span className="text-base shrink-0">{sport.emoji}</span>
                      {sport.label}
                    </Link>
                  );
                })}
              </div>

              {/* Chat link — below sport links */}
              <div className="px-4 pt-2 border-t border-gray-700/40 mt-3">
                <Link
                  href={`/${
                    pathname.startsWith("/nba") ? "nba" : pathname.startsWith("/mlb") ? "mlb" : "nfl"
                  }/chat`}
                  onClick={() => setMenuOpen(false)}
                  className={linkClass(pathname.includes("/chat"))}
                >
                  <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
                  </svg>
                  Chat With Earl
                </Link>
              </div>
            </div>
          </div>
        </div>,
        document.body
      )}

      <LoginModal open={loginOpen} onClose={() => setLoginOpen(false)} />
    </>
  );
}
