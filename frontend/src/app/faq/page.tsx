import type { Metadata } from "next";
import type { ReactNode } from "react";
import Link from "next/link";

export const metadata: Metadata = {
  title: "FAQ",
  description:
    "Frequently asked questions about Earl Knows Ball — how the AI handicapper works, picks, probabilities, betting data, subscription plans, and support.",
  keywords: ["Earl Knows Ball FAQ", "AI sports handicapping", "sports betting help", "betting picks questions"],
};

const FAQS: { q: string; a: string | ReactNode }[] = [
  {
    q: "What is Earl Knows Ball?",
    a: "Earl Knows Ball is an AI-powered sports handicapping service that provides football (NFL), baseball (MLB), and basketball (NBA) predictions, picks with probabilities, betting lines, statistics, game writeups, and a conversational AI handicapper called Earl. It is built to give bettors the research and analysis they need to make more informed decisions.",
  },
  {
    q: "How does Earl's AI handicapper work?",
    a: "Earl combines historical stats, current betting lines, injuries, splits, situational data, and matchup analysis to produce predictions for moneyline, against-the-spread (ATS), and over/under (total) markets. You can ask Earl questions directly in the chat to get explanations behind any pick. All predictions are statistical estimates and are never guaranteed to win.",
  },
  {
    q: "What is the difference between the Free and Premium plans?",
    a: "Free gives you limited access to picks and basic features. Premium unlocks full writeups, detailed handicapping info, and expanded chat. You can see the full comparison and upgrade on the Pricing page inside your account.",
  },
  {
    q: "What payment methods do you accept?",
    a: "All subscriptions are processed securely through Stripe. We accept all major credit and debit cards, and Stripe's supported payment methods, depending on your region.",
  },
  {
    q: "How do I cancel my subscription?",
    a: (
      <>
        You can cancel anytime by visiting your profile at{" "}
        <Link href="/profile" className="text-earl-400 hover:text-earl-300 underline">
          https://earlknowsball.com/profile
        </Link>
        . Cancellation takes effect at the end of your current billing period, so you keep Premium access until
        then. After that you&apos;ll downgrade to the Free plan automatically.
      </>
    ),
  },
  {
    q: "Do you offer refunds?",
    a: "We do not offer refunds for our service in line with our Terms &amp; Conditions. If you believe you were incorrectly charged, or the Service did not work as described, please contact customer support and we'll review your request.",
  },
  {
    q: "Is it legal for me to use this service?",
    a: "Earl Knows Ball is a sports analytics and information service, and is for entertainment purposes only. We do not offer gambling, financial, or professional advice, and you must be of legal age to gamble in your jurisdiction. Always bet responsibly. If you or someone you know has a gambling problem, call 1-800-GAMBLER or contact your local responsible-gambling resource.",
  },
  {
    q: "Do your picks guarantee wins?",
    a: "No. No handicapping service can guarantee results. Earl's picks are based on statistical models and analysis, but sports outcomes are inherently uncertain. Past performance does not guarantee future results, and you should never bet more than you can afford to lose.",
  },
  {
    q: "Where does Earl's data come from?",
    a: "Earl pulls from official sports statistics, live betting lines, team and player data, schedules, injuries, and matchup information. Data is refreshed on a regular schedule to keep picks and writeups as current as possible.",
  },
  {
    q: "How do I contact customer support?",
    a: "You can reach our support team using the Customer Service chat on the Support page, or an administrator may email you directly from the support system. We aim to respond as quickly as possible during business hours.",
  },
];

export default function FAQPage() {
  return (
    <div className="max-w-3xl mx-auto px-4 py-12">
      <h1 className="text-3xl font-bold mb-2">Frequently Asked Questions</h1>
      <p className="text-sm text-gray-500 mb-10">
        Everything you need to know about Earl Knows Ball. Still have a question? Reach out through{" "}
        <a href="/support" className="text-earl-400 hover:text-earl-300 underline">
          customer service
        </a>
        .
      </p>

      <div className="space-y-3">
        {FAQS.map((faq) => (
          <details
            key={faq.q}
            className="group rounded-xl border border-white/10 bg-white/[0.02] overflow-hidden"
          >
            <summary className="flex items-center justify-between gap-4 px-5 py-4 cursor-pointer select-none hover:bg-white/[0.03] transition list-none">
              <span className="text-white font-medium">{faq.q}</span>
              <span className="text-earl-400 transition-transform duration-200 group-open:rotate-180">
                <svg xmlns="http://www.w3.org/2000/svg" className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                </svg>
              </span>
            </summary>
            <div className="px-5 pb-5">
              <p className="text-gray-400 text-sm leading-relaxed">{faq.a}</p>
            </div>
          </details>
        ))}
      </div>
    </div>
  );
}
