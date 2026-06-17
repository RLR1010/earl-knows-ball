import Link from "next/link";

const SPORT_INFO: Record<string, { emoji: string; name: string; subtitle: string }> = {
  nfl: {
    emoji: "🏈",
    name: "NFL",
    subtitle: "Full handicapping engine, DFS salaries, 465k+ articles, and AI chat. The most complete data on the platform.",
  },
  nba: {
    emoji: "🏀",
    name: "NBA",
    subtitle: "30 teams seeded, AI chat active with 30k+ articles. Handicapping and DFS coming soon.",
  },
  mlb: {
    emoji: "⚾",
    name: "MLB",
    subtitle: "30 teams seeded, AI chat active with 61k+ articles. Handicapping and DFS coming soon.",
  },
};

export default async function SportHomePage({ params }: { params: Promise<{ sport: string }> }) {
  const { sport } = await params;
  const info = SPORT_INFO[sport];

  if (!info) {
    return <div className="text-center py-24 text-gray-500">Sport not found</div>;
  }

  return (
    <div className="space-y-16">
      {/* Hero */}
      <section className="text-center py-16 space-y-6">
        <div className="text-6xl mb-4">{info.emoji}</div>
        <h1 className="font-display text-5xl md:text-7xl font-bold tracking-tight">
          <span className="text-white">EARL KNOWS</span>{" "}
          <span className="text-earl-400">{info.name}</span>
        </h1>
        <p className="text-lg text-gray-400 max-w-2xl mx-auto">
          {info.subtitle}
        </p>
        <div className="flex gap-4 justify-center pt-4 flex-wrap">
          <Link
            href={`/${sport}/stats`}
            className="px-6 py-3 rounded-full bg-earl-600 text-white font-semibold hover:bg-earl-500 transition"
          >
            View Stats
          </Link>
          <Link
            href={`/${sport}/teams`}
            className="px-6 py-3 rounded-full border border-white/20 text-gray-300 font-semibold hover:bg-white/5 transition"
          >
            Browse Teams
          </Link>
          <Link
            href={`/${sport}/schedule`}
            className="px-6 py-3 rounded-full border border-white/20 text-gray-300 font-semibold hover:bg-white/5 transition"
          >
            Schedule
          </Link>
          <Link
            href="/chat"
            className="px-6 py-3 rounded-full bg-white/10 text-white font-semibold hover:bg-white/20 transition"
          >
            AI Chat
          </Link>
        </div>
      </section>

      {/* Quick links */}
      <section className="grid md:grid-cols-3 gap-6">
        <FeatureCard
          title="Statistics"
          desc="Player stats, team stats, and league leaders. Sortable tables with all major categories."
          emoji="📊"
          href={`/${sport}/stats`}
        />
        <FeatureCard
          title="Teams"
          desc="Browse all teams, view depth charts, and check game-by-game schedules."
          emoji="🏟️"
          href={`/${sport}/teams`}
        />
        <FeatureCard
          title="Schedule"
          desc="Full season schedule with scores, box scores, and game details."
          emoji="📅"
          href={`/${sport}/schedule`}
        />
      </section>

      {/* Data status */}
      <section className="border border-white/10 rounded-2xl p-8 bg-white/5 space-y-4">
        <h2 className="font-display text-2xl font-bold">About {info.name} Data</h2>
        <p className="text-sm text-gray-400 leading-relaxed">
          Earl Knows Ball ingests articles, game data, player stats, and betting lines across all major
          sports. {info.name} data is continually updated through automated pipelines.
        </p>
        <Link
          href={`/${sport}/players`}
          className="inline-block text-sm text-earl-400 hover:underline"
        >
          Browse players →
        </Link>
      </section>
    </div>
  );
}

function FeatureCard({
  title,
  desc,
  emoji,
  href,
}: {
  title: string;
  desc: string;
  emoji: string;
  href: string;
}) {
  return (
    <Link
      href={href}
      className="border border-white/10 rounded-2xl p-6 bg-white/5 hover:bg-white/10 transition space-y-3 block"
    >
      <span className="text-3xl">{emoji}</span>
      <h3 className="font-display text-xl font-bold">{title}</h3>
      <p className="text-sm text-gray-400 leading-relaxed">{desc}</p>
    </Link>
  );
}
