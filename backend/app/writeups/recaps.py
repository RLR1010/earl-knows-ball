"""Post-game recap articles for earlknowsball.com.

A recap is a short, factual, SEO-rich article written the *next morning* after a
game. It examines what actually happened and compares it to what our model told
subscribers beforehand (the premium pick + prop plays), calling out what we got
right and wrong and what it implies for future picks.

Storage: rows in ``public.original_articles`` with ``section='recap'`` so they
automatically appear in the sport + team Article listings and get their own
``/<sport>/articles/<slug>`` page. Recaps are public (free) because the game is
already over — see project notes.

Timing: scheduled for early morning ET (off-peak DeepSeek pricing, after the
overnight article scrapes, and before the next day's preview writeups run).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings

log = logging.getLogger("earl.recaps")

ET = ZoneInfo("America/New_York")


def _et_date(dt: Any) -> Any:
    """Return the US-Eastern calendar date for a timestamp/date.

    All "previous day" / game-date reasoning on the site is anchored to US
    Eastern, so every recap date must be computed the same way.
    """
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return (dt.astimezone(ET) if dt.tzinfo else dt).date()
    return dt  # already a date

# --- per-sport wiring -------------------------------------------------------
# ``picks`` is an ordered list of (pick_column, result_column, human_label).
SPORTS: dict[str, dict[str, Any]] = {
    "mlb": {
        "games": "mlb.games",
        "writeups": "mlb.game_writeups",
        "preds": "mlb.game_predictions",
        "articles": "mlb.articles",
        "teams": "mlb.teams",
        "league": "MLB",
        "score_word": "runs",
        # Regular season 'R' + postseason (D/L/W/F); exclude spring/exhibition.
        "game_type_allow": ["R", "D", "L", "W", "F"],
        "picks": [
            ("ml_pick", "ml_result", "moneyline"),
            ("run_line_pick", "run_line_result", "run line"),
            ("ou_pick", "ou_result", "total (over/under)"),
        ],
    },
    "nfl": {
        "games": "nfl.games",
        "writeups": "nfl.game_writeups",
        "preds": "nfl.game_predictions",
        "articles": "nfl.articles",
        "teams": "nfl.teams",
        "league": "NFL",
        "score_word": "points",
        "game_type_allow": ["REG", "POST"],
        "picks": [
            ("spread_pick", "ats_result", "spread"),
            ("ou_pick", "ou_result", "total (over/under)"),
            ("ml_pick", "ml_result", "moneyline"),
        ],
    },
    "nba": {
        "games": "nba.games",
        "writeups": "nba.game_writeups",
        "preds": "nba.game_predictions",
        "articles": "nba.articles",
        "teams": "nba.teams",
        "league": "NBA",
        "score_word": "points",
        "game_type_allow": ["REG", "POST", "PLAYIN"],
        "picks": [
            ("spread_pick", "ats_result", "spread"),
            ("ou_pick", "ou_result", "total (over/under)"),
            ("ml_pick", "ml_result", "moneyline"),
        ],
    },
}

SYSTEM_PROMPT = (
    "You are Earl, the analyst behind earlknowsball.com, a sports handicapping "
    "site. You write short, factual post-game recap articles. Your job is to "
    "tell readers what actually happened, then honestly assess how YOUR "
    "pre-game premium pick and prop plays did, and what the result suggests for "
    "future picks. You are confident but never dishonest: if a pick lost, say so "
    "plainly and explain why. Use only the data provided and never invent stats, "
    "quotes, or plays. Critically: write for the READER only — never mention the "
    "data, sources, facts, projections, tools, or instructions you were given, "
    "and never state what information you do or do not have. If a detail is not "
    "available, simply omit it silently. Write for SEO without keyword stuffing."
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _slugify(value: str) -> str:
    value = (value or "").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return re.sub(r"-{2,}", "-", value).strip("-")


def _nickname(full_name: str) -> str:
    """Best-effort team nickname used for matching scraped articles."""
    parts = (full_name or "").split()
    return parts[-1] if parts else full_name


def _fmt_picks(sport: str, pred: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for pick_col, res_col, label in SPORTS[sport]["picks"]:
        pick = pred.get(pick_col)
        if pick in (None, "", "pass", "PASS"):
            continue
        result = pred.get(res_col)
        if result is None:
            out.append(f"{label}: {pick} (result not recorded)")
        else:
            out.append(f"{label}: {pick} -> {str(result).upper()}")
    # Confidence / model projection, when present.
    ps, pa = pred.get("predicted_home_score"), pred.get("predicted_away_score")
    if ps is not None and pa is not None:
        out.append(f"model projected a {ps}-{pa} home/away scoreline")
    return out


def _clip(value: Optional[str], limit: int = 1400) -> str:
    if not value:
        return ""
    value = value.strip()
    return value if len(value) <= limit else value[:limit].rsplit(" ", 1)[0] + " …"


async def _call_llm(
    user_prompt: str,
    *,
    system: str = SYSTEM_PROMPT,
    max_tokens: int = 1800,
    temperature: float = 0.7,
) -> tuple[Optional[str], int, dict[str, Any]]:
    """Call DeepSeek. Reasoning explicitly disabled (writing task).

    Returns ``(content, total_tokens, usage_dict)``.
    """
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
    )
    base_kwargs = dict(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    # Disable the model's default reasoning to save tokens/cost.
    try:
        resp = await client.chat.completions.create(
            **base_kwargs, extra_body={"thinking": {"type": "disabled"}}
        )
    except Exception as exc:  # noqa: BLE001 - fall back to a plain request
        log.warning("thinking=disabled rejected (%s); retrying without", exc)
        resp = await client.chat.completions.create(**base_kwargs)
    content = (resp.choices[0].message.content or "").strip()
    tokens = 0
    usage: dict[str, Any] = {}
    try:
        if resp.usage is not None:
            usage = resp.usage.model_dump()
            tokens = int(usage.get("total_tokens") or 0)
    except Exception:  # noqa: BLE001 - usage accounting is best-effort
        usage, tokens = {}, 0
    return (content or None), tokens, usage


def _parse_json(raw: str) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    raw = raw.strip()
    # Strip ``` fences if present.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


async def _scraped_snippets(
    session: AsyncSession,
    sport: str,
    home_name: str,
    away_name: str,
    game_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """Post-game article coverage about the matchup.

    Primary path reuses the same pgvector retriever the preview writeups use
    (``app.ingestion.pgvector_search.search_articles``), windowed to articles
    published from the game date onward (i.e. post-game reports). Falls back to
    a plain keyword match on the sport's scraped-articles table.
    """
    try:
        from app.ingestion.pgvector_search import search_articles

        date_from = game_dt
        date_to = datetime.now(game_dt.tzinfo) if game_dt and game_dt.tzinfo else datetime.utcnow()
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for q in (
            f"{away_name} vs {home_name} final score recap",
            f"{home_name} {away_name} game recap",
            f"{away_name} {home_name}",
        ):
            try:
                res = await search_articles(
                    db=session, query=q, sport=sport,
                    date_from=date_from, date_to=date_to, top_k=5,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("vector search failed for %s: %s", sport, exc)
                break
            for a in res or []:
                t = a.get("title") or ""
                if t and t not in seen:
                    seen.add(t)
                    out.append(
                        {
                            "title": t,
                            "excerpt": _clip(a.get("text") or "", 300),
                            "source": a.get("source_name"),
                        }
                    )
            if len(out) >= 8:
                break
            await asyncio.sleep(0.05)
        if out:
            return out[:8]
    except Exception:  # noqa: BLE001 - fall back to keyword match below
        log.exception("scraped-article vector lookup unavailable for %s", sport)
    return await _scraped_snippets_kw(session, sport, home_name, away_name)


async def _scraped_snippets_kw(
    session: AsyncSession, sport: str, home_name: str, away_name: str
) -> list[dict[str, Any]]:
    cfg = SPORTS[sport]
    patterns = []
    for name in (home_name, away_name):
        if not name:
            continue
        patterns.append(f"%{name}%")
        nick = _nickname(name)
        if nick != name:
            patterns.append(f"%{nick}%")
    if not patterns:
        return []
    where = " OR ".join(
        ["a.title ILIKE :p{i} OR a.excerpt ILIKE :p{i}".format(i=i) for i in range(len(patterns))]
    )
    params = {f"p{i}": p for i, p in enumerate(patterns)}
    sql = text(
        f"""
        SELECT a.title, a.excerpt, a.source_name, a.published_at
        FROM {cfg['articles']} a
        WHERE a.published_at IS NOT NULL
          AND a.published_at > now() - interval '40 hours'
          AND ({where})
        ORDER BY a.published_at DESC
        LIMIT 8
        """
    )
    try:
        rows = (await session.execute(sql, params)).fetchall()
    except Exception as exc:  # noqa: BLE001 - scraped-article grounding is optional
        log.warning("scraped-article lookup failed for %s: %s", sport, exc)
        return []
    return [
        {"title": r.title, "excerpt": _clip(r.excerpt, 300), "source": r.source_name}
        for r in rows
    ]


_META_PATTERNS = re.compile(
    r"(the facts i (have|was given|have been given)|"
    r"i (do not|don'?t) have|"
    r"not (supplied|provided|available) (in|to) (the )?(facts|data)|"
    r"(can'?t|cannot|won'?t|will not|not going to) grade|"
    r"the (data|information) i have|"
    r"(facts|data|information) (i|we) (have|have been given|received)|"
    r"what i can say is|"
    r"(not|isn'?t) in (the )?(supplied )?(facts|data))",
    re.IGNORECASE,
)
_META_FINDING = {
    "claim": "meta-commentary about the writer's own data/process",
    "issue": (
        "The draft refers to the facts/data/information it was given, or to what "
        "it does or does not have. Write ONLY for the reader: never mention your "
        "inputs, sources, tools, or instructions, and never state that data is "
        "missing. If a detail (e.g. a prop result) is not available, omit it silently."
    ),
}


_VERIFIER_SYSTEM = (
    "You are a meticulous sports fact-checker for post-game recap articles. "
    "Check ONLY concrete factual claims of these kinds: the final score, who won, "
    "the margin, the total, and whether OUR premium pick and each of OUR prop plays "
    "WON or LOST (including any stated odds). "
    "Do NOT flag analysis, opinions, rationale, predictions, or what a result 'means "
    "for future picks' — those are the author's commentary, not facts. "
    "Do NOT flag claims that are simple arithmetic derived from the FACTS (e.g. a "
    "3-run margin means a +1.5 run line missed by 1.5). "
    "Only report a finding when a material factual claim CONTRADICTS the FACTS, or "
    "states a score / winner / margin / total / pick outcome that the FACTS do not "
    "support. If the draft is consistent with the FACTS, return no findings. "
    "Return STRICT JSON only."
)


def _ip(value: Any) -> str:
    """Format a baseball innings value (stored as decimal thirds) as 3.1 / 3.2."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    whole = int(v)
    frac = round(v - whole, 1)
    if abs(frac - 0.3) < 0.06:
        return f"{whole}.1"
    if abs(frac - 0.7) < 0.06:
        return f"{whole}.2"
    return f"{whole}.0"


