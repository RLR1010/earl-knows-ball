"""Headline accent-text helpers for original-article social cards.

The LLM may propose a short "accent" phrase (e.g. "a Top-Heavy") that the card
renders in the theme accent color. These helpers make that safe:

  accent_bounds(title, accent)  -> (start, end) slice of the accent within the
                                   title (case-insensitive, first match), or None
                                   when accent is empty / not a real substring.
  split_title_on_accent(...)    -> [before, accent, after] for the card template.
  valid_accent(title, accent)   -> bool server-side guardrail:
                                   substring of title, not pure number/date/short.

Pure functions, no I/O, no LLM — unit-testable and safe to import anywhere.
"""
from __future__ import annotations

import re

# Tokens that are bad accent candidates: pure numbers, scores, dates, years,
# and very generic stop-words. Accent should be an evocative word/phrase.
_BARE_NUM_RE = re.compile(
    r"^(?:\d+|\d+-\d+|\d+\.\d+|%\d+|\$\d[\d,.]*|(?:19|20)\d{2})$"
)
_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "for", "at", "on", "in", "with",
    "by", "vs", "as", "so", "but", "not", "is", "are", "was", "were", "than",
}

MIN_ACCENT_LEN = 3      # shorter than "you"/"it" -> not a hook
SPLIT_LEN = 128         # guard against absurd inputs


def accent_bounds(title: str, accent: str | None):
    """Return (start, end) slice indices of `accent` in `title`, else None.

    Case-insensitive first occurrence. `accent` is trusted text (it came from the
    authoring prompt / article row) but still escaped at the HTML layer later.
    """
    if not title or not accent:
        return None
    low_t = title.lower()
    low_a = accent.lower().strip()
    if not low_a or len(low_a) > SPLIT_LEN or len(low_t) > SPLIT_LEN:
        return None
    i = low_t.find(low_a)
    if i < 0:
        return None
    return (i, i + len(low_a))


def split_title_on_accent(title: str, accent: str | None):
    """Return [before, accenpiece, after] splitting title at the accent.

    If accent is missing/invalid/not found -> [title, "", ""].
    """
    t = (title or "").strip()
    if not t:
        return ["", "", ""]
    b = accent_bounds(t, accent)
    if not b:
        return [t, "", ""]
    s, e = b
    return [t[:s], t[s:e], t[e:]]


def render_accented_title(title: str, accent: str | None, span: str = "em"):
    """Return an HTML string with the accent span, for embedding in card markup.

    Everything is HTML-escaped; the accent piece is wrapped in <span> using the
    given tag/class-name so CSS colors it. Empty accent -> plain escaped title.
    """
    import html as _h

    before, piece, after = split_title_on_accent(title, accent)
    esc = _h.escape
    if not piece:
        return esc(before + piece + after)
    return f"{esc(before)}<{span}>{esc(piece)}</{span}>{esc(after)}"


def valid_accent(title: str, accent: str | None) -> bool:
    """Server-side guardrail: would this accent render (not trash the headline)?

    Rules:
      * accent must be a non-empty contiguous substring of title;
      * not a pure number / score / date / year;
      * length >= MIN_ACCENT_LEN; not composed only of stop-words.
    """
    b = accent_bounds(title, accent)
    if not b:
        return False
    piece = (accent or "").strip()
    lower = piece.lower()
    # strip surrounding punctuation from the phrase for classification
    core = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", lower)
    if not core or len(core) < MIN_ACCENT_LEN:
        return False
    if _BARE_NUM_RE.match(core):
        return False
    words = [w for w in re.split(r"[^a-z0-9]+", core) if w]
    if not words:
        return False
    # reject if EVERY word is a stop-word (e.g. "the of") or just one tiny stop
    non_stop = [w for w in words if w not in _STOP]
    if not non_stop:
        return False
    # reject a phrase that is only a bare number surrounded by stop-words
    if all(_BARE_NUM_RE.match(w) for w in non_stop):
        return False
    return True


# Deterministic fallback when the LLM proposes nothing usable: pick the longest
# non-stop, non-numeric token that's not the very first word of the headline.
# Keeps the timeline varied without a forced accent when nothing qualifies.
def fallback_accent(title: str):
    """Return a reasonable accent token from the title, or None.

    Choose the last candidate token that is not pure-numeric and not a stop-word,
    preferring a mid/end word (headline start is rarely the hook). Deterministic.
    """
    t = (title or "").strip()
    if not t:
        return None
    tokens = re.split(r"\s+", t)
    seen = []
    for i, tok in enumerate(tokens):
        core = re.sub(r"^[^a-zA-Z0-9]+|[^a-zA-Z0-9]+$", "", tok)
        if len(core) >= MIN_ACCENT_LEN and core.lower() not in _STOP and not _BARE_NUM_RE.match(core):
            seen.append((i, tok.strip(".,:;!?()\"'`-…—–")))
    # after the first word, prefer the LAST eligible (a hook near the end)
    tail = [(i, tk) for (i, tk) in seen if i >= 1]
    pool = tail or seen
    if not pool:
        return None
    return pool[-1][1]
