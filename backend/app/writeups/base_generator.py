"""Base write-up generator — DeepSeek integration, prompt templates, QC."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
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
    MAX_TOKENS = 16384  # enough for public + premium (4k-6k words total)
    TIMEOUT = 120.0  # generous for longer generation

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

OUTPUT FORMAT:
Return ONLY valid JSON with the following fields:
{{
    "title": "Engaging article title (include team names, max ~80 chars)",
    "public_content": "Full public article text (1600-3200 words, many paragraphs - be detailed and comprehensive)",
    "premium_content": "Full premium analysis text (1600-3200 words, many paragraphs - be detailed and comprehensive)"
}}

{tense_note}

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

Length: 1200-2000 words.

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

This is a game preview — not a betting analysis. Write in the style of a well-informed beat writer: insightful, engaging, and authoritative.

{tense_note}

Write your article below. Start with the title on its own line (preceded by ##), then the article body."""

    # ── Generation ──────────────────────────────────────────

    async def generate(
        self,
        game_id: int,
        is_historical: bool = False,
        as_of_date: datetime | None = None,
    ) -> dict[str, Any]:
        """Generate a write-up for the given *game_id*.

        Returns the dict with keys: *title*, *public_content*, *premium_content*,
        *title_brief*, *research_brief*, *is_historical*, *qc_results*.
        """
        logger.info("generating write-up for game_id=%s", game_id)

        # ---- 1. Research ----
        research = await self.research_brief(game_id, as_of_date)
        if "error" in research:
            logger.warning("research_brief failed for game %s: %s", game_id, research["error"])
            return {"error": research["error"]}

        # ---- 2. Build conversation ----
        system = self.system_prompt(is_historical)
        user_prompt = self._build_messages(research)

        # ---- 3. Call DeepSeek ----
        raw = await self._call_deepseek(system, user_prompt)
        if raw is None:
            return {"error": "DeepSeek API call failed — check logs"}

        # ---- 4. Parse response ----
        parsed = self._parse_response(raw, research, is_historical)
        if "error" in parsed:
            return parsed

        # ---- 5. Quality checks ----
        qc_results = self.run_quality_checks(parsed, research)
        parsed["qc_results"] = qc_results

        # ---- 6. Store ----
        await self.store(game_id, parsed, qc_results)

        logger.info(
            "write-up %s for game %s — qc=%s/%s passed",
            parsed.get("title", "(no title)"),
            game_id,
            sum(1 for q in qc_results if q.get("passed")),
            len(qc_results),
        )
        return parsed

    async def _call_deepseek(self, system: str, user_prompt: str) -> str | None:
        """Call DeepSeek via OpenAI SDK and return the raw response content.

        Returns *None* on failure — caller checks for None.
        """
        try:
            client = AsyncOpenAI(
                api_key=settings.deepseek_api_key,
                base_url=f"{settings.deepseek_base_url}/v1",
            )

            response = await client.chat.completions.create(
                model=self.MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=self.TEMPERATURE,
                max_tokens=self.MAX_TOKENS,
                timeout=self.TIMEOUT,
            )

            content = response.choices[0].message.content
            if not content or not content.strip():
                logger.error("DeepSeek returned empty response")
                return None

            return content

        except Exception as e:
            logger.error("DeepSeek API call failed: %s", e)
            return None

    # ── Prompt Building ─────────────────────────────────────

    def _build_messages(self, research: dict[str, Any]) -> str:
        """Build the user prompt from the research data."""
        # Start building the research overview
        lines = [
            "=== RESEARCH DATA ===",
            f"Game: {research.get('home_team_name', '?')} vs {research.get('away_team_name', '?')}",
            f"Date: {research.get('game_date', '?')}",
            f"Venue: {research.get('venue_name', '?')}",
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
            lines.append(f"\n--- {research.get('home_team_name', 'Home')} STATS ---")
            if isinstance(home_stats, dict):
                for key, value in home_stats.items():
                    lines.append(f"  {key}: {value}")

        if away_stats := research.get("away_stats"):
            lines.append(f"\n--- {research.get('away_team_name', 'Away')} STATS ---")
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
    ) -> dict[str, Any]:
        """Generate a public-only write-up (no picks, no premium section).

        This is a separate, lighter LLM call meant for the public-facing
        endpoint. The 1200-2000 word target avoids overwhelming casual readers
        and the stripped research keeps proprietary data out of the prompt.
        """
        system = self.public_system_prompt(is_historical)
        user_prompt = self._build_public_messages(research)

        raw = await self._call_deepseek(system, user_prompt)
        if raw is None:
            return {"error": "DeepSeek API call failed — check logs"}

        # Parse into title + content (free-form; we expect first line as title)
        lines = raw.strip().split("\n", 1)
        title = lines[0].strip().strip("#").strip() if lines else ""
        content = lines[1].strip() if len(lines) > 1 else ""

        return {
            "title": title,
            "public_content": content,
            "research_brief": research,
            "is_historical": is_historical,
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
        home_team = (research.get("home_team_name") or "").lower()
        away_team = (research.get("away_team_name") or "").lower()
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