async def _player_lines_block(
    session: AsyncSession, sport: str, game_id: int, blob: str
) -> str:
    """Final box-score lines for the players we wrote about, so player props can
    be graded from real data. Implemented for MLB; other sports return ""."""
    if sport != "mlb" or not blob:
        return ""
    pitchers = [
        dict(r._mapping)
        for r in (
            await session.execute(
                text(
                    "SELECT pitcher_name AS name, team_abbr AS team, is_starter, "
                    "ip, er, h, k, bb, hr FROM mlb.pitcher_game_stats WHERE game_id = :g"
                ),
                {"g": game_id},
            )
        ).fetchall()
    ]
    batters = [
        dict(r._mapping)
        for r in (
            await session.execute(
                text(
                    "SELECT p.name AS name, b.at_bats, b.hits, b.home_runs, b.total_bases, "
                    "b.runs, b.runs_batted_in, b.base_on_balls, b.strikeouts "
                    "FROM mlb.batting_game_stats b JOIN mlb.players p ON p.id = b.player_id "
                    "WHERE b.game_id = :g"
                ),
                {"g": game_id},
            )
        ).fetchall()
    ]
    out: list[str] = []
    for r in pitchers:
        if r.get("name") and r["name"] in blob:
            tag = "SP" if r.get("is_starter") else "RP"
            out.append(
                f"- {r['name']} ({r.get('team')}, {tag}): {_ip(r.get('ip'))} IP, "
                f"{r.get('er')} ER, {r.get('h')} H, {r.get('k')} K, {r.get('bb')} BB, {r.get('hr')} HR"
            )
    for r in batters:
        if r.get("name") and r["name"] in blob:
            out.append(
                f"- {r['name']}: {r.get('at_bats')} AB, {r.get('hits')} H, "
                f"{r.get('home_runs')} HR, {r.get('total_bases')} TB, {r.get('runs')} R, "
                f"{r.get('runs_batted_in')} RBI, {r.get('base_on_balls')} BB, {r.get('strikeouts')} K"
            )
    if not out:
        return ""
    return (
        "BOX SCORE — FINAL LINES FOR PLAYERS WE WROTE ABOUT (authoritative):\n"
        + "\n".join(out)
    )


