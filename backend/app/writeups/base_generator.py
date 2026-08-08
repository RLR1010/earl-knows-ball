"""Base write-up generator — DeepSeek integration, prompt templates, QC."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any, Optional

from json_repair import repair_json
from openai import AsyncOpenAI

from app.core.config import settings

logger = logging.getLogger("writeups")

# ── Quality check result types ─────────────────────────────────────

QCResult = dict[str, Any]  # {check_name: str, passed: bool, detail: str}
QCResults = list[QCResult]


# ── Base Generator ─────────────────────────────────────────────────

class BaseWriteupGenerator(ABC):
    """Shared generation logic for all sports.

    Subclasses implement *research_brief()* and *prompt_builder()*.
    """

    # DeepSeek model to use
    MODEL = "deepseek-v4-flash"
    TEMPERATURE = 0.5  # moderate creativity for sports writing
    MAX_TOKENS = 24576  # fallback when a call omits max_tokens; PUBLIC=24576, PREMIUM=32768 (4k-6k words total)
    TIMEOUT = 120.0  # generous for longer generation
    # Retry policy for DeepSeek calls. Empty responses are a known DeepSeek
    # behavior when thinking mode burns the whole max_tokens budget on
    # reasoning — retry, then fall back to a no-thinking call.
    MAX_DEEPSEEK_ATTEMPTS = 3
    DEEPSEEK_BACKOFF_BASE = 2.0  # seconds; attempt n waits base * n

    # ── Subclass hooks ──────────────────────────────────────

    @abstractmethod
    async def research_brief(self, game_id: int, as_of_date: datetime | None = None) -> dict[str, Any]:
        """Return the full research dict for this game."""
        ...

    @abstractmethod
    def sport_context(self) -> str:
        """Return a description of the sport for the system prompt
        (e.g. 'Major League Baseball', 'National Football League')."""
        ...

    def system_prompt(self, is_historical: bool = False) -> str:
        """System prompt shared by all generations."""
        tense_note = (
            "CRITICAL: This is a HISTORICAL write-up. The game has already been played "
            "but the article must be written as if it hasn't happened yet. "
            "DO NOT mention the actual result, final score, or anything that happened in the game. "
            "Write entirely in future/present tense as if previewing an upcoming game. "
            "Use phrases like 'will face', 'looks to', 'enters this game'. "
            "Never use 'won', 'lost', 'defeated', 'victory', or any past-tense outcome language."
        ) if is_historical else (
            "This is a PREVIEW for an upcoming game. Write in present/future tense."
        )

        return f"""You are a senior sports writer for Earl Knows Ball, the premier handicapping and analysis platform for {self.sport_context()}. Your writing style is professional, insightful, and engaging — think a mix of ESPN insider analysis and a sharp beat writer.

You will be given structured research data about an upcoming game. Your job is to write TWO versions of a game preview article.


⚠️ PREMIUM CONTENT RULES (STRICT):
- Premium content must offer genuine ADDITIONAL insight beyond public content.
- Good premium content: advanced stats breakdown, key matchup analysis, in-depth handicapping angle, betting trend with historical context, coaching strategy deep-dive.
- Bad premium content: rephrasing the public section, generic filler, content that would be obvious to any casual fan.
- If you cannot think of genuinely premium-worthy content, focus on one key matchup or betting angle and explain it exhaustively.
- Premium content should feel like you're giving the reader a real edge.

PUBLIC CONTENT (also required):
- Great for broad overview, team context, what to watch for.
- Must still be informative and well-written, not just generic.
- Should make the reader feel informed and excited about the game.

⚠️ STRICT ACCURACY RULES (VIOLATION WILL BE REJECTED):
- EVERY player you mention MUST appear in the RESEARCH DATA section below.
- If a player is NOT in the research data, do NOT mention them by name.
- Do NOT reference players who have been traded, released, or retired based on your training data.
- Only cite statistics that are explicitly provided in the research data.
- Do NOT fabricate statistics, percentages, or historical comparisons.
- Stick to what is in the research. If you are unsure, leave it out.

OUTPUT FORMAT:
Return ONLY valid JSON with the following fields:
{{
    "title": "Engaging article title (include team names, max ~80 chars)",
    "public_content": "Full public article text (800-1000 words, several paragraphs - be detailed and comprehensive)",
    "premium_content": "Full premium analysis text (1100-1500 words, several paragraphs - be detailed and comprehensive)"
}}

{tense_note}

CONTENT FORMATTING: Use markdown inside both content fields. `##` headings to organize sections, `**` for emphasis, bullet lists for key points. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Pitcher | Record | ERA | WHIP |
|---------|--------|-----|------|
| Gavin Williams (R) | 10-4 | 3.81 | 1.15 |

Keep it article-like — no blockquotes, no emoji, no chat-style formatting.

Return valid JSON only. No markdown fences. No extra text."""

    def public_system_prompt(self, is_historical: bool = False) -> str:
        """System prompt for the public-only writeup (no picks, no betting data).

        Uses markdown/plain text output (not JSON) since there is only one
        content section and we want a natural article format.
        """
        tense_note = (
            "CRITICAL: This is a HISTORICAL write-up. The game has already been played "
            "but the article must be written as if it hasn't happened yet. "
            "DO NOT mention the actual result, final score, or anything that happened in the game. "
            "Write entirely in future/present tense as if previewing an upcoming game. "
            "Use phrases like 'will face', 'looks to', 'enters this game'. "
            "Never use 'won', 'lost', 'defeated', 'victory', or any past-tense outcome language."
        ) if is_historical else (
            "This is a PREVIEW for an upcoming game. Write in present/future tense."
        )

        return f"""You are a baseball writer for Earl Knows Ball, a sports analysis site. Write a game preview/article for the general public.

Length: 800-1000 words.

Focus on:
- Game narrative and stakes (division race, wild card implications, streaks)
- Team context and recent storylines
- Pitching matchup highlights (ERA, recent outings, velocity trends — skip deep batter-vs-pitcher tables)
- Key player storylines (who's hot, who's slumping, milestones, returns from IL)
- Basic venue and weather context
- High-level injury notes

Do NOT include:
- Betting odds, lines, spreads, totals, or moneyline numbers
- Implied public betting percentages
- ATS splits or any ATS/OU record references
- Any handicapping predictions, model picks, or edge calculations
- Line movement data

⚠️ STRICT ACCURACY RULES (VIOLATION WILL BE REJECTED):
- EVERY player you mention MUST appear in the RESEARCH DATA section below.
- If a player is NOT in the research data, do NOT mention them by name.
- Do NOT reference players who have been traded, released, or retired based on your training data.
- Only cite statistics that are explicitly provided in the research data.
- Do NOT fabricate statistics, percentages, or historical comparisons.
- Stick to what is in the research. If you are unsure, leave it out.

This is a game preview — not a betting analysis. Write in the style of a well-informed beat writer: insightful, engaging, and authoritative.

{tense_note}

FORMATTING: This renders as a web article via markdown. Use `##` for the title on line 1. Use `##` section headers to organize the body. Use `**` for emphasis sparingly. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Pitcher | Record | ERA | WHIP |
|---------|--------|-----|------|
| Gavin Williams (R) | 10-4 | 3.81 | 1.15 |

Bullet lists work for key points. Keep it article-like — no blockquotes, no emoji, no chat-style formatting."""

    def premium_system_prompt(self, is_historical: bool = False) -> str:
        """System prompt for the premium-only (insider) writeup.

        This is called AFTER the public writeup is done, with the full research
        brief that includes betting lines, splits, model predictions, etc.
        Same format as public: first line is the title, then blank line, then content. No JSON.
        """
        tense_note = (
            "CRITICAL: This is a HISTORICAL write-up. The game has already been played. "
            "Do NOT mention the actual result or final score — this is a post-game "
            "handicapping analysis, not a recap. Focus on how the game played out "
            "relative to the betting lines, what moved, and lessons for future games."
            if is_historical else (
                "This is a PREVIEW for an upcoming game. Write in present/future tense."
            )
        )

        return f"""You are a senior sports writer for Earl Knows Ball, the premier handicapping and analysis platform. Your writing style is professional, insightful, and engaging — think a mix of ESPN insider analysis and a sharp beat writer.

