"""LLM-based team extraction for original articles.

Given an article's title + body, ask DeepSeek to list every team mentioned,
ordered from most-mentioned to least-mentioned, returned as team abbreviations.
The article page then shows each team's logo (via the frontend team_logos
lookup) for the teams it covers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("team_extractor")

# Valid team abbreviations per sport. The LLM is constrained to these, so the
# extracted values resolve to logos on the frontend (getTeamLogoUrl).
VALID_ABBR: dict[str, set[str]] = {
    "mlb": {
        "ARI", "ATL", "BAL", "BOS", "CHC", "CIN", "CLE", "COL", "CWS", "DET",
        "HOU", "KC", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK",
        "PHI", "PIT", "SD", "SEA", "SF", "STL", "TB", "TEX", "TOR", "WSH",
    },
    "nba": {
        "ATL", "BOS", "CLE", "NOP", "CHI", "DAL", "DEN", "GSW", "HOU", "LAC",
        "LAL", "MIA", "MIL", "MIN", "BKN", "NYK", "ORL", "IND", "PHI", "PHX",
        "POR", "SAC", "SAS", "OKC", "TOR", "UTA", "MEM", "WAS", "DET", "CHA",
    },
    "nfl": {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
        "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
        "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
        "TEN", "WSH",
    },
}

EXTRACT_SYSTEM_PROMPT = (
    "You are a sports analytics editor. Given an article title and body, list "
    "every team that is mentioned, ordered from MOST-mentioned to "
    "LEAST-mentioned. Use ONLY the valid team abbreviations provided. Consider "
    "the team name, its city, its nickname, and any obvious alias (e.g. "
    "'the Astros', 'Houston', 'HOU' all map to HOU)."
    "\nRules:"
    "\n- Include a team only if it is actually mentioned in the body; if none "
    "  are mentioned, return an empty list."
    "\n- If the article is PRIMARILY about one team and other teams are only "
    "  mentioned in passing (as an opponent, in a schedule roundup, or as a "
    "  comparison), list ONLY the team the article is really about. Do not "
    "  tag teams that are merely mentioned in passing as a side note."
    "\n- Rule of thumb: list a team only if the article meaningfully centers "
    "  on it (deep coverage: its game, its season, its players, its outlook). "
    "  A passing mention in a sentence does not make the article 'about' that "
    "  team."
    "\n- Order strictly by frequency of mention, most frequent first."
    "\n- Return ONLY JSON in this exact shape: {\"teams\": [\"NYY\", \"BOS\"]} "
    "  using the provided valid abbreviations. No markdown, no commentary."
)


def _manifest(sport: str) -> str:
    valid = sorted(VALID_ABBR.get(sport, set()))
    return ", ".join(valid)


async def extract_teams(
    sport: str, title: str, content: str, max_teams: int = 6
) -> list[str]:
    """Return the article's mentioned teams (abbreviations), most-mentioned first."""
    valid = VALID_ABBR.get(sport)
    if not valid:
        return []
    if not settings.deepseek_api_key:
        return []
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=f"{settings.deepseek_base_url.rstrip('/')}/v1",
        timeout=60.0,
    )
    body = (content or "")[:4000]
    prompt = (
        f"VALID TEAM ABBREVIATIONS ({sport.upper()}):\n{_manifest(sport)}\n\n"
        f"TITLE:\n{title}\n\nBODY:\n{body}"
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=4000,
            extra_body={"thinking": {"type": "disabled"}},
        )
        raw = resp.choices[0].message.content or ""
        # DeepSeek thinking models sometimes put the JSON in reasoning_content.
        if not raw.strip():
            raw = getattr(resp.choices[0].message, "reasoning_content", None) or ""
    except Exception as e:  # noqa: BLE001
        logger.warning("team extraction failed for %r: %s", title, e)
        return []

    teams = _parse_teams(raw, valid)
    # Keep only the top N, de-duplicated, preserving most-mentioned-first order.
    return teams[:max_teams]


def _parse_teams(raw: str, valid: set[str]) -> list[str]:
    """Best-effort parse of the LLM's JSON (handles code fences)."""
    if not raw:
        return []
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if not isinstance(data, dict):
        # Try to find a JSON array manually.
        arr = re.search(r"\[.*?\]", raw, re.DOTALL)
        if arr:
            try:
                data = {"teams": json.loads(arr.group(0))}
            except Exception:
                data = None
    if not isinstance(data, dict):
        return []

    teams = data.get("teams") or data.get("team") or []
    out: list[str] = []
    seen: set[str] = set()
    for t in teams if isinstance(teams, list) else []:
        abbr = str(t).strip().upper()
        if abbr in valid and abbr not in seen:
            seen.add(abbr)
            out.append(abbr)
    return out


def extract_teams_blocking(
    sport: str, title: str, content: str, max_teams: int = 6
) -> list[str]:
    """Synchronous wrapper for callers outside an async context."""
    return asyncio.run(extract_teams(sport, title, content, max_teams))


def sanitize_teams(teams: object) -> list[str]:
    """Cast whatever came back from the DB to a JSON-safe list of abbrs."""
    if not teams:
        return []
    if isinstance(teams, list):
        return [str(t).strip().upper() for t in teams if isinstance(t, (str,)) and t.strip()]
    return []