def _facts_text(
    sport: str, game: dict[str, Any], writeup: dict[str, Any], pred: dict[str, Any],
    player_lines: str = "",
) -> str:
    """Authoritative, deterministic facts — the single source of truth for both
    writing and fact-checking.

    Includes the data AND the pre-game material we published (premium pick, prop
    plays), since those are what the recap grades.
    """
    cfg = SPORTS[sport]
    hs, aws = game.get("home_score"), game.get("away_score")
    gdate = game.get("game_date_et")
    if gdate is None and game.get("date") is not None:
        gdate = _et_date(game["date"])
    lines = [
        f"LEAGUE: {cfg['league']}",
        (f"DATE: {gdate.strftime('%A, %B %-d, %Y')}" if hasattr(gdate, "strftime") else "DATE: unknown"),
        f"GAME: {game['away_name']} (away) at {game['home_name']} (home)",
        f"FINAL SCORE: {game['away_name']} {aws}, {game['home_name']} {hs}",
    ]
    if hs is not None and aws is not None:
        winner = (
            game["home_name"] if hs > aws else game["away_name"] if aws > hs else "TIE"
        )
        lines.append(f"WINNER: {winner} by {abs(hs - aws)} {cfg['score_word']}")
        lines.append(f"TOTAL {cfg['score_word'].upper()}: {hs + aws}")
    picks = _fmt_picks(sport, pred)
    lines.append(
        "OUR PRE-GAME MODEL CALLS (authoritative): "
        + ("; ".join(picks) if picks else "none recorded")
    )
    if writeup.get("premium_content"):
        lines.append(
            "OUR PREMIUM PICK (published to subscribers before the game):\n"
            + _clip(writeup["premium_content"], 1400)
        )
    if writeup.get("prop_content"):
        lines.append(
            "OUR PROP PLAYS (published to subscribers before the game):\n"
            + _clip(writeup["prop_content"], 1400)
        )
    if player_lines:
        lines.append(player_lines)
    return "\n".join(lines)