Write an exclusive insider analysis article for PAYING SUBSCRIBERS. This is a full article, not a short snippet.

Length: 1100-1500 words — be detailed and comprehensive.

What to include:
- Advanced stats breakdown and key matchup analysis
- In-depth handicapping angles with historical context
- Betting trends, line movement analysis, and what it means
- CRITICAL: Use Earl's MODEL PREDICTIONS from the "--- MODEL PREDICTIONS ---" section below as your FOUNDATION for picks. Lead with: "Earl's model says..." or "Our model sees edge on..." Do NOT recommend the opposite side.
- The "--- MODEL SHAP DRIVERS ---" section explains WHY the model leans the way it does: each feature's contribution, in runs (ATS = home margin, positive favors home; OU = total runs, positive favors Over). Use the top drivers to explain the model's reasoning — e.g. "the model's lean is driven mainly by the home pitcher's recent form (largest single driver, -0.4 runs)". DO NOT claim a negative contribution means a team is bad or will lose, and do NOT present correlated features as independent causes. These are per-game attributions, not team-quality statements.
- Explicit betting recommendations where supported by the data
- Why the public is wrong vs right
- Proprietary handicapping insights that give the reader a real edge
- Coaching strategy deep-dives when relevant

⚠️ RULES:
- This content must offer genuine ADDITIONAL insight. It must be different from the public preview.
- Good: deep breakdown of one key matchup, betting angle with context, proprietary edge analysis
- Bad: rephrasing the public section, generic filler, content obvious to any casual fan
- If you cannot think of genuinely premium-worthy content, focus on one key angle and explain it exhaustively
- Premium content should feel like you're giving the reader a real edge they can't get elsewhere

OUTPUT FORMAT: Start with the article TITLE on its own line (use `##` as a heading).
Then a blank line. Then the full article formatted in markdown.
This renders as a web article, so use markdown appropriate for publishing:
- `##` section headers to organize the analysis
- `**` for emphasis on key numbers/angles
- Tables using proper pipe-and-dash markdown syntax with a separator row:
  | Pitcher | Record | ERA | WHIP |
  |---------|--------|-----|------|
  | Gavin Williams (R) | 10-4 | 3.81 | 1.15 |
- Bullet lists for key points
- Keep it article-like — no blockquotes, no emoji, no chat-style formatting

Example:
## The Javier Conundrum

On paper, this looks like a battle of two middling AL West teams with losing June records...

