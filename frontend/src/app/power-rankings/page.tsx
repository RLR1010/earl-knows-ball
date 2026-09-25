import Link from "next/link";
import type { Metadata } from "next";

export const revalidate = 3600;

export const metadata: Metadata = {
  title: "Power Rankings — Earl Knows Ball",
  description:
    "Earl's weekly NFL, NBA and MLB power rankings: every team rated in points, backed by hard data and updated each week.",
  alternates: { canonical: "/power-rankings" },
};

const SPORTS: { slug: string; label: string }[] = [
  { slug: "nfl", label: "NFL" },
  { slug: "nba", label: "NBA" },
  { slug: "mlb", label: "MLB" },
];

export default function PowerRankingsLanding() {
  return (
    <div className="max-w-3xl mx-auto px-4 py-10">
      <h1 className="text-3xl font-bold text-white mb-3">Earl&apos;s Power Rankings</h1>
      <p className="text-gray-300 mb-8 max-w-2xl">
        Every team rated in <strong>points on a neutral field</strong>, updated weekly and backed
        by hard data. A rating gap is the spread — so the board doubles as Earl&apos;s view of the
        market.
      </p>
      <ul className="space-y-3">
        {SPORTS.map((s) => (
          <li key={s.slug}>
            <Link
              href={`/${s.slug}/power-rankings`}
              className="block rounded-lg border border-white/10 px-5 py-4 hover:border-emerald-500/50 hover:bg-white/5"
            >
              <span className="text-lg font-semibold text-white">{s.label} Power Rankings</span>
              <span className="block text-sm text-gray-400">
                Weekly team ratings, movement, and Earl&apos;s take.
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