def _research_trace(
    sport: str,
    game: dict[str, Any],
    writeup: dict[str, Any],
    pred: dict[str, Any],
    snippets: list[dict[str, Any]],
    facts_text: str,
) -> list[dict[str, Any]]:
    """Deterministic research trace stored in ``research_json`` (admin-visible).

    The first step carries the authoritative FACTS text so the admin
    "Recheck" action can re-verify a recap without regenerating it.
    """
    cfg = SPORTS[sport]
    return [
        {
            "tool": "facts",
            "arguments": {"game_id": game["id"]},
            "result": facts_text,
        },
        {
            "tool": f"{sport}_game_result",
            "arguments": {"game_id": game["id"]},
            "result": {
                "away": game["away_name"],
                "home": game["home_name"],
                "away_score": game["away_score"],
                "home_score": game["home_score"],
            },
        },
        {
            "tool": f"{sport}_premium_pick",
            "arguments": {"game_id": game["id"]},
            "result": _clip(writeup.get("premium_content"), 1000) or None,
        },
        {
            "tool": f"{sport}_prop_plays",
            "arguments": {"game_id": game["id"]},
            "result": _clip(writeup.get("prop_content"), 1000) or None,
        },
        {
            "tool": f"{sport}_model_predictions",
            "arguments": {"game_id": game["id"]},
            "result": _fmt_picks(sport, pred),
        },
        {
            "tool": f"{sport}_scraped_articles",
            "arguments": {"teams": [game["away_name"], game["home_name"]]},
            "result": [s.get("title") for s in snippets],
        },
    ]