⚠️ STRICT ACCURACY RULES (VIOLATION WILL BE REJECTED):
- EVERY player you mention MUST appear in the RESEARCH DATA section below.
- If a player is NOT in the research data, do NOT mention them by name.
- Do NOT reference players who have been traded, released, or retired based on your training data.
- Only cite statistics that are explicitly provided in the research data.
- Do NOT fabricate statistics, percentages, or historical comparisons.
- Stick to what is in the research. If you are unsure, leave it out.
{tense_note}"""

    # ── Generation ──────────────────────────────────────────

    async def generate(
        self,
        game_id: int,
        is_historical: bool | None = None,  # deprecated — now read from research
        as_of_date: datetime | None = None,
        reasoning: str = "minimal",  # thinking enabled + minimal reasoning (works; ~1k reas tokens)
        usage_log: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Generate a write-up for the given *game_id*.

        Makes TWO separate LLM calls:
          1. Public call  — stripped research, no betting data, plain text
          2. Premium call — full research with picks, JSON output

        is_historical is now determined from the game's status in the
        research brief. The parameter is kept for backward compat.

        Returns the dict with keys: *title*, *public_content*, *premium_content*,
        *title_brief*, *research_brief*, *is_historical*, *qc_results*.
        """
        # Normalize reasoning:
        #   None/""/"off"/"none"/"disabled" → no enabled-thinking send.
        #   "minimal"/"low"/"medium"/"high"/"xhigh"/"max" → thinking.enabled + reasoning_effort.
        # NOTE: API rejects "off" (invalid variant). "none" burns ~15k reasoning
        # tokens and broke writeups with empty responses. "minimal" is the
        # working "off-but-minimal" value (~1.1k reas tokens).
        if reasoning in (None, "", "off", "none", "disabled"):
            reasoning = None
        logger.info("generating write-up for game_id=%s", game_id)

        # ---- 1. Full research is fetched once ----
        research = await self.research_brief(game_id, as_of_date)
        if "error" in research:
            logger.warning("research_brief failed for game %s: %s", game_id, research["error"])
            return {"error": research["error"]}

        is_historical = research.get("is_historical", bool(is_historical))

        # ---- 2A. Public call — stripped research, no betting data ----
        stripped = dict(research)
        for key in ("betting_lines", "predictions", "model_predictions", "shap_digest", "home_splits", "away_splits"):
            stripped.pop(key, None)

        public_system = self.SHARED_SYSTEM
        # Shared system + STRIPPED research prefix, then the public write task.
        # Byte-identical prefix with the public accuracy lane -> cache hit, and
        # the public writeup never sees our picks (stripped data + no-bets rules).
        public_prompt = (
            self._build_messages(stripped) + "\n\n"
            "=== WRITE THE PUBLIC GAME PREVIEW ===\n"
            + self.public_system_prompt(is_historical)
            + "\n\n" + self.SEO_OUTPUT_INSTRUCTION
        )

        raw_public = await self._call_deepseek(public_system, public_prompt, max_tokens=24576, reasoning=reasoning, usage_log=usage_log)
        if raw_public is None:
            return {"error": "DeepSeek API call failed for public section"}

        # Parse title + body from public response
        raw_public, seo_from_public = self._extract_seo_block(raw_public)
        pub_lines = raw_public.strip().split("\n", 1)
        title = pub_lines[0].strip().strip("#").strip() if pub_lines else ""
        public_content = pub_lines[1].strip() if len(pub_lines) > 1 else ""
        seo_description = seo_from_public.get("seo_description")
        seo_keywords = seo_from_public.get("seo_keywords")

        # ---- 2B. Premium call — full research with picks ----
        premium_system = self.SHARED_SYSTEM
        # Shared system + FULL research prefix, then the premium write task.
        # Byte-identical prefix with the premium accuracy lane -> cache hit.
        premium_prompt = (
            self._build_messages(research) + "\n\n"
            "=== WRITE THE PREMIUM ANALYSIS ===\n"
            + self.premium_system_prompt(is_historical)
        )

        # NOTE: deepseek-v4-flash is a reasoning model — it spends a large
        # chunk of max_tokens on reasoning_content even without the thinking
        # flag (observed ~7k tokens of reasoning on a 26k-char research brief).
        # Budgets below 16k caused empty premium content (finish=length with
        # all tokens consumed by reasoning). 32768 gives ample headroom.
        raw_premium = await self._call_deepseek(premium_system, premium_prompt, max_tokens=32768, reasoning=reasoning, usage_log=usage_log)
        if raw_premium is None:
            logger.warning("premium LLM call failed for game %s — using fallback", game_id)
            premium_content = "Premium content unavailable — API call failed."
            premium_title_brief = ""
        else:
            # Parse JSON premium response
            premium_parsed = self._parse_premium_response(raw_premium)
            premium_content = premium_parsed.get("content", "")
            premium_title_brief = premium_parsed.get("title", "")

        # ---- 3. Assemble final result ----
        parsed = {
            "title": title,
            "title_brief": premium_title_brief,
            "public_content": public_content,
            "premium_content": premium_content,
            "research_brief": research,
            "is_historical": is_historical,
        }

        # ---- 3b. SEO meta (description + keywords) ----
        # Prefer SEO parsed from the public-generation response (no extra LLM
        # call). Fall back to _generate_seo only if unusable.
        seo_desc = (seo_description or "").strip()[:500]
        seo_kw = (seo_keywords or "").strip()[:500]
        if not seo_desc:
            _seo = await self._generate_seo(
                title or "",
                (public_content or "") + "\n" + (premium_content or ""),
                usage_log=usage_log,
            )
            seo_desc = (_seo.get("seo_description") or "").strip()[:500]
            seo_kw = (_seo.get("seo_keywords") or "").strip()[:500]
        parsed["seo_description"] = seo_desc or None
        parsed["seo_keywords"] = seo_kw or None

        # ---- 4. Quality checks ----
        qc_results = self.run_quality_checks(parsed, research)
        parsed["qc_results"] = qc_results

        # ---- 4b. Accuracy verification (final fact-check) ----
        # Verify every fact/stat/name in the article is traceable to the research
        # brief, and that the PUBLIC section contains no betting predictions.
        # Bounded fix-loop: at most 1 revision pass if findings surface.
        accuracy_check = await self._verify_accuracy(
            parsed.get("public_content", ""),
            parsed.get("premium_content", ""),
            research,
            research_prefix=self._build_messages(research),
            usage_log=usage_log,
        )
        # Correction loop: up to MAX_CORRECTION_PASSES attempts to fix
        # accuracy findings, re-verifying after each pass. If we can't get it
        # clean, we keep the best-effort corrected version and let the
        # ``accuracy_check`` tell the listing that an inaccuracy remains — we
        # do NOT try to programmatically strip claims (unverified edits are
        # riskier than a visible flag).
        retries_used = 0
        max_passes = getattr(self, "MAX_CORRECTION_PASSES", 2)
        has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")
        rejection_history = list(parsed.get("rejection_history") or [])
        while has_findings and retries_used < max_passes:
            # Snapshot the failing draft + the accuracy findings that caught it,
            # BEFORE overwriting with the corrected version.
            rejection_history.append({
                "attempt": retries_used + 1,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "accuracy_check": accuracy_check,
                "public_content": parsed.get("public_content", ""),
                "premium_content": parsed.get("premium_content", ""),
            })
            corrected = await self._correct_article(
                parsed.get("public_content", ""),
                parsed.get("premium_content", ""),
                research,
                accuracy_check.get("findings") or [],
                usage_log=usage_log,
            )
            retries_used += 1
            if not corrected:
                break
            if corrected.get("public_content"):
                parsed["public_content"] = corrected["public_content"]
            if corrected.get("premium_content"):
                parsed["premium_content"] = corrected["premium_content"]
            parsed["title"] = corrected.get("title") or parsed.get("title")
            # Re-run QC + accuracy on the corrected article.
            qc_results = self.run_quality_checks(parsed, research)
            parsed["qc_results"] = qc_results
            accuracy_check = await self._verify_accuracy(
                parsed.get("public_content", ""),
                parsed.get("premium_content", ""),
                research,
                research_prefix=self._build_messages(research),
                usage_log=usage_log,
            )
            has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")

        accuracy_check["retries_used"] = retries_used
        # Surface any remaining inaccuracies so the admin listing can flag it.
        accuracy_check["has_inaccuracy"] = has_findings
        accuracy_check["accuracy_pass"] = not has_findings
        parsed["accuracy_check"] = accuracy_check
        parsed["accuracy_check_tokens"] = accuracy_check.get("tokens") or 0
        parsed["rejection_history"] = rejection_history

        # ---- Slug (deterministic: YYYY-MM-DD-<title>) ----
        gd = None
        try:
            gd = research.get("game_summary", {}).get("date")
        except Exception:  # noqa: BLE001
            gd = None
        parsed["slug"] = self._derive_slug(game_id, parsed.get("title", ""), gd)

        # ---- Test instrumentation ----
        # Used for A/B reasoning comparisons.
        if usage_log is not None:
            parsed["reasoning_effort"] = reasoning
            parsed["usage_log"] = usage_log
            token_totals = [
                item["total_tokens"]
                for item in usage_log
                if isinstance(item, dict) and isinstance(item.get("total_tokens"), int)
            ]
            parsed["total_tokens"] = sum(token_totals) if token_totals else 0
        else:
            parsed["total_tokens"] = 0

        # ---- 5. Store ----
        await self.store(game_id, parsed, qc_results)

        logger.info(
            "write-up %s for game %s — qc=%s/%s passed",
            title or "(no title)",
            game_id,
            sum(1 for q in qc_results if q.get("passed")),
            len(qc_results),
        )
        return parsed

    def _parse_premium_response(self, raw: str) -> dict[str, str]:
        """Parse premium response.

        Handles two formats:
          1. Plain text — first line = title, rest = content.
          2. JSON-like — the LLM sometimes returns JSON despite being told not to.
        """
        cleaned = raw.strip()

        # Detect JSON-like response: starts with "title": or {
        if cleaned.startswith('"title":') or cleaned.startswith('{'):
            import json
            # Try full JSON parse first
            try:
                parsed = json.loads(cleaned)
                if isinstance(parsed, dict) and ("title" in parsed or "content" in parsed):
                    t = parsed.get("title", "").strip()
                    c = parsed.get("content", "").strip()
                    return {"title": t, "content": c}
            except (json.JSONDecodeError, ValueError, TypeError):
                pass

            # Fallback: manual extraction of "title" and "content" keys
            import re
            title_match = re.search(r'"title"\s*:\s*"(.*?)"(?:[,\n]|$)', cleaned, re.DOTALL)
            content_match = re.search(r'"content"\s*:\s*"(.*?)"$', cleaned, re.DOTALL)
            title = title_match.group(1).strip() if title_match else ""
            content = content_match.group(1).strip() if content_match else cleaned
            # Unescape internal quotes
            title = title.replace('\\"', '"').replace('\\n', '\n')
            content = content.replace('\\"', '"').replace('\\n', '\n')
            return {"title": title, "content": content}

        # Plain text format: first line = title, blank line, then content
        lines = cleaned.split("\n", 1)
        title = lines[0].strip().strip("#").strip() if lines else ""
        content = lines[1].strip() if len(lines) > 1 else cleaned
        return {"title": title, "content": content}

    async def _call_deepseek(self, system: str, user_prompt: str, *, max_tokens: int | None = None, reasoning: str | None = None, usage_log: list[dict[str, Any]] | None = None, max_attempts: int | None = None) -> str | None:
        """Call DeepSeek via OpenAI SDK and return the raw response content.

        Retries with backoff on API errors and empty responses. A known
        DeepSeek failure mode is an HTTP 200 with empty ``content`` when
        thinking mode consumes the entire max_tokens budget on reasoning
        tokens — so if every thinking-enabled attempt comes back empty, one
        final attempt is made with thinking disabled to guarantee content.

        Returns *None* on failure — caller checks for None.

        If *usage_log* is provided (a list), each successful attempt appends a
        dict with timing + token usage stats: ``{"attempt", "reasoning",
        "elapsed_s", "prompt_tokens", "completion_tokens", "reasoning_tokens",
        "total_tokens"}``.
        """
        attempts_limit = (
            max_attempts
            if max_attempts is not None
            else getattr(self, "MAX_DEEPSEEK_ATTEMPTS", 3)
        )
        backoff = getattr(self, "DEEPSEEK_BACKOFF_BASE", 2.0)

        async def _attempt(use_reasoning: str | None) -> str | None:
            """Single API call; returns content string or None."""
            start = time.monotonic()
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=f"{settings.deepseek_base_url}/v1",
            )
            kwargs: dict[str, Any] = {
                "model": self.MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": self.TEMPERATURE,
                "max_tokens": max_tokens or self.MAX_TOKENS,
                "timeout": self.TIMEOUT,
            }
            if use_reasoning == "disabled":
                # True thinking off: explicitly disable. This differs from
                # omitting the block (None), which lets DeepSeek use its default
                # and still run hidden/reasoned CoT (observed off had 10.7k
                # reasoning tokens).
                kwargs["extra_body"] = {
                    "thinking": {"type": "disabled"},
                }
            elif use_reasoning:
                kwargs["extra_body"] = {
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": use_reasoning,
                }
            response = await client.chat.completions.create(**kwargs)
            elapsed_s = round(time.monotonic() - start, 2)
            content = response.choices[0].message.content
            if usage_log is not None:
                usage = response.usage
                usage_log.append({
                    "attempt": len(usage_log) + 1,
                    "reasoning": use_reasoning,
                    "elapsed_s": elapsed_s,
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
                    "reasoning_tokens": (
                        getattr(
                            getattr(usage, "completion_tokens_details", None),
                            "reasoning_tokens",
                            0,
                        )
                        or 0
                    ),
                    "total_tokens": getattr(usage, "total_tokens", 0) or 0,
                })
            if not content or not content.strip():
                return None
            return content

        last_error: Exception | None = None
        empty_attempts = 0

        for attempt in range(1, attempts_limit + 1):
            try:
                content = await _attempt(reasoning)
                if content is not None:
                    return content
                empty_attempts += 1
                logger.warning(
                    "DeepSeek returned empty response (attempt %d/%d)%s — %s",
                    attempt,
                    attempts_limit,
                    f" with reasoning={reasoning!r}" if reasoning else "",
                    "retrying" if attempt < attempts_limit else "giving up",
                )
            except Exception as e:
                last_error = e
                logger.warning("DeepSeek API call failed (attempt %d/%d): %s", attempt, attempts_limit, e)
            if attempt < attempts_limit:
                await asyncio.sleep(backoff * attempt)

        # Fallback: thinking mode likely ate the whole token budget. Try once
        # without thinking to guarantee content comes back.
        if reasoning and empty_attempts == attempts_limit:
            logger.warning("DeepSeek empty responses with thinking enabled — retrying without thinking")
            for fb_attempt in range(1, attempts_limit + 1):
                try:
                    content = await _attempt(None)
                    if content is not None:
                        return content
                except Exception as e:
                    last_error = e
                    logger.error("DeepSeek API call failed on no-thinking fallback: %s", e)
                logger.warning("DeepSeek no-thinking fallback empty (attempt %d/%d)", fb_attempt, attempts_limit)
                if fb_attempt < attempts_limit:
                    await asyncio.sleep(backoff * fb_attempt)
            logger.error("DeepSeek returned empty response after %d attempts (incl. no-thinking fallback)", attempts_limit)
            return None

        if empty_attempts == attempts_limit:
            logger.error("DeepSeek returned empty response after %d attempts", attempts_limit)
        else:
            logger.error("DeepSeek API call failed after %d attempts: %s", attempts_limit, last_error)
        return None

    # ── SEO Meta Generation ────────────────────────────────

    SEO_PROMPT = (
        "You are an SEO specialist for Earl Knows Ball, a premium sports handicapping "
        "site. Given a sports betting article's title and content, produce a compelling "
        "meta description and a keyword list.\n"
        "Rules:\n"
        "- Meta description: 140-160 chars, 1-2 punchy sentences that summarize and "
        "  entice clicks. Plain text, no quotes, no trailing period if it exceeds the "
        "  limit. Betting-focused.\n"
        "- Keywords: a comma-separated list of 5-8 lowercase SEO phrases a bettor would "
        "  search, e.g. 'mlb betting picks, padres vs dodgers, over under odds, "
        "  sportsbook analysis'. No spaces after commas, no trailing comma.\n"
        "- Return ONLY JSON: {\"seo_description\": \"...\", \"seo_keywords\": "
        "\"...\"}. No markdown fences, no commentary."
    )

    async def _generate_seo(
        self, title: str, content: str, *, usage_log: Optional[list] = None
    ) -> dict[str, str]:
        """Return {seo_description, seo_keywords}: a meta-description + keywords.

        Uses the shared ``_call_deepseek`` (retry + reason-aware) so SEO is never
        blank because of a single flaky call, and its tokens are counted in
        ``usage_log``. Falls back to a title-derived description if the model is
        unreachable.
        """
        if not settings.deepseek_api_key:
            return {}
        body = (content or "")[:4000]
        user_prompt = f"TITLE:\n{title}\n\nBODY (truncated):\n{body}"
        raw = await self._call_deepseek(
            self.SEO_PROMPT,
            user_prompt,
            max_tokens=500,
            reasoning=None,
            max_attempts=1,
            usage_log=usage_log,
        )
        data = self._extract_seo_json(raw or "") if raw else {}
        seo_description = (data.get("seo_description") or "").strip()[:500]
        seo_keywords = (data.get("seo_keywords") or "").strip()[:500]
        if not seo_description:
            # Title-based fallback so SEO is never blank.
            seo_description = (title or content or "")[:160].strip()
        if not seo_keywords:
            # Title/keyword fallback so keywords are never blank either.
            words = [
                w.lower()
                for w in re.findall(r"[A-Za-z][A-Za-z0-9\-']*", title or "")
            ]
            seen = []
            for w in words:
                if w not in seen and len(w) >= 4:
                    seen.append(w)
                if len(seen) >= 6:
                    break
            seo_keywords = ", ".join(seen)
        return {
            "seo_description": seo_description,
            "seo_keywords": seo_keywords,
        }

    @staticmethod
    def _extract_seo_json(raw: str) -> dict:
        """Best-effort parse of the LLM's SEO JSON (handles markdown fences)."""
        import json
        import re as _re

        if not raw:
            return {}
        fenced = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
        if fenced:
            raw = fenced.group(1)
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        start = raw.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(raw)):
                if raw[i] == "{":
                    depth += 1
                elif raw[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(raw[start : i + 1])
                        except Exception:
                            break
        return {}

    # ── Accuracy Verification ───────────────────────────────

    # Shared system message used VERBATIM by ALL four DeepSeek calls that feed
    # an article's research: the public write, the premium write, and the two
    # per-lane accuracy checks. DeepSeek's input cache requires byte-identity
    # from position 0 INCLUDING the system message, so every call must start
    # with this exact block followed by the research prefix for the research
    # tokens to come from cache. Task-specific instructions (write vs verify;
    # public vs premium) live in the USER message tail AFTER the research, never
    # in the system block.
    #
    # NOTE: PUBLIC and PREMIUM are two SEALED lanes. The public lane is fed
    # stripped research (no picks/bets); the premium lane gets the full research
    # with picks. This block itself carries no picks, so it's safe to share.
    SHARED_SYSTEM = (
        "You are helping produce editorial game previews for Earl Knows Ball, "
        "a sports handicapping site. You write and fact-check NFL, NBA, and MLB "
        "game previews.\n\n"
        "Each request embeds the research data (teams, stats, standings, lines, "
        "injuries, context, and—where applicable—picks) directly in the prompt "
        "after this system message. Ground every claim you produce or verify in "
        "that research; never invent facts, numbers, or names. Follow the specific "
        "task instructions in the request below the research data."
    )

    ACCURACY_SYSTEM_PROMPT = (
        "You are the final fact-checker for sports preview articles. "
        "Your ONLY job is to verify the article against the research data "
        "provided. You do NOT rewrite, edit, or improve the article.\n\n"
        "Check EVERY factual claim, statistic, player, coach, team, venue, "
        "date, and trademarked/detailed number against the RESEARCH DATA. "
        "A claim is a problem if EITHER:\n"
        "  1. FACT-NOT-IN-RESEARCH — the article asserts a specific fact, "
        "     stat (e.g. a batting average, PPG, win%, an injury), or names "
        "     a person/team that does not appear anywhere in the research.\n"
        "  2. CONTRADICTS-RESEARCH — the article states a number or name that "
        "     conflicts with the research (e.g. wrong record, wrong player, "
        "     wrong venue).\n"
        "Only flag claims the article treats as true facts. Do not flag general "
        "prose, reasonable color, or facts that ARE present in the research.\n\n"
        "PUBLIC-CONTENT PREDICTION RULE:\n"
        "The PUBLIC section of an article must NEVER make any betting "
        "prediction, pick, or recommendation. Verify the public section \"PUBLIC\" "
        "(and titles/SEO) contains NO: point-spread or against-the-spread pick, "
        "moneyline pick, over/under pick, projected/final score, betting "
        "recommendation, or any statement telling a reader to bet or that a specific "
        "team will win.\n\n"
        "The PREMIUM section (\"PREMIUM\") is allowed to contain picks and is "
        "NOT subject to the prediction rule — do not flag betting content there.\n\n"
        "Return a JSON object ONLY (no markdown, no commentary) with this shape:\n"
        "{\n"
        "  \"passed\": true|false,\n"
        "  \"findings\": [\n"
        "    {\"type\": \"fact-not-in-research\" | \"contradicts-research\" | \"prediction-in-public\", "
        "\"claim\": \"the exact text from the article\", "
        "\"section\": \"public\" | \"premium\" | \"seo\", "
        "\"detail\": \"explain which research field it should match or which rule it broke\"}\n"
        "  ]\n"
        "}\n"
        "passed=true only if there are NO findings. If you legitimately find "
        "nothing wrong, \"passed\": true and an empty findings list. Do not "
        "invent problems."
    )

    def _summary_of_research(self, research: dict[str, Any]) -> str:
        """Flatten the research dict into a compact, serializable text summary."""
        def _flatten(value: Any, depth: int = 0) -> list[str]:
            out: list[str] = []
            if isinstance(value, dict):
                for k, v in value.items():
                    out.append(f"{'  '*depth}{k}:")
                    out += _flatten(v, depth + 1)
            elif isinstance(value, list):
                for i, v in enumerate(value):
                    out.append(f"{'  '*depth}[{i}]")
                    out += _flatten(v, depth + 1)
            elif value is not None:
                out.append(f"{'  '*depth}{value}")
            return out

        try:
            return "\n".join(_flatten(research))
        except Exception:  # noqa: BLE001
            try:
                return json.dumps(research, default=str)[:12000]
            except Exception:  # noqa: BLE001
                return str(research)[:12000]

    # Instruct the model to append SEO metadata after the article so it is
    # derived from the SAME response (no extra LLM call).
    SEO_OUTPUT_INSTRUCTION = (
        "\n\nAfter the article, on the final lines, provide search-engine meta "
        "metadata with EXACTLY this format (no extra commentary):\n"
        "SEO_DESCRIPTION: <one sentence, under 160 chars, marketing tone>\n"
        "SEO_KEYWORDS: <comma-separated keywords, 3-8 items>"
    )

    def _extract_seo_block(self, text: str) -> tuple[str, dict[str, str]]:
        """Extract trailing SEO_DESCRIPTION/SEO_KEYWORDS lines from a response.

        Returns (clean_text, {seo_description, seo_keywords}). The instruction
        block is removed from the returned text.
        """
        text = text or ""
        desc = None
        kw = None
        lines = text.split("\n")
        keep = []
        for ln in lines:
            s = ln.strip()
            m = re.match(r"^SEO_DESCRIPTION:\s*(.*)$", s, re.IGNORECASE)
            if m and m.group(1).strip():
                desc = m.group(1).strip()
                continue
            m = re.match(r"^SEO_KEYWORDS:\s*(.*)$", s, re.IGNORECASE)
            if m and m.group(1).strip():
                kw = m.group(1).strip()
                continue
            keep.append(ln)
        return ("\n".join(keep).strip(), {"seo_description": desc or "", "seo_keywords": kw or ""})

    async def _verify_lane(
        self,
        *,
        label: str,
        content: str,
        research_prefix: str,
        task_rule: str,
        max_tokens: int = 50000,
        usage_log: Optional[list] = None,
    ) -> dict[str, Any]:
        """Run ONE per-lane fact-check call against a shared cache prefix.

        system is always SHARED_SYSTEM; the user prompt starts with
        research_prefix (byte-identical to the lane's write call) so DeepSeek
        serves the research tokens from input cache. task_rule holds the
        lane-specific verification instructions in the user tail (after the
        article). Returns {"passed", "findings", "raw", "tokens", "skipped", "error"}.
        """
        if not (content or "").strip():
            return {
                "skipped": True,
                "passed": False,
                "findings": [],
                "error": f"No {label} article content to verify.",
                "raw": "",
                "tokens": 0,
            }

        user_prompt = (
            f"{research_prefix}\n\n"
            "=== ARTICLE TO VERIFY ===\n"
            f"{(content or '')[:28000]}\n\n"
            f"{task_rule}"
        )

        lane_log: list = []
        raw = await self._call_deepseek(
            self.SHARED_SYSTEM,
            user_prompt,
            max_tokens=max_tokens,
            reasoning="minimal",
            max_attempts=2,
            usage_log=lane_log,
        )
        tokens = sum((item.get("total_tokens") or 0) for item in lane_log)
        if usage_log is not None:
            usage_log.extend(lane_log)

        if not raw:
            return {
                "skipped": True,
                "passed": False,
                "findings": [],
                "error": f"{label.capitalize()} accuracy check returned no response.",
                "raw": "",
                "tokens": tokens,
            }

        data = self._extract_seo_json(raw) or {}
        findings = data.get("findings") or []
        if not isinstance(findings, list):
            findings = []
        return {
            "passed": bool(data.get("passed")),
            "findings": findings,
            "raw": raw,
            "tokens": tokens,
            "skipped": False,
            "error": None,
        }

    async def _verify_accuracy(
        self,
        public_content: str,
        premium_content: str,
        research: dict[str, Any],
        *,
        research_prefix: str = "",
        usage_log: Optional[list] = None,
    ) -> dict[str, Any]:
        """Final fact-check of the assembled article, SPLIT into two sealed lanes.

        PUBLIC and PREMIUM are verified in SEPARATE DeepSeek calls against
        SEPARATE research:

          - PUBLIC lane  -> STRIPPED research (no picks/bets); enforces the
            no-betting-predictions rule so the public piece never sees our picks.
          - PREMIUM lane -> FULL research (with picks); fact-checks only, because
            betting language is allowed/expected in premium.

        Each lane's research prefix is byte-identical to the matching write call
        so DeepSeek serves the research from input cache. Returns a dict like
        {"passed": bool, "findings": [...], "raw": str, "tokens": int, "skipped": bool}.
        """
        stripped = self._strip_betting(research)
        # Full lane prefix: prefer the caller's (already-built) full research
        # prefix so the premium accuracy call shares the premium write call's
        # exact bytes; fall back to rebuilding it deterministically.
        full_prefix = research_prefix if research_prefix else self._build_messages(research)
        # Public lane prefix is ALWAYS built from STRIPPED research. Never reuse
        # the passed prefix here — in the full flow that prefix is the FULL
        # research (with picks), which must not reach the public accuracy lane.
        stripped_prefix = self._build_messages(stripped)

        pub_rule = (
            "Verify the PUBLIC article against the research. It must contain NO "
            "betting predictions (no spread/ATS pick, moneyline, over/under pick, "
            "score prediction, or [bet on X] guidance). Flag every factual claim "
            "not traceable to the research, every contradiction, and every betting "
            "prediction. Return ONLY a JSON object: {\"passed\": true|false, "
            "\"findings\": [{\"type\": \"fact-not-in-research\" | \"contradicts-research\" | "
            "\"betting-prediction-in-public\", \"claim\": \"...\", \"section\": \"public\", "
            "\"detail\": \"...\"}]}."
        )
        prem_rule = (
            "Verify the PREMIUM article against the research. It MAY contain picks "
            "and betting advice — that is allowed and should NOT be flagged. Only "
            "flag factual claims not traceable to the research or that contradict it. "
            "Return ONLY a JSON object: {\"passed\": true|false, \"findings\": [{\"type\": "
            "\"fact-not-in-research\" | \"contradicts-research\", \"claim\": \"...\", "
            "\"section\": \"premium\", \"detail\": \"...\"}]}."
        )

        pub_res = await self._verify_lane(
            label="public",
            content=public_content,
            research_prefix=stripped_prefix,
            task_rule=pub_rule,
            usage_log=usage_log,
        )
        prem_res = await self._verify_lane(
            label="premium",
            content=premium_content,
            research_prefix=full_prefix,
            task_rule=prem_rule,
            usage_log=usage_log,
        )

        findings = (pub_res.get("findings") or []) + (prem_res.get("findings") or [])
        skipped = bool(pub_res.get("skipped") and prem_res.get("skipped"))
        raw = "[PUBLIC]\n" + (pub_res.get("raw") or "") + "\n\n[PREMIUM]\n" + (prem_res.get("raw") or "")
        tokens = (pub_res.get("tokens") or 0) + (prem_res.get("tokens") or 0)
        return {
            "passed": bool(pub_res.get("passed")) and bool(prem_res.get("passed")),
            "findings": findings,
            "raw": raw,
            "tokens": tokens,
            "skipped": skipped,
            "error": ("Accuracy check returned no response." if skipped else None),
        }

    def _render_full_article(self, parsed: dict[str, Any]) -> str:
        """Concatenate public + premium content for verification/fix loops."""
        parts = []
        if parsed.get("public_content"):
            parts.append(parsed["public_content"])
        if parsed.get("premium_content"):
            parts.append(parsed["premium_content"])
        return "\n\n".join(parts)

    CORRECT_SYSTEM_PROMPT = (
        "You are a careful sports editor making minimal corrections to a "
        "preview article. You only fix the EXACT problems listed below. "
        "Do not rewrite, embellish, or change anything else. Do not invent "
        "new facts or numbers.\n\n"
        "RULES BY FINDING TYPE:\n"
        "- type 'fact-not-in-research': the claim cannot be traced to the "
        "  research. DELETE the claim (the whole sentence is best). Do NOT "
        "  keep, rephrase, or soften it — a fact that isn't in the research "
        "  cannot be made true by rewording. Remove it outright.\n"
        "- type 'contradicts-research': the claim directly conflicts with the "
        "  research. REWRITE it to match the research exactly.\n"
        "- type 'betting-prediction-in-public': REMOVE the pick/odds/prediction "
        "  from the public section (move to premium only if appropriate)."
    )

    async def _correct_article(
        self,
        public_content: str,
        premium_content: str,
        research: dict[str, Any],
        findings: list[Any],
        *,
        usage_log: Optional[list] = None,
    ) -> dict[str, Any]:
        """Bounded single revision pass fixing accuracy-check findings.

        Sends the article and the list of findings back to the LLM with
        instructions to correct only those items. Returns a dict with corrected
        ``public_content`` / ``premium_content`` and ``title`` (empty dict if the
        correction call fails).
        """
        findings_text = json.dumps(findings, ensure_ascii=False, indent=2)
        research_summary = self._summary_of_research(research)[:12000]
        user_prompt = (
            "=== RESEARCH DATA ===\n"
            f"{research_summary}\n\n"
            "=== ACCURACY FINDINGS TO FIX ===\n"
            f"{findings_text}\n\n"
            "=== CURRENT PUBLIC SECTION ===\n"
            f"{(public_content or '').strip()}\n\n"
            "=== CURRENT PREMIUM SECTION ===\n"
            f"{(premium_content or '').strip()}\n\n"
            "Return the FULL corrected article ONLY, in this exact format:\n"
            "TITLE: <title>\n\n"
            "[PUBLIC]\n<corrected public content — must contain NO betting "
            "predictions or picks>\n\n[PREMIUM]\n<corrected premium content — "
            "picks allowed here>\n"
        )

        raw = await self._call_deepseek(
            self.CORRECT_SYSTEM_PROMPT,
            user_prompt,
            max_tokens=40000,
            reasoning="minimal",
            usage_log=usage_log,
        )
        if not raw:
            return {}

        result: dict[str, Any] = {}
        title = None
        text = raw
        title_match = re.match(r"^\s*TITLE:\s*(.+)$", text, flags=re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()[:300]
            text = re.sub(r"^\s*TITLE:\s*.+?$\n*", "", text, count=1, flags=re.MULTILINE)

        pub_match = re.search(r"\[PUBLIC\]\s*(.*?)(?:\s*\[PREMIUM\]|\Z)", text, flags=re.DOTALL)
        pre_match = re.search(r"\[PREMIUM\]\s*(.*)\Z", text, flags=re.DOTALL)
        if pub_match:
            result["public_content"] = pub_match.group(1).strip()
        if pre_match and pre_match.group(1).strip():
            result["premium_content"] = pre_match.group(1).strip()
        if title:
            result["title"] = title
        return result

    @staticmethod
    def slugify_title(title: str) -> str:
        """Turn a title into a URL-safe slug fragment (mirrors Original Articles)."""
        t = (title or "").strip().lower()
        t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
        t = re.sub(r"-{2,}", "-", t)
        return t[:80]

    def _derive_slug(self, game_id: int, title: str, game_date: Any = None) -> str:
        """Deterministic writeup slug: YYYY-MM-DD-<title-slug> (uses game date)."""
        date_part = ""
        if game_date is not None:
            try:
                date_part = game_date.strftime("%Y-%m-%d")
            except Exception:  # noqa: BLE001
                date_part = str(game_date)[:10]
        base = self.slugify_title(title) or f"writeup-{game_id}"
        return f"{date_part}-{base}" if date_part else base

    # ── Prompt Building ─────────────────────────────────────

    def _strip_betting(self, research: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of the research with all betting/pick keys removed.

        Used to build the PUBLIC writeup lane: the public article (and its
        accuracy check) must never see our picks/odds/predictions/model output.
        Premium keeps the full research.
        """
        stripped = dict(research)
        for key in (
            "betting_lines",
            "bets",
            "predictions",
            "model_predictions",
            "shap_digest",
            "home_splits",
            "away_splits",
        ):
            stripped.pop(key, None)
        return stripped

    def _build_messages(self, research: dict[str, Any]) -> str:
        """Build the user prompt from the research data."""
        # Pull game identity fields, preferring the nested game_summary shape,
        # falling back to legacy top-level keys when present.
        gs = research.get("game_summary") or {}
        home_obj = gs.get("home_team") or {}
        away_obj = gs.get("away_team") or {}
        venue_obj = gs.get("venue") or {}
        home_name = (
            home_obj.get("name")
            if isinstance(home_obj, dict)
            else research.get("home_team_name")
        ) or research.get("home_team_name") or "?"
        away_name = (
            away_obj.get("name")
            if isinstance(away_obj, dict)
            else research.get("away_team_name")
        ) or research.get("away_team_name") or "?"
        venue_name = (
            venue_obj.get("name")
            if isinstance(venue_obj, dict)
            else research.get("venue_name")
        ) or research.get("venue_name") or "?"

        # Game date/day must be rendered in US Eastern so the LLM never has to
        # guess the day. game_summary.date is already tz-aware ET when present;
        # normalize any input to America/New_York and print an explicit weekday.
        raw_date = gs.get("date") or research.get("game_date")
        date_label = "?"
        if raw_date:
            try:
                dt = raw_date if isinstance(raw_date, datetime) else datetime.fromisoformat(str(raw_date))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("America/New_York"))
                else:
                    dt = dt.astimezone(ZoneInfo("America/New_York"))
                date_label = dt.strftime("%A, %B %d, %Y at %I:%M %p ET")
            except Exception:
                date_label = str(raw_date)

        # Start building the research overview
        lines = [
            "=== RESEARCH DATA ===",
            f"Game: {home_name} vs {away_name}",
            f"Date: {date_label}",
            f"Venue: {venue_name}",
            "",
            "--- HANDICAP INFO ---",
        ]

        if handicap := research.get("handicap_info"):
            for key, value in handicap.items():
                if isinstance(value, dict):
                    lines.append(f"\n  [{key}]")
                    for k, v in value.items():
                        lines.append(f"    {k}: {v}")
                else:
                    lines.append(f"  {key}: {value}")

        if betting_lines := research.get("betting_lines"):
            lines.append("\n--- BETTING LINES ---")
            for key, value in betting_lines.items():
                lines.append(f"  {key}: {value}")

        if home_stats := research.get("home_stats"):
            lines.append(f"\n--- {home_name} STATS ---")
            if isinstance(home_stats, dict):
                for key, value in home_stats.items():
                    lines.append(f"  {key}: {value}")

        if away_stats := research.get("away_stats"):
            lines.append(f"\n--- {away_name} STATS ---")
            if isinstance(away_stats, dict):
                for key, value in away_stats.items():
                    lines.append(f"  {key}: {value}")

        if key_matchups := research.get("key_matchups"):
            lines.append("\n--- KEY MATCHUPS ---")
            if isinstance(key_matchups, list):
                for matchup in key_matchups:
                    if isinstance(matchup, dict):
                        for k, v in matchup.items():
                            lines.append(f"  {k}: {v}")
                        lines.append("")
                    else:
                        lines.append(f"  {matchup}")
            elif isinstance(key_matchups, dict):
                for k, v in key_matchups.items():
                    lines.append(f"  {k}: {v}")

        if pitching_matchup := research.get("pitching_matchup"):
            lines.append("\n--- PITCHING MATCHUP ---")
            for team_key in ("home", "away"):
                if tm := pitching_matchup.get(team_key):
                    team_label = f"{research.get(f'{team_key}_team_name', team_key.title())} Pitcher"
                    lines.append(f"\n  [{team_label}]")
                    if isinstance(tm, dict):
                        for k, v in tm.items():
                            if isinstance(v, dict):
                                lines.append(f"    {k}:")
                                for sk, sv in v.items():
                                    lines.append(f"      {sk}: {sv}")
                            elif isinstance(v, list):
                                lines.append(f"    {k}:")
                                for i, item in enumerate(v):
                                    if isinstance(item, dict):
                                        parts = [f"      Start {i+1}:"]
                                        for sk, sv in item.items():
                                            parts.append(f"        {sk}: {sv}")
                                        lines.append("\n".join(parts))
                                    else:
                                        lines.append(f"      {item}")
                            else:
                                lines.append(f"    {k}: {v}")

        if injuries := research.get("injuries"):
            lines.append("\n--- INJURIES ---")
            if isinstance(injuries, dict):
                for team_key in ("home", "away"):
                    team_label = f"{research.get(f'{team_key}_team_name', team_key.title())}"
                    if team_injuries := injuries.get(team_key):
                        lines.append(f"\n  [{team_label}]")
                        if isinstance(team_injuries, list):
                            for injury in team_injuries:
                                if isinstance(injury, dict):
                                    parts = []
                                    for k, v in injury.items():
                                        parts.append(f"    {k}: {v}")
                                    lines.append("\n".join(parts))
                                else:
                                    lines.append(f"  {injury}")
                        elif isinstance(team_injuries, dict):
                            for k, v in team_injuries.items():
                                lines.append(f"  {k}: {v}")
                    else:
                        lines.append(f"  No injuries for {team_label}")
            elif isinstance(injuries, list):
                for injury in injuries:
                    lines.append(f"  {injury}")

        if venue := research.get("venue"):
            lines.append("\n--- VENUE ---")
            if isinstance(venue, dict):
                for k, v in venue.items():
                    lines.append(f"  {k}: {v}")
            else:
                lines.append(f"  {venue}")

        if predictions := research.get("predictions"):
            lines.append("\n--- MODEL PREDICTIONS ---")
            if isinstance(predictions, dict):
                for key, value in predictions.items():
                    if isinstance(value, dict):
                        lines.append(f"  [{key}]")
                        for k, v in value.items():
                            lines.append(f"    {k}: {v}")
                    else:
                        lines.append(f"  {key}: {value}")
            else:
                lines.append(f"  {predictions}")

        if shap := research.get("shap_digest"):
            lines.append("\n--- MODEL REASONING (why an edge may exist here) ---")
            if isinstance(shap, dict):
                # Sanitized rationale first: plain-language, no feature names or
                # numbers. This is what the writer should use. (Option A.)
                rationale = (shap.get("narrative_rationale") or "").strip()
                if rationale:
                    lines.append(f"  {rationale}")
                # Do NOT dump raw feature values/names into the prompt — that
                # risks leaking stat names and numbers into the final text.
                # The raw digest stays available in research_brief for Admin UI.
            elif shap:
                lines.append(f"  {shap}")

        if narrative_data := research.get("narrative_data"):
            lines.append("\n--- NARRATIVE / CONTEXT ---")
            if isinstance(narrative_data, dict):
                for key, value in narrative_data.items():
                    lines.append(f"  {key}: {value}")
            else:
                lines.append(f"  {narrative_data}")

        # Include article enrichment (vector search summary from DeepSeek)
        if enrichment := research.get("article_enrichment"):
            enriched_summary = enrichment.get("enriched_summary", "") if isinstance(enrichment, dict) else ""
            if enriched_summary.strip():
                lines.append("\n--- RECENT ARTICLES CONTEXT ---")
                lines.append(f"  {enriched_summary}")

        return "\n".join(lines)

    # ── Response Parsing ────────────────────────────────────

    def _parse_response(
        self,
        raw: str,
        research: dict[str, Any],
        is_historical: bool,
    ) -> dict[str, Any]:
        """Parse the DeepSeek response into a structured dict."""
        cleaned = raw.strip()
        # Strip markdown code fences
        if cleaned.startswith("```"):
            start = cleaned.find("{")
            if start >= 0:
                cleaned = cleaned[start:]
            end = cleaned.rfind("}")
            if end >= 0:
                cleaned = cleaned[: end + 1]

        # First try: direct json.loads (fast path)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Second try: use json_repair for robust malformed-JSON handling
            logger.warning("JSON parse failed — attempting repair with json_repair")
            try:
                repaired = repair_json(cleaned)
                data = json.loads(repaired)
            except Exception as e:
                logger.error("JSON repair failed: %s — raw=%s", e, raw[:300])
                return {"error": f"Failed to parse DeepSeek response: {e}"}

        title = data.get("title", "").strip()
        public_content = data.get("public_content", "").strip()
        premium_content = data.get("premium_content", "").strip()

        if not public_content or not premium_content:
            missing = []
            if not public_content:
                missing.append("public_content")
            if not premium_content:
                missing.append("premium_content")
            return {"error": f"Missing content fields: {', '.join(missing)}"}

        return {
            "title": title,
            "public_content": public_content,
            "premium_content": premium_content,
            "research_brief": research,
            "is_historical": is_historical,
        }

    # ── Public-only generation ────────────────────────────

    def _build_public_messages(self, research: dict[str, Any]) -> str:
        """Build the user prompt for a public-only writeup.

        Relies on the caller having already stripped betting/proprietary keys
        from the research dict via get_public_research_brief.
        """
        return self._build_messages(research)

    async def generate_public(
        self,
        game_id: int,
        research: dict[str, Any],
        is_historical: bool = False,
        usage_log: Optional[list] = None,
    ) -> dict[str, Any]:
        """Generate a public-only write-up (no picks, no premium section).

        This is a separate, lighter LLM call meant for the public-facing
        endpoint. Length: 800-1000 words — same as the public section of a full
        writeup. The stripped research keeps proprietary data out of the prompt.
        """
        system = self.SHARED_SYSTEM
        # Shared system + stripped research prefix, then the public write task.
        user_prompt = (
            self._build_public_messages(research) + "\n\n"
            "=== WRITE THE PUBLIC GAME PREVIEW ===\n"
            + self.public_system_prompt(is_historical)
            + "\n\n" + self.SEO_OUTPUT_INSTRUCTION
        )

        raw = await self._call_deepseek(system, user_prompt)
        if raw is None:
            return {"error": "DeepSeek API call failed — check logs"}

        # Parse into title + content (free-form; we expect first line as title)
        raw, seo_from_resp = self._extract_seo_block(raw)
        lines = raw.strip().split("\n", 1)
        title = lines[0].strip().strip("#").strip() if lines else ""
        content = lines[1].strip() if len(lines) > 1 else ""

        # SEO meta: prefer from the public response (no extra call); fall back
        # to _generate_seo only if the model omitted it.
        seo_desc = (seo_from_resp.get("seo_description") or "").strip()[:500]
        seo_kw = (seo_from_resp.get("seo_keywords") or "").strip()[:500]
        if not seo_desc:
            _seo = await self._generate_seo(title or "", content or "", usage_log=usage_log)
            seo_desc = (_seo.get("seo_description") or "").strip()[:500]
            seo_kw = (_seo.get("seo_keywords") or "").strip()[:500]
        seo = {"seo_description": seo_desc, "seo_keywords": seo_kw}

        # ---- Final accuracy verification ----
        # Correction loop: up to MAX_CORRECTION_PASSES attempts, re-verifying
        # after each. If it won't come clean, keep best-effort content and let
        # accuracy_check surface the remaining inaccuracy to the listing.
        accuracy_check = await self._verify_accuracy(
            content or "", "", research, usage_log=usage_log,
            research_prefix=self._build_public_messages(research),
        )
        retries_used = 0
        max_passes = getattr(self, "MAX_CORRECTION_PASSES", 2)
        has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")
        _rej_hist = []
        while has_findings and retries_used < max_passes:
            _rej_hist.append({
                "attempt": retries_used + 1,
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "accuracy_check": accuracy_check,
                "public_content": content or "",
                "premium_content": "",
            })
            corrected = await self._correct_article(
                content or "",
                "",
                research,
                accuracy_check.get("findings") or [],
                usage_log=usage_log,
            )
            retries_used += 1
            if not corrected:
                break
            if corrected.get("public_content"):
                content = corrected["public_content"]
            if corrected.get("title"):
                title = corrected["title"]
            # Re-run checks on the corrected content.
            accuracy_check = await self._verify_accuracy(
                content or "", "", research, usage_log=usage_log,
                research_prefix=self._build_public_messages(research),
            )
            has_findings = bool(accuracy_check.get("findings")) and not accuracy_check.get("skipped")
        accuracy_check["retries_used"] = retries_used
        accuracy_check["has_inaccuracy"] = has_findings
        accuracy_check["accuracy_pass"] = not has_findings
        rejection_history = _rej_hist

        gd = None
        try:
            gd = research.get("game_summary", {}).get("date")
        except Exception:  # noqa: BLE001
            gd = None
        return {
            "title": title,
            "public_content": content,
            "research_brief": research,
            "is_historical": is_historical,
            "slug": self._derive_slug(game_id, title, gd),
            "seo_description": (seo.get("seo_description") or "").strip()[:500] or None,
            "seo_keywords": (seo.get("seo_keywords") or "").strip()[:500] or None,
            "accuracy_check": accuracy_check,
            "accuracy_check_tokens": accuracy_check.get("tokens") or 0,
            "rejection_history": rejection_history or [],
        }

    # ── Quality Checks ──────────────────────────────────────

    def run_quality_checks(
        self,
        article: dict[str, Any],
        research: dict[str, Any],
    ) -> QCResults:
        """Run quality checks on the generated article.

        Returns a list of check results, each with: *check_name*, *passed*, *detail*.
        """
        checks: QCResults = []

        # Check 1: title length
        title = article.get("title", "")
        checks.append({
            "check_name": "title_length",
            "passed": 20 <= len(title) <= 120,
            "detail": f"Title has {len(title)} characters (target: 20-120)",
        })

        # Check 2: public content length
        public_content = article.get("public_content", "")
        public_words = len(public_content.split())
        checks.append({
            "check_name": "public_word_count",
            "passed": 300 <= public_words <= 6000,
            "detail": f"Public content has {public_words} words (target: 300-6000)",
        })

        # Check 3: premium content length
        premium_content = article.get("premium_content", "")
        premium_words = len(premium_content.split())
        checks.append({
            "check_name": "premium_word_count",
            "passed": 300 <= premium_words <= 6000,
            "detail": f"Premium content has {premium_words} words (target: 300-6000)",
        })

        # Check 4: mentions both teams
        gs = research.get("game_summary") or {}
        _ht = gs.get("home_team") or {}
        _at = gs.get("away_team") or {}
        home_team = (_ht.get("name") if isinstance(_ht, dict) else None) or research.get("home_team_name") or ""
        away_team = (_at.get("name") if isinstance(_at, dict) else None) or research.get("away_team_name") or ""
        home_team = str(home_team).lower()
        away_team = str(away_team).lower()
        combined = (public_content + " " + premium_content).lower()

        if home_team and away_team:
            mentions_home = home_team in combined
            mentions_away = away_team in combined
            checks.append({
                "check_name": "both_teams_mentioned",
                "passed": mentions_home and mentions_away,
                "detail": (
                    f"Home team '{home_team}' mentioned: {mentions_home}, "
                    f"Away team '{away_team}' mentioned: {mentions_away}"
                ),
            })
        else:
            checks.append({
                "check_name": "both_teams_mentioned",
                "passed": True,  # skip if names unavailable
                "detail": "Team names not available in research — skipped",
            })

        # Check 5: premium is distinct from public
        public_set = set(public_content.lower().split())
        premium_set = set(premium_content.lower().split())
        overlap = len(public_set & premium_set)
        ratio = overlap / max(len(premium_set), 1)
        checks.append({
            "check_name": "premium_distinctness",
            "passed": ratio < 0.6,
            "detail": f"Word overlap ratio: {ratio:.0%} (target: <60%)",
        })

        return checks

    # ── Storage ─────────────────────────────────────────────

    async def store(
        self,
        game_id: int,
        article: dict[str, Any],
        qc_results: QCResults,
    ) -> None:
        """Persist the generated article. Subclass hook."""
        # Override in sport-specific subclass
        pass

    # ── Static helpers ──────────────────────────────────────

    @staticmethod
    def _fmt(
        d: dict[str, Any] | None,
        key: str,
        fmt: str = "{}",
        default: str = "",
    ) -> str:
        """Safely format a value from a dict."""
        if d is None:
            return default
        val = d.get(key)
        if val is None:
            return default
        return fmt.format(val)

    @staticmethod
    def _maybe(d: dict[str, Any] | None, key: str) -> str:
        """Return value if present, else empty string."""
        return str(d[key]) if d and d.get(key) else ""
