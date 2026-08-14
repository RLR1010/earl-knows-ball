"use client";

import { usePathname } from "next/navigation";
import { AuthProvider } from "@/lib/auth-context";
import HeaderUserArea from "@/components/HeaderUserArea";
import MobileMenu from "@/components/MobileMenu";
import Footer from "@/components/Footer";
import { Sparkles } from "lucide-react";

type Sport = "nfl" | "nba" | "mlb";

const SPORTS: { key: Sport; label: string; emoji: string }[] = [
  { key: "nfl", label: "NFL", emoji: "🏈" },
  { key: "nba", label: "NBA", emoji: "🏀" },
  { key: "mlb", label: "MLB", emoji: "⚾" },
];

const SUB_NAV_ITEMS = [
  { label: "Home", path: "" },
  { label: "Schedule", path: "/schedule" },
  { label: "Analysis", path: "/analysis" },
  { label: "Teams", path: "/teams" },
  { label: "Props", path: "/props" },
  { label: "Articles", path: "/articles" },
  { label: "Results", path: "/results" },
  { label: "Chat With Earl", path: "/chat" },
];

function getActiveSport(pathname: string): Sport | null {
  const parts = pathname.split("/").filter(Boolean);
  const valid = SPORTS.map((s) => s.key);
  return parts.length > 0 && valid.includes(parts[0] as Sport)
    ? (parts[0] as Sport)
    : null;
}

/** Extract the sub-path within a sport route, e.g. "/nfl/stats" → "/stats" */
function getSportSubPath(pathname: string, sport: Sport): string {
  const prefix = `/${sport}`;
  if (!pathname.startsWith(prefix)) return "/";
  return pathname.slice(prefix.length) || "/";
}

export default function SiteChrome({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const activeSport = getActiveSport(pathname);

  const hideSportChrome =
    ["/login", "/register", "/profile"].some((p) => pathname.startsWith(p));

  // The sport sub-nav (Home/Schedule/Analysis/...) is hidden on the standalone
  // chat page (which has its own left-nav), but the sport pills and login/profile
  // icon STAY in the header there on non-mobile.
  const hideSportSubNav =
    hideSportChrome || pathname.endsWith("/chat");
  // The chat page manages its own width/padding/height (it's an app-style layout),
  // so <main> should not add site padding there even though the header shows.
  const paddedMain = activeSport && !hideSportChrome && !pathname.endsWith("/chat");

  return (
    <>
      {/* ⚠️ IMPORTANT: Keep this header EXACTLY as it appears for the site chrome.
          It renders the earl-logo.png and the sport-selector pills. */}
      <AuthProvider>
        <header className="sticky top-0 z-50">
          {/* ── Top Nav ──────────────────────────────────────────── */}
          <div className="border-b border-white/10 bg-black/50 backdrop-blur-sm relative z-10">
            <div className="max-w-7xl mx-auto px-4 h-20 flex items-center justify-between">
              {/* Logo */}
              <a href="/" className="shrink-0">
                <img src="/earl-logo.png" alt="Earl Knows Ball" className="h-6 sm:h-8 w-auto" />
              </a>

              {/* Sport selector pills — hidden on mobile */}
              {!hideSportChrome && (
              <div className="hidden md:flex items-center gap-1 bg-white/[0.04] rounded-full p-0.5 border border-white/10">
                {SPORTS.map((sport) => {
                  const isActive = activeSport === sport.key;
                  return (
                    <a
                      key={sport.key}
                      href={`/${sport.key}`}
                      className={`px-4 py-1.5 rounded-full text-xs font-bold uppercase tracking-wider transition flex items-center gap-1.5 ${
                        isActive
                          ? "bg-earl-600 text-white shadow-lg shadow-earl-600/25"
                          : "text-gray-400 hover:text-white hover:bg-white/10"
                      }`}
                    >
                      <span>{sport.emoji}</span>
                      {sport.label}
                    </a>
                  );
                })}
              </div>
              )}

              {/* Right side — hidden on mobile */}
              {!hideSportChrome && (
              <div className="hidden md:flex items-center gap-4">
                <HeaderUserArea />
              </div>
              )}

              {/* Mobile hamburger */}
              <div className="flex md:hidden items-center">
                <MobileMenu />
              </div>
            </div>
          </div>

          {/* ── Sport Sub-Nav ───────────────────────────────────── */}
          {activeSport && !hideSportSubNav && (
            <div className="border-b border-white/10 bg-black/40 backdrop-blur-sm">
              <div className="max-w-7xl mx-auto px-4 flex items-center">
                {/* Scrollable main items (Chat With Earl is pinned separately) */}
                <div className="relative flex-1 min-w-0">
                  <div className="flex items-center overflow-x-auto no-scrollbar">
                    {SUB_NAV_ITEMS.filter((item) => item.label !== "Chat With Earl").map((item) => {
                      const href = item.path ? `/${activeSport}${item.path}` : `/${activeSport}`;
                      const currentSubPath = getSportSubPath(pathname, activeSport);
                      const isActive =
                        item.path === ""
                          ? currentSubPath === "/"
                          : currentSubPath.startsWith(item.path);

                      const displayLabel =
                        item.label === "Home" && activeSport ? activeSport.toUpperCase() : item.label;

                      return (
                        <a
                          key={item.label}
                          href={href}
                          className={`flex items-center gap-1.5 whitespace-nowrap px-4 py-2.5 text-xs font-semibold tracking-wide uppercase transition border-b-2 ${
                            isActive
                              ? "text-earl-400 border-earl-400"
                              : "text-gray-500 border-transparent hover:text-gray-300 hover:border-gray-600"
                          }`}
                        >
                          {displayLabel}
                        </a>
                      );
                    })}
                  </div>
                  {/* Right fade → hints more items can scroll into view */}
                  <div className="pointer-events-none absolute inset-y-0 right-0 w-6 bg-gradient-to-l from-black/40 to-transparent" />
                </div>

                {/* Pinned “Chat With Earl” CTA — always visible, never cut off */}
                <a
                  href={`/${activeSport}/chat`}
                  className={`group shrink-0 ml-2 pl-4 flex items-center gap-2 border-l border-white/10 py-2.5 text-xs font-bold tracking-wide uppercase transition ${
                    getSportSubPath(pathname, activeSport).startsWith("/chat")
                      ? "text-earl-300"
                      : "text-earl-400 hover:text-earl-300"
                  }`}
                >
                  <span className="flex items-center gap-1.5 rounded-full bg-gradient-to-r from-earl-600 to-earl-500/80 border border-earl-500/40 px-3 py-1.5 text-gray-300 group-hover:text-white shadow-lg shadow-earl-600/20 group-hover:shadow-earl-600/35">
                    <Sparkles className="h-4 w-4" />
                    <span className="hidden sm:inline">Chat With Earl</span>
                    <span className="sm:hidden">Earl</span>
                  </span>
                </a>
              </div>
            </div>
          )}
        </header>

        {/* ── Main Content ───────────────────────────────────────── */}
        <main className={`flex-1 w-full ${paddedMain ? "max-w-7xl mx-auto px-2 sm:px-4 py-6" : ""}`}>
          {children}
        </main>
        <Footer />
      </AuthProvider>
    </>
  );
}
