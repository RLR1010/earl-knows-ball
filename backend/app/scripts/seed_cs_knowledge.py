"""Seed the customer-service knowledge base (cs_knowledge).

Sources:
- FAQ entries: professional starter FAQ written for Earl Knows Ball (category='faq').
- Terms & Conditions + Privacy Statement: extracted from the live Next.js page source
  (category='terms' / 'privacy') so the support bot's grounding matches what is
  actually published on the site.

Idempotent: clears the table first, then re-seeds.
"""
import html
import re
from pathlib import Path

from sqlalchemy import create_engine, text
from app.core.config import settings

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "frontend" / "src" / "app"

# ─────────────────────────────────────────────────────────────────────────────
# Professional starter FAQ (category = 'faq')
# ─────────────────────────────────────────────────────────────────────────────
FAQ = [
    (
        "What is Earl Knows Ball?",
        "Earl Knows Ball is an AI-powered sports handicapping service that provides football "
        "(NFL), baseball (MLB), and basketball (NBA) predictions, picks with probabilities, "
        "betting lines, statistics, game writeups, and a conversational AI handicapper called "
        "Earl. It is built to give bettors the research and analysis they need to make more "
        "informed decisions.",
    ),
    (
        "How does Earl's AI handicapper work?",
        "Earl combines historical stats, current betting lines, injuries, splits, situational "
        "data, and matchup analysis to produce predictions for moneyline, against-the-spread "
        "(ATS), and over/under (total) markets. You can ask Earl questions directly in the chat "
        "to get explanations behind any pick. All predictions are statistical estimates and are "
        "never guaranteed to win.",
    ),
    (
        "What is the difference between the Free and Premium plans?",
        "Free gives you limited access to picks and basic features. Premium unlocks full writeups, "
        "detailed handicapping info, and expanded chat. You can see the full comparison and upgrade "
        "on the Pricing page inside your account.",
    ),
    (
        "What payment methods do you accept?",
        "All subscriptions are processed securely through Stripe. We accept all major credit and "
        "debit cards, and Stripe's supported payment methods, depending on your region.",
    ),
    (
        "How do I cancel my subscription?",
        "You can cancel anytime by visiting your profile at https://earlknowsball.com/profile. "
        "Cancellation takes effect at the end of your current billing period, so you keep Premium "
        "access until then. After that you'll downgrade to the Free plan automatically.",
    ),
    (
        "Do you offer refunds?",
        "We do not offer refunds for our service in line with our Terms & Conditions. If you "
        "believe you were incorrectly charged, or the Service did not work as described, please "
        "contact customer support and we'll review your request.",
    ),
    (
        "Is it legal for me to use this service?",
        "Earl Knows Ball is a sports analytics and information service, and is for entertainment "
        "purposes only. We do not offer gambling, financial, or professional advice, and you must "
        "be of legal age to gamble in your jurisdiction. Always bet responsibly. If you or someone "
        "you know has a gambling problem, call 1-800-GAMBLER or contact your local responsible-"
        "gambling resource.",
    ),
    (
        "Do your picks guarantee wins?",
        "No. No handicapping service can guarantee results. Earl's picks are based on statistical "
        "models and analysis, but sports outcomes are inherently uncertain. Past performance does "
        "not guarantee future results, and you should never bet more than you can afford to lose.",
    ),
    (
        "Where does Earl's data come from?",
        "Earl pulls from official sports statistics, live betting lines, team and player data, "
        "schedules, injuries, and matchup information. Data is refreshed on a regular schedule to "
        "keep picks and writeups as current as possible.",
    ),
    (
        "How do I contact customer support?",
        "You can reach our support team using the Customer Service chat on the Support page, or an "
        "administrator may email you directly from the support system. We aim to respond as quickly "
        "as possible during business hours.",
    ),
]

# ─────────────────────────────────────────────────────────────────────────────
# ToS / Privacy extraction from the Next.js page source
# ─────────────────────────────────────────────────────────────────────────────

_ENTITY_ALIASES = {
    "&ldquo;": "\u201c", "&rdquo;": "\u201d", "&lsquo;": "\u2018", "&rsquo;": "\u2019",
    "&amp;": "&", "&quot;": '"', "&apos;": "'", "&nbsp;": " ", "&#39;": "'",
}


def _strip_entities(text: str) -> str:
    for k, v in _ENTITY_ALIASES.items():
        text = text.replace(k, v)
    return html.unescape(text)


def _strip_tags(text: str) -> str:
    # Remove <strong>, <a>, etc. but keep their inner text; drop stray braces of JSX exprs.
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("{", " ").replace("}", " ")
    return re.sub(r"\s+", " ", text).strip()


def _extract_sections(page_file: str):
    """Return list of (title, body) extracted from a static terms/privacy page."""
    src = (FRONTEND / page_file).read_text(encoding="utf-8")

    # Split into <section> blocks.
    sections = re.findall(r"<section[^>]*>(.*?)</section>", src, flags=re.S)
    out = []
    for block in sections:
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", block, flags=re.S)
        title = _strip_entities(_strip_tags(h2.group(1))) if h2 else "Terms & Conditions"
        ps = re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.S)
        bodies = [_strip_entities(_strip_tags(p)) for p in ps]
        bodies = [b for b in bodies if b and b.lower() not in ("terms & conditions",)]
        body = "\n\n".join(bodies)
        if body:
            out.append((title, body))
    return out


def main():
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM public.cs_knowledge"))

        # FAQ
        for title, body in FAQ:
            conn.execute(
                text(
                    "INSERT INTO public.cs_knowledge (category, title, content, active) "
                    "VALUES ('faq', :t, :c, TRUE)"
                ),
                {"t": title, "c": body},
            )
        print(f"Seeded {len(FAQ)} FAQ entries.")

        # Terms + Privacy sections
        for page_file, category in (("terms/page.tsx", "terms"), ("privacy/page.tsx", "privacy")):
            sections = _extract_sections(page_file)
            for title, body in sections:
                conn.execute(
                    text(
                        "INSERT INTO public.cs_knowledge (category, title, content, active) "
                        "VALUES (:cat, :t, :c, TRUE)"
                    ),
                    {"cat": category, "t": title, "c": body},
                )
            print(f"Seeded {len(sections)} {category} sections.")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT category, COUNT(*) FROM public.cs_knowledge GROUP BY category ORDER BY category"
            )
        ).fetchall()
        for r in rows:
            print(f"  {r[0]}: {r[1]} entries")


if __name__ == "__main__":
    main()
