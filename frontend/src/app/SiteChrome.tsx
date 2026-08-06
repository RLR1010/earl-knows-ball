"use client";

import { usePathname } from "next/navigation";
import { AuthProvider } from "@/lib/auth-context";
import HeaderUserArea from "@/components/HeaderUserArea";
import MobileMenu from "@/components/MobileMenu";
import Footer from "@/components/Footer";

type Sport = "nfl" | "nba" | "mlb";

const SPORTS: { key: Sport; label: string; emoji: string }[] = [
  { key: "nfl", label: "NFL", emoji: "🏈" },
  { key: "nba", label: "NBA", emoji: "🏀" },
  { key: "mlb", label: "MLB", emoji: "⚾" },
];

const SUB_NAV_ITEMS = [
  { label: "Home", path: "" },
  { label: "Schedule", path: "/schedule" },
  { label: "Teams", path: "/teams" },
  { label: "Props", path: "/props" },
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
    ["/login", "/register", "/profile"].some((p) => pathname.startsWith(p)) ||
    pathname.endsWith("/chat");

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
          {activeSport && !hideSportChrome && (
            <div className="border-b border-white/10 bg-black/40 backdrop-blur-sm">
              <div className="max-w-7xl mx-auto px-4">
                <div className="flex items-center overflow-x-auto">
                  {SUB_NAV_ITEMS.map((item) => {
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
                        className={`px-5 py-2.5 text-xs font-semibold tracking-wide uppercase transition border-b-2 ${
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
              </div>
            </div>
          )}
        </header>

        {/* ── Main Content ───────────────────────────────────────── */}
        <main className={`flex-1 w-full ${activeSport && !hideSportChrome ? "max-w-7xl mx-auto px-4 py-6" : ""}`}>
          {children}
        </main>
        <Footer />
      </AuthProvider>
    </>
  );
}
