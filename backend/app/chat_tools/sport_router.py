"""Cross-sport chat routing (prototype B).

When a user is in one sport's chat (e.g. NFL) but asks a question that is really about a
different sport (e.g. "how are the Astros doing?"), the current chat engine has no tools
for that sport and cannot answer from data. This module classifies the sport of an incoming
message so the frontend can *switch* the conversation to the right sport's chat instead.

The classifier is intentionally tiny and cheap: one temperature-0 JSON classification call.
It returns a sport only when it is one of nfl/nba/mlb AND differs from the sport of the chat
the user is already in, so same-sport / ambiguous / multi-sport messages are left alone.
"""

from __future__ import annotations

import json
import logging
import re

from app.core.config import settings

logger = logging.getLogger(__name__)

#: The sports the product supports (matches the per-sport chat routes).
VALID_SPORTS = ("nfl", "nba", "mlb")

#: Human-readable labels for UI use.
SPORT_LABELS = {"nfl": "NFL", "nba": "NBA", "mlb": "MLB"}

_SYSTEM = (
    "Classify which sport a user's chat question is about. Choose exactly one of: "
    "mlb (baseball / MLB), nfl (American football / NFL), nba (basketball / NBA), "
    "none (not sport-specific, unclear, or genuinely about more than one sport). "
    'Reply with ONLY JSON: {"sport":"<mlb|nfl|nba|none>"}'
)


async def detect_sport(message: str, current_sport: str | None = None) -> str | None:
    """Return the sport the message is about, or ``None`` to stay where we are.

    Returns a sport only when the classification is one of ``nfl``/``nba``/``mlb`` **and**
    it differs from ``current_sport``. Anything ambiguous, multi-sport, non-sport, or equal
    to the current sport yields ``None`` so normal in-sport chat is unaffected.
    """
    text = (message or "").strip()
    if len(text) < 3:
        return None

    from openai import AsyncOpenAI  # lazy import: only needed when a message arrives

    try:
        client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
        )
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text[:1500]},
            ],
            # DeepSeek V4.x reasons by default on the raw API; a generous budget keeps
            # content from being starved by reasoning tokens (see TOOLS.md).
            max_tokens=2000,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        detected = str((json.loads(raw) or {}).get("sport", "")).strip().lower()
    except Exception:  # noqa: BLE001 — never let routing break chat
        logger.exception("chat sport detection failed")
        return None

    current = (current_sport or "").strip().lower()
    if detected in VALID_SPORTS and detected != current:
        return detected
    return None
