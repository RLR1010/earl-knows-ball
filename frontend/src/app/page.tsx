import type { Metadata } from "next";
import UpcomingGames from "@/components/UpcomingGames";
import BestBetsPanel from "@/components/BestBetsPanel";
import SiteEditorialSection from "@/components/SiteEditorialSection";
import JsonLd from "@/components/JsonLd";
import { websiteStructuredData } from "@/lib/structured-data";

export const metadata: Metadata = {
  title: "Earl Knows Ball — AI-Powered Sports Handicapping & Picks",
  description:
    "Earl Knows Ball is the ultimate AI sports handicapping tool for NFL, MLB, and NBA. Get picks with probabilities, betting lines, trends, and a chat handicapper that explains every call.",
  keywords: [
    "sports betting picks",
    "AI handicapper",
    "NFL predictions",
    "MLB predictions",
    "NBA predictions",
    "betting odds and spreads",
  ],
  openGraph: {
    title: "Earl Knows Ball — AI-Powered Sports Handicapping & Picks",
    description: "The ultimate AI sports handicapping tool for NFL, MLB, and NBA.",
    url: "https://earlknowsball.com/",
    siteName: "Earl Knows Ball",
    type: "website",
  },
};

export default function Home() {
  return (
    <div className="space-y-16">
      <JsonLd data={websiteStructuredData()} />
      {/* ── Hero: Portrait | Screenshot | Bullet points ──────────── */}
      <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-[1fr_auto_1fr] gap-6 lg:gap-8 items-center py-12">
        {/* Merged graphic — shown only where the three columns won't fit (below lg) */}
        <div className="flex justify-center md:justify-end lg:hidden">
          <img
            src="/earl-merged.png"
            alt="Earl Knows Ball"
            className="w-64 md:w-64 h-auto object-contain"
          />
        </div>

        {/* Left: portrait (lg+ only) */}
        <div className="hidden lg:flex justify-center lg:justify-end">
          <img
            src="/earl-portrait.png"
            alt="Earl Knows Ball"
            className="w-56 md:w-64 h-auto object-contain"
          />
        </div>

        {/* Center: home page screenshot (lg+ only) */}
        <div className="hidden lg:flex justify-center">
          <div className="relative w-auto max-w-sm rounded-xl overflow-hidden shadow-2xl shadow-earl-500/5">
            <img
              src="/earl-home-page.png"
              alt="Earl Knows Ball home page preview"
              className="w-full h-auto"
            />
          </div>
        </div>

        {/* Right: bullet points */}
        <div className="space-y-5 px-6 md:px-0 lg:pl-0">
          <ul className="space-y-5">
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">AI Powered Handicapping For NFL, MLB, and NBA</span>
            </li>
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">Chat with Earl About Games, Players, and Picks</span>
            </li>
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">Get Picks, Analysis, and Recommendations</span>
            </li>
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">All the Statistics You Could Ever Want</span>
            </li>
          </ul>
        </div>
      </section>

      {/* Site-wide "All" editorial articles */}
      <SiteEditorialSection />

      {/* Earl's Best Bets across all sports (single highest-value pick per game) */}
      <BestBetsPanel
        sport="all"
        showSport
        limit={6}
        containerClassName="max-w-6xl mx-auto px-4"
        title="Earl's Best Bets"
        subtitle="One value pick per upcoming game, ranked by edge (model confidence vs. implied odds)."
      />

      {/* Upcoming games across all sports */}
      <UpcomingGames />
    </div>
  );
}
