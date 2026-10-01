"""Package an original article for MANUAL cross-posting to Substack.

Substack has **no public API** (and its internal/password login forces a captcha),
so we deliberately do **not** auto-post. Instead this module produces a clean,
formatted package — title, subtitle, Markdown body, canonical link — that an admin
copies straight into the Substack composer.

`package_article()` is the single source of truth for all Substack formatting
(title-strip guard, subtitle derivation, canonical footer). Keep it here rather
than in the UI so the same rules apply wherever we cross-post.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from app.core.config import settings


# -----------------------------------------------------------------------------
# Publication helpers
# -----------------------------------------------------------------------------
def publication_url() -> str:
    """Bare publication host, e.g. ``earlknowsball.substack.com``."""
    pub = (settings.substack_publication_url or "").strip()
    pub = re.sub(r"^https?://", "", pub).strip("/")
    return pub


def home_url() -> str:
    return f"https://{publication_url()}"


def composer_url() -> str:
    """Deep link to Substack's 'new post' composer."""
    return f"https://{publication_url()}/publish/post/new"


def default_tags() -> list[str]:
    raw = (settings.substack_default_tags or "").strip()
    return [t.strip() for t in raw.split(",") if t.strip()] if raw else []


def canonical_url(sport: str, slug: Optional[str]) -> Optional[str]:
    slug = (slug or "").strip()
    if not slug:
        return None
    return f"https://earlknowsball.com/{sport}/articles/{slug}"


# -----------------------------------------------------------------------------
# Text shaping
# -----------------------------------------------------------------------------
def _strip_leading_title(title: str, text: str) -> str:
    """Drop a leading repeat of the title (word-prefix match), if present.

    Editorial drafts store ``summary`` == start-of-body, and the body often
    redundantly re-leads with its own headline. Substack takes the headline as a
    separate field, so we remove the echoed headline from the body.
    """
    if not title or not text:
        return text
    t = re.sub(r"\s+", " ", title).strip().strip("#").strip()
    if not t:
        return text
    head = text
    pat = r"^\s*" + r"\s+".join(re.escape(w) for w in t.split()) + r"[\s:—–-•|]*"
    m = re.match(pat, head, flags=re.IGNORECASE)
    if m and m.end() < len(head):
        rest = head[m.end():].lstrip()
        if rest:
            return rest
    return text


def subtitle_from_row(row: dict[str, Any], max_len: int = 280) -> str:
    s = re.sub(r"\s+", " ", (row.get("summary") or "").strip())
    s = _strip_leading_title((row.get("title") or "").strip(), s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len].rstrip()


def body_to_markdown(row: dict[str, Any], canonical: Optional[str] = None) -> str:
    """Compose the Substack body Markdown from a stored article row."""
    body = _strip_leading_title((row.get("title") or "").strip(), (row.get("content") or "").strip())

    footer_bits: list[str] = []
    if canonical:
        footer_bits.append(f"*Originally published at [Earl Knows Ball]({canonical}).*")
    footer_bits.append(
        "*Earl Knows Ball writes data-driven NFL, NBA and MLB analysis — model "
        "picks, power rankings and original research.*"
    )
    footer = "\n\n---\n\n" + "\n\n".join(footer_bits)
    return body + footer


def package_article(row: dict[str, Any]) -> dict[str, Any]:
    """Build the copy-paste package for one article.

    Returns: title, subtitle, markdown, canonical_url, suggested_tags, and the
    Substack links (composer + publication home).
    """
    sport = (row.get("sport") or "").strip()
    title = (row.get("title") or "").strip()
    if not title:
        title = ((row.get("summary") or "").strip().split(".")[0] or "Earl Knows Ball")[:120].strip()

    canon = canonical_url(sport, row.get("slug"))
    return {
        "title": title,
        "subtitle": subtitle_from_row(row),
        "markdown": body_to_markdown(row, canonical=canon),
        "canonical_url": canon,
        "suggested_tags": default_tags(),
        "composer_url": composer_url(),
        "publication_url": home_url(),
    }
