import UpcomingGames from "@/components/UpcomingGames";

export default function Home() {
  return (
    <div className="space-y-16">
      {/* ── Hero: Portrait | Screenshot | Bullet points ──────────── */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-8 items-center py-12">
        {/* Left: portrait */}
        <div className="flex justify-center lg:justify-end">
          <img
            src="/earl-portrait.png"
            alt="Earl Knows Ball"
            className="w-56 md:w-64 h-auto object-contain"
          />
        </div>

        {/* Center: home page screenshot */}
        <div className="flex justify-center">
          <div className="relative w-full max-w-sm rounded-xl overflow-hidden border border-white/10 shadow-2xl shadow-earl-500/5">
            <img
              src="/earl-home-page.png"
              alt="Earl Knows Ball home page preview"
              className="w-full h-auto"
            />
          </div>
        </div>

        {/* Right: bullet points */}
        <div className="space-y-5 lg:pl-4">
          <ul className="space-y-5">
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">AI Powered Handicapping</span>
            </li>
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">Chat with Earl About Your Bets</span>
            </li>
            <li className="flex items-start gap-4">
              <span className="mt-1 shrink-0 w-6 h-6 rounded-full bg-earl-600 flex items-center justify-center">
                <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={3} d="M5 13l4 4L19 7" />
                </svg>
              </span>
              <span className="text-gray-300 font-medium text-xl">Get Picks and Recommendations</span>
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

      {/* Upcoming games across all sports */}
      <UpcomingGames />
    </div>
  );
}
