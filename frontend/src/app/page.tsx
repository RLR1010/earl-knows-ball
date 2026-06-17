import Link from "next/link";

export default function Home() {
  return (
    <div className="space-y-16">
      {/* Hero */}
      <section className="text-center py-16 space-y-6">
        <h1 className="font-display text-5xl md:text-7xl font-bold tracking-tight">
          <span className="text-white">EARL</span>{" "}
          <span className="text-earl-400">KNOWS</span>{" "}
          <span className="text-white">BALL</span>
        </h1>
        <p className="text-lg text-gray-400 max-w-2xl mx-auto">
          Game handicapping, DFS lineup building, and AI-powered analysis
          — all backed by real data across NFL, NBA, and MLB.
        </p>
        <div className="flex gap-4 justify-center pt-4">
          <Link
            href="/chat"
            className="px-6 py-3 rounded-full bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
          >
            Ask Earl
          </Link>
          <Link
            href="/teams"
            className="px-6 py-3 rounded-full border border-white/20 text-gray-300 font-semibold hover:bg-white/5 transition"
          >
            Browse Teams
          </Link>
        </div>
      </section>

      {/* Feature cards */}
      <section className="grid md:grid-cols-3 gap-6">
        <FeatureCard
          title="Handicapping"
          desc="Spread picks, over/under analysis, moneyline predictions, and situation-based modeling with detailed reasoning."
          icon="🎲"
        />
        <FeatureCard
          title="DFS Optimizer"
          desc="DraftKings and FanDuel lineup building with salary optimization, game stacks, and leverage plays."
          icon="💰"
        />
        <FeatureCard
          title="AI Chat"
          desc="Ask Earl about teams, players, matchups, bets, or DFS across NFL, NBA, and MLB. Backed by real data."
          icon="🧠"
        />
      </section>

      {/* Sport Cards */}
      <section className="grid md:grid-cols-3 gap-6">
        <div className="border border-white/10 rounded-2xl p-6 bg-white/5 space-y-2">
          <h3 className="font-display text-xl font-bold">🏈 NFL</h3>
          <p className="text-sm text-gray-400">Full handicapping engine, DFS salaries, 389k+ articles, and AI chat. Active.</p>
        </div>
        <div className="border border-white/10 rounded-2xl p-6 bg-white/5 space-y-2">
          <h3 className="font-display text-xl font-bold">🏀 NBA</h3>
          <p className="text-sm text-gray-400">30 teams seeded, AI chat active. Handicapping and DFS coming soon.</p>
        </div>
        <div className="border border-white/10 rounded-2xl p-6 bg-white/5 space-y-2">
          <h3 className="font-display text-xl font-bold">⚾ MLB</h3>
          <p className="text-sm text-gray-400">30 teams seeded, AI chat active. Handicapping and DFS coming soon.</p>
        </div>
      </section>
    </div>
  );
}

function FeatureCard({ title, desc, icon }: { title: string; desc: string; icon: string }) {
  return (
    <div className="border border-white/10 rounded-2xl p-6 bg-white/5 hover:bg-white/10 transition space-y-3">
      <span className="text-3xl">{icon}</span>
      <h3 className="font-display text-xl font-bold">{title}</h3>
      <p className="text-sm text-gray-400 leading-relaxed">{desc}</p>
    </div>
  );
}