def facts_from_trace(trace: list[dict[str, Any]] | None) -> Optional[str]:
    """Recover the authoritative FACTS text from a recap research trace.

    Prefers the dedicated ``facts`` step; otherwise synthesizes a facts block
    from the stored game-result + model-prediction steps.
    """
    steps = [s for s in (trace or []) if isinstance(s, dict)]
    for step in steps:
        if step.get("tool") == "facts":
            result = step.get("result")
            if isinstance(result, str) and result.strip():
                return result

    result = next(
        (s.get("result") for s in steps if str(s.get("tool", "")).endswith("_game_result")),
        None,
    )
    if not isinstance(result, dict):
        return None
    preds = next(
        (
            s.get("result")
            for s in steps
            if str(s.get("tool", "")).endswith("_model_predictions")
        ),
        None,
    )
    away, home = result.get("away"), result.get("home")
    aws, hs = result.get("away_score"), result.get("home_score")
    lines = [
        f"GAME: {away} (away) at {home} (home)",
        f"FINAL SCORE: {away} {aws}, {home} {hs}",
    ]
    if isinstance(hs, int) and isinstance(aws, int):
        winner = home if hs > aws else away if aws > hs else "TIE"
        lines.append(f"WINNER: {winner} by {abs(hs - aws)}")
    if preds:
        lines.append("OUR PRE-GAME MODEL CALLS (authoritative): " + "; ".join(map(str, preds)))
    return "\n".join(lines)


async def _verify_recap(title: str, body: str, facts: str) -> dict[str, Any]:
    """Fact-check the draft against the authoritative facts. Never publishes blind."""
    out: dict[str, Any] = {
        "raw": None,
        "passed": True,
        "tokens": 0,
        "findings": [],
        "retries_used": 0,
        "accuracy_pass": True,
        "has_inaccuracy": False,
        "verification_error": False,
    }
    prompt = (
        f"FACTS:\n{facts}\n\n"
        f"DRAFT TITLE:\n{title}\n\n"
        f"DRAFT BODY:\n{body}\n\n"
        "Check the draft against the FACTS. Report a finding ONLY for a material "
        "factual error: a wrong score/winner/margin/total, or a premium/prop pick "
        "labeled as won/lost inconsistently with the FACTS. Ignore commentary, "
        "opinions, and what it means for future picks. "
        'Return STRICT JSON: {"passed": <bool>, "findings": '
        '[{"claim": "...", "issue": "..."}]}. '
        'If the draft is accurate and consistent, return {"passed": true, "findings": []}.'
    )
    try:
        raw, tokens, _ = await _call_llm(
            prompt, system=_VERIFIER_SYSTEM, max_tokens=900, temperature=0.0
        )
    except Exception:  # noqa: BLE001
        log.exception("recap verification call failed")
        out["verification_error"] = True
        return out
    out["raw"], out["tokens"] = raw, tokens
    parsed = _parse_json(raw or "")
    if not isinstance(parsed, dict):
        out["verification_error"] = True
        return out
    findings = parsed.get("findings") or []
    passed = bool(parsed.get("passed")) and not findings
    out.update(passed=passed, findings=findings, accuracy_pass=passed, has_inaccuracy=not passed)
    return out


async def _correct_recap(
    title: str, body: str, findings: list[Any], facts: str
) -> Optional[dict[str, Any]]:
    """Rewrite the draft to fix fact-check findings, using only the FACTS."""
    prompt = (
        f"FACTS:\n{facts}\n\n"
        f"DRAFT TITLE:\n{title}\n\nDRAFT BODY:\n{body}\n\n"
        f"FACT-CHECK ISSUES TO FIX:\n{json.dumps(findings)}\n\n"
        "Rewrite the recap so every factual claim matches the FACTS exactly. "
        "Keep it 220-320 words, keep the same editorial voice, do not add new "
        'facts. Return STRICT JSON: {"title": "...", "body": "..."}.'
    )
    raw, _, _ = await _call_llm(prompt, system=SYSTEM_PROMPT, max_tokens=1800)
    return _parse_json(raw or "")


