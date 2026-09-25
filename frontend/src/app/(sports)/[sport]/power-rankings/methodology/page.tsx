import Link from "next/link";
import type { Metadata } from "next";

import { getMeta } from "@/lib/power-rankings";
import { sportLabel } from "@/lib/seo-content";

type Props = { params: Promise<{ sport: string }> };

export const revalidate = 3600;

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { sport } = await params;
  const label = sportLabel(sport);
  const title = `${label} Power Rankings Methodology — How Earl Rates Teams`;
  const description = `How Earl Knows Ball builds its ${label} power rankings: a transparent, points-denominated team rating model backed by hard data.`;
  const url = `/${sport}/power-rankings/methodology`;
  return {
    title,
    description,
    alternates: { canonical: url },
    openGraph: { title, description, url, siteName: "Earl Knows Ball", type: "article" },
  };
}

export default async function Methodology({ params }: Props) {
  const { sport } = await params;
  const label = sportLabel(sport);
  const meta = await getMeta(sport);

  return (
    <div className="max-w-3xl mx-auto px-4 py-8 prose-invert">
      <nav className="text-sm text-gray-500 mb-2">
        <Link href={`/${sport}/power-rankings`} className="hover:text-emerald-400">
          {label} Power Rankings
        </Link>
        <span className="mx-1">/</span>
        <span>Methodology</span>
      </nav>
      <h1 className="text-3xl font-bold text-white mb-4">
        How Earl rates {label} teams
      </h1>

      <p className="text-gray-300 mb-4">{meta?.methodology ?? "Methodology coming soon."}</p>

      <h2 className="text-xl font-semibold text-white mt-6 mb-2">Reading the number</h2>
      <ul className="list-disc pl-6 text-gray-300 space-y-2">
        <li>
          <strong>Rating (points, neutral field).</strong> A team&apos;s rating minus its
          opponent&apos;s approximates the expected margin. A +6.0 team is about six points better
          than average, i.e. roughly a six-point favourite on a neutral field.
        </li>
        <li>
          <strong>Δ Wk.</strong> Movement versus last week — ratings are anchored, so they change
          only as much as results justify.
        </li>
        <li>
          <strong>SOS.</strong> Strength of schedule — the average rating of the opponents a team
          has faced, expressed in <em>points versus the league average</em>. A positive SOS means a
          team has played a harder-than-average schedule; a negative SOS means an easier one. (It
          is not a ranking — it&apos;s the same points scale as the rating itself, so it can be
          negative.)
        </li>
        <li>
          <strong>Injuries.</strong> Earl docks points off a team&apos;s rating for key players
          unavailable for that week&apos;s game. Each unavailable player is worth a position-based
          amount (a starting quarterback is worth far more than a rotational defender), scaled by
          how big a role he actually plays and by how likely he is to miss the game
          (Out / Doubtful / Questionable, or a practice-status fallback). The docked total is
          shown as <em>Inj</em>. The rating still starts from on-field results, so the injury dock
          reflects who is <em>expected to be missing going forward</em>.
        </li>
        <li>
          <strong>vs the Market.</strong> Separately, Earl solves the same rating model on the
          <em>closing point spreads</em> of every game to produce a market-implied rating, then
          places it on Earl&apos;s points scale and reports the difference as <em>vs Mkt</em>. A
          positive number means Earl is higher on the team than the betting market; a negative
          number means the market is higher. The market is <em>never</em> an input to Earl&apos;s
          rating — it is a comparison only.
        </li>
      </ul>

      <h2 className="text-xl font-semibold text-white mt-6 mb-2">Principles</h2>
      <ul className="list-disc pl-6 text-gray-300 space-y-2">
        <li>Every rating is decomposed into its inputs, so any ranking can be audited.</li>
        <li>Ratings use FINAL games only — never future results.</li>
        <li>Early in the season ratings shrink toward last season&apos;s finish, then stand on their own.</li>
        <li>Earl&apos;s written take is commentary on the number — never an input to it.</li>
      </ul>

      {meta?.season && meta?.week ? (
        <p className="text-sm text-gray-500 mt-6">
          Current snapshot: {label} {meta.season}, Week {meta.week}.
        </p>
      ) : null}
    </div>
  );
}