def _build_prompt(
    sport: str,
    game: dict[str, Any],
    writeup: dict[str, Any],
    pred: dict[str, Any],
    snippets: list[dict[str, Any]],
    player_lines: str = "",
) -> str:
    cfg = SPORTS[sport]
    _d = game.get("game_date_et") or _et_date(game.get("date"))
    date_str = _d.strftime("%A, %B %-d, %Y") if hasattr(_d, "strftime") else ""
    fact_lines = [
        f"- League: {cfg['league']}",
        f"- Date: {date_str}",
        f"- Matchup: {game['away_name']} (away) at {game['home_name']} (home)",
        f"- Final score: {game['away_name']} {game['away_score']}, "
        f"{game['home_name']} {game['home_score']}",
    ]
    picks = _fmt_picks(sport, pred)
    if picks:
        fact_lines.append("- Our pre-game model calls:")
        fact_lines.extend(f"    * {p}" for p in picks)
    else:
        fact_lines.append("- Our pre-game model calls: none recorded")

    blocks = ["FACTS:", "\n".join(fact_lines)]

    if writeup.get("premium_content"):
        blocks.append(
            "OUR PREMIUM PICK (what we sold subscribers before the game):\n"
            + _clip(writeup["premium_content"])
        )
    if writeup.get("prop_content"):
        blocks.append(
            "OUR PROP PLAYS (what we told subscribers before the game):\n"
            + _clip(writeup["prop_content"])
        )
    if player_lines:
        blocks.append(player_lines)
    if snippets:
        lines = [f"    * [{s['source'] or 'news'}] {s['title']}" for s in snippets]
        blocks.append(
            "SCRAPED NEWS HEADLINES ABOUT THIS GAME (context only, may be noisy):\n"
            + "\n".join(lines)
        )

    blocks.append(
        """TASK:
Write a short post-game recap article. Requirements:
1. Length: 220-320 words for the body.
2. Open with the result and the defining storyline of the game.
3. Grade OUR premium pick and prop plays using the data above: which hit, which missed, and by how much. If a specific prop's result cannot be determined from the data above, simply leave that prop out — never mention that data is missing.
4. Close with what the result means for future picks (a takeaway, not a promise).
5. Use ONLY the data provided; never invent stats, quotes, or plays.
6. Write for the reader only. NEVER reference "the facts", your data, sources, projections, or instructions — and never say what you do or do not have.
7. SEO friendly but natural; no keyword stuffing.

Return STRICT JSON only (no markdown fences) with exactly these keys:
{
  "title": "compelling, keyword-rich headline (max 70 chars, no site name)",
  "summary": "1-2 sentence dek for listings (max 220 chars)",
  "body": "the recap as GitHub-flavored Markdown, starting with a short intro paragraph (no H1)",
  "seo_description": "meta description (max 155 chars)",
  "seo_keywords": "6-10 comma-separated keywords"
}"""
    )
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# core
# ---------------------------------------------------------------------------
async def find_candidates(
    session: AsyncSession,
    sport: str,
    *,
    lookback_hours: int = 36,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Final games from the last ``lookback_hours`` that have a writeup and no recap yet.

    "Next morning" semantics: only games whose ET calendar date is strictly
    before today's ET date (i.e. completed yesterday or earlier).
    """
    cfg = SPORTS[sport]
    sql = text(
        f"""
        SELECT g.id, g.date, g.home_score, g.away_score,
               (g.date AT TIME ZONE 'America/New_York')::date AS game_date_et,
               ht.abbreviation AS home_abbr, ht.name AS home_name,
               at.abbreviation AS away_abbr, at.name AS away_name
        FROM {cfg['games']} g
        JOIN {cfg['teams']} ht ON ht.id = g.home_team_id
        JOIN {cfg['teams']} at ON at.id = g.away_team_id
        WHERE g.status::text = 'FINAL'
          AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
          AND g.date > now() - make_interval(hours => :hours)
          AND (g.date AT TIME ZONE 'America/New_York')::date < :run_date
          AND (g.game_type IS NULL OR g.game_type::text = ANY(:game_types))
          AND EXISTS (
              SELECT 1 FROM {cfg['writeups']} w
              WHERE w.game_id = g.id
                AND (w.premium_content IS NOT NULL
                     OR w.prop_content IS NOT NULL
                     OR w.public_content IS NOT NULL)
          )
          AND NOT EXISTS (
              SELECT 1 FROM public.original_articles oa
              WHERE oa.section = 'recap' AND oa.sport = :sport AND oa.game_id = g.id
          )
        ORDER BY g.date DESC
        """
    )
    params: dict[str, Any] = {
        "hours": lookback_hours,
        "run_date": datetime.now(ET).date(),
        "sport": sport,
        "game_types": cfg.get("game_type_allow"),
    }
    if limit:
        sql = text(str(sql) + "\nLIMIT :limit")
        params["limit"] = limit
    rows = (await session.execute(sql, params)).fetchall()
    return [dict(r._mapping) for r in rows]


_INSERT_SQL = """
INSERT INTO public.original_articles
    (sport, title, summary, content, slug, seo_description, seo_keywords,
     teams, section, status, visibility, author, game_id,
     research_json, prompt_json, accuracy_check, accuracy_check_tokens,
     usage_json, tokens_used, published_at, created_at, updated_at)
VALUES
    (:sport, :title, :summary, :content, :slug, :seo_description, :seo_keywords,
     CAST(:teams AS jsonb), 'recap', 'published', 'public', 'Earl', :game_id,
     CAST(:research_json AS jsonb), CAST(:prompt_json AS jsonb), CAST(:accuracy_check AS json),
     :accuracy_check_tokens, CAST(:usage_json AS jsonb), :tokens_used, now(), now(), now())
ON CONFLICT DO NOTHING
RETURNING id
"""


async def _insert_recap(session: AsyncSession, record: dict[str, Any]) -> Optional[int]:
    """Insert a recap; retry once with a disambiguated slug on a slug collision."""
    row = (await session.execute(text(_INSERT_SQL), record)).fetchone()
    if row is None:
        record = {**record, "slug": f"{record['slug']}-{record['game_id']}"}
        row = (await session.execute(text(_INSERT_SQL), record)).fetchone()
    return row[0] if row else None


async def _generate_one(
    session: AsyncSession, sport: str, game: dict[str, Any], *, dry_run: bool = False
) -> Optional[dict[str, Any]]:
    cfg = SPORTS[sport]
    game_id = game["id"]

    # --- research: pull the authoritative facts and source material -------
    wrow = (
        await session.execute(
            text(f"SELECT * FROM {cfg['writeups']} WHERE game_id = :g ORDER BY id DESC LIMIT 1"),
            {"g": game_id},
        )
    ).fetchone()
    writeup = dict(wrow._mapping) if wrow else {}

    prow = (
        await session.execute(
            text(f"SELECT * FROM {cfg['preds']} WHERE game_id = :g ORDER BY id DESC LIMIT 1"),
            {"g": game_id},
        )
    ).fetchone()
    pred = dict(prow._mapping) if prow else {}

    snippets = await _scraped_snippets(
        session, sport, game["home_name"], game["away_name"], game.get("date")
    )
    blob = "\n".join(
        filter(None, [writeup.get("prop_content"), writeup.get("premium_content")])
    )
    player_lines = await _player_lines_block(session, sport, game_id, blob)
    facts = _facts_text(sport, game, writeup, pred, player_lines)
    research_trace = _research_trace(sport, game, writeup, pred, snippets, facts)

    prompt = _build_prompt(sport, game, writeup, pred, snippets, player_lines)
    raw, gen_tokens, gen_usage = await _call_llm(prompt)
    parsed = _parse_json(raw or "")
    if not parsed or not parsed.get("body"):
        log.error("recap LLM output unusable for %s game %s", sport, game_id)
        return None

    title = (parsed.get("title") or "").strip()
    body = (parsed.get("body") or "").strip()
    summary = (parsed.get("summary") or "").strip()[:300]
    seo_desc = (parsed.get("seo_description") or "").strip()[:300]
    seo_kw = (parsed.get("seo_keywords") or "").strip()

    if not title:
        title = (
            f"{game['away_name']} at {game['home_name']} Recap: "
            f"{game['away_abbr']} {game['away_score']}, {game['home_abbr']} {game['home_score']}"
        )

    # --- fact-check against the authoritative facts + bounded correction --
    accuracy = await _verify_recap(title, body, facts)
    retries = 0
    while retries < 2 and (accuracy.get("findings") or _META_PATTERNS.search(body)):
        findings = list(accuracy.get("findings") or [])
        if _META_PATTERNS.search(body):
            findings.append(_META_FINDING)
        corrected = await _correct_recap(title, body, findings, facts)
        retries += 1
        if corrected and corrected.get("body"):
            title = (corrected.get("title") or title).strip()
            body = corrected["body"].strip()
        accuracy = await _verify_recap(title, body, facts)
    accuracy["retries_used"] = retries
    accuracy["has_inaccuracy"] = bool(accuracy.get("findings"))

    game_date = game.get("game_date_et") or _et_date(game.get("date")) or datetime.now(ET).date()
    slug_base = (
        f"{game_date.isoformat()}-{_slugify(game['away_name'])}-at-"
        f"{_slugify(game['home_name'])}-recap"
    )
    teams_json = json.dumps([game["away_abbr"], game["home_abbr"]])
    verify_tokens = int(accuracy.get("tokens") or 0)

    record = {
        "sport": sport,
        "title": title,
        "summary": summary,
        "content": body,
        "slug": slug_base,
        "seo_description": seo_desc,
        "seo_keywords": seo_kw,
        "teams": teams_json,
        "game_id": game_id,
        "word_count": len(body.split()),
        "research_json": json.dumps(research_trace),
        "prompt_json": json.dumps({"prompt": prompt}),
        "accuracy_check": json.dumps(accuracy),
        "accuracy_check_tokens": verify_tokens,
        "usage_json": json.dumps(
            {"generation": gen_usage, "verification_tokens": verify_tokens}
        ),
        "tokens_used": int(gen_tokens) + verify_tokens,
    }

    if dry_run:
        log.info(
            "[dry-run] %s game %s -> '%s' accuracy_pass=%s findings=%d",
            sport, game_id, title, accuracy.get("accuracy_pass"),
            len(accuracy.get("findings") or []),
        )
        return record

    article_id = await _insert_recap(session, record)
    if article_id is None:
        log.warning("recap insert skipped (already exists) for %s game %s", sport, game_id)
        return None

    await session.commit()
    record["article_id"] = article_id
    log.info(
        "recap %s game %s -> article %s '%s' (%s words, accuracy_pass=%s, retries=%s)",
        sport, game_id, article_id, title, record["word_count"],
        accuracy.get("accuracy_pass"), accuracy.get("retries_used"),
    )
    return record


async def run_recaps(
    sport: str,
    *,
    lookback_hours: int = 36,
    limit: Optional[int] = None,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """Generate recaps for one sport. Returns the list of created recaps."""
    from app.database import async_session

    if sport not in SPORTS:
        raise ValueError(f"unknown sport {sport!r}; expected one of {sorted(SPORTS)}")

    created: list[dict[str, Any]] = []
    async with async_session() as session:
        candidates = await find_candidates(
            session, sport, lookback_hours=lookback_hours, limit=limit
        )
        log.info("%s: %d recap candidate(s)", sport, len(candidates))
        for game in candidates:
            try:
                rec = await _generate_one(session, sport, game, dry_run=dry_run)
            except Exception:  # noqa: BLE001 - one bad game must not kill the run
                log.exception("recap failed for %s game %s", sport, game.get("id"))
                await session.rollback()
                continue
            if rec:
                created.append(rec)
    return created


async def run_all_sports(**kwargs: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for sport in SPORTS:
        out[sport] = await run_recaps(sport, **kwargs)
    return out
