"""NBA write-up generator — extends BaseWriteupGenerator for basketball.

Follows the same pattern as nfl/generator.py:
- Override generate() to accept db, store as self._db, call super()
- Prompt includes --- START PUBLIC WRITEUP --- / --- END PUBLIC WRITEUP ---
  and --- START PREMIUM WRITEUP --- / --- END PREMIUM WRITEUP --- markers
  that the base generator parses out.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.writeups.base_generator import BaseWriteupGenerator

logger = logging.getLogger("nba.generator")


class NBAGameWriteupGenerator(BaseWriteupGenerator):
    """Generator for NBA game write-ups."""

    SPORT = "nba"

    # ── Generate (overridden to inject db) ─────────────────────

    async def generate(
        self,
        db: AsyncSession,
        game_id: int,
        is_historical: bool = False,
        as_of_date: datetime | None = None,
    ):
        """Full pipeline with DB session. Follows NFL pattern."""
        self._db = db
        result = await super().generate(game_id, is_historical, as_of_date)
        self._db = None
        if "error" in result:
            return result, []
        qc_results = result.pop("qc_results", [])
        return result, qc_results

    # ── Research ─────────────────────────────────────────────

    async def research_brief(
        self,
        game_id: int,
        as_of_date: datetime | None = None,
    ) -> dict[str, Any]:
        return await get_research_brief(self._db, game_id)

    async def get_public_research(
        self,
        game_id: int,
        as_of_date: datetime | None = None,
    ) -> dict[str, Any]:
        return await get_public_research_brief(self._db, game_id)

    # ── Sport context ─────────────────────────────────────────

    def sport_context(self) -> str:
        return (
            "You are writing an NBA game preview in the voice of Earl, "
            "a sharp, confident sports handicapper. "
            "Earl knows basketball inside and out — stats, trends, ATS records, "
            "team narratives, player matchups, and coaching tendencies. "
            "He writes with authority but never arrogance, "
            "and he always backs up his takes with data. "
            "The tone is analytical yet conversational — "
            "like a knowledgeable friend breaking down a game at the bar. "
            "Avoid hype and clichés; focus on the numbers that actually matter."
        )

    # ── Prompts with START/END markers ────────────────────────

    def premium_system_prompt(self, is_historical: bool = False) -> str:
        """System prompt for the premium-only NBA writeup.

        Same format as MLB/base: first line is the title, then blank line,
        then content as plain text with paragraph breaks (double newlines).
        No JSON.
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

        return f"""You are a senior NBA handicapper and analyst for Earl Knows Ball, a premium sports betting analysis site. You write detailed game previews that help bettors make informed decisions.

You will receive RESEARCH DATA below — structured JSON with team stats, betting lines, model predictions, player profiles, injuries, and situational context. Use this data to write a comprehensive premium betting preview.

{tense_note}

Cover these angles in the article:
- Team analysis: offensive and defensive identity, strengths and weaknesses
- Key matchups: star player vs defense, pace battles, bench comparison
- Model's pick: explain what the numbers say (ATS, OU, ML) and why
- Betting angles: line movement, public betting direction, value spots
- Situational factors: rest days, home/away splits, division implications
- Injuries: impact of key players in/out
- Final verdict: concise recommendation with the pick, confidence, and a short rationale

Write with the voice of a sharp handicapper — analytical, confident, data-driven. Pull specific numbers from the research data. Explain WHY the numbers support your read.

FORMATTING: Start with the article TITLE on its own line (use `##` as a heading). Then a blank line. Then the full article formatted in markdown. Use `##` section headers to organize the analysis. Use `**` for emphasis on key numbers/angles. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Player | PTS | REB | AST | FG% |
|--------|-----|-----|-----|-----|
| Luka Dončić | 34.5 | 9.2 | 8.7 | 48.1 |

Bullet lists work for key points in moderation. Keep it article-like — no blockquotes, no emoji, no chat-style formatting."""

    def public_system_prompt(self, is_historical: bool = False) -> str:
        tense = (
            "Past-tense: use 'were', 'had', 'played', etc."
            if is_historical
            else "Future-tense: use 'will', 'should', 'are expected to', etc."
        )
        return f"""You are Earl, a sharp NBA analyst writing a free game preview.

Your job is to preview the game in an informative, engaging way. You do NOT make betting picks — the picks are for premium subscribers only.

{tense}

Write a preview covering:
- Context: what's at stake, standings implications, streaks
- Recent form for both teams
- Key stats that jump out
- Star player watch
- What to watch for

IMPORTANT RULES:
- NEVER include betting picks, ATS advice, or over/under recommendations.
- Do mention betting lines factually ("Lakers are 4.5-point favorites") but don't advise on them.
- Keep it to 300-450 words.
- Be informative and entertaining. No hype or clichés.

FORMATTING: Use `##` for the title on line 1. Use `##` section headers to organize the analysis. Use `**` for emphasis sparingly. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Player | PTS | REB | AST | FG% |
|--------|-----|-----|-----|-----|
| Luka Dončić | 34.5 | 9.2 | 8.7 | 48.1 |

Bullet lists work for key points. Keep it article-like — no blockquotes, no emoji, no chat-style formatting."""

    # ── Message building ────────────────────────────────────

    def _build_messages(self, research: dict[str, Any]) -> str:
        lines = []
        gi = research.get("game_info", {})
        home = gi.get("home_team", {})
        away = gi.get("away_team", {})
        lines.append(f"Game: {home.get('name', '?')} vs {away.get('name', '?')}")
        lines.append(f"Date: {gi.get('formatted_time', '') or gi.get('date', '?')}")
        lines.append(f"Venue: {gi.get('venue', '?')}")
        lines.append(f"Game Type: {gi.get('game_type', 'Regular Season')}")
        lines.append("")

        th = research.get("team_home", {})
        lines.append(f"--- {home.get('name', 'Home Team')} ---")
        if rec := th.get("record"):
            lines.append(f"Record: {rec.get('wins', 0)}-{rec.get('losses', 0)} ({rec.get('pct', 0):.3f})")
        if stats := th.get("stats"):
            if stats.get("ppg"):
                lines.append(f"PPG: {stats['ppg']} | OPPG: {stats['oppg']} | Pt Diff: {stats['pt_diff']}")
        if star := th.get("star_player"):
            lines.append(f"Star: {star.get('name', '')} ({star.get('position', '')}) — {star.get('ppg', 0)} PPG, {star.get('rpg', 0)} RPG, {star.get('apg', 0)} APG")
        if form := th.get("recent_form"):
            results = " ".join(g.get("result", "-") for g in form[:5])
            lines.append(f"Last 5: {results}")
        if ats_val := th.get("ats_recent"):
            lines.append(f"ATS (last {ats_val.get('total', 0)}): {ats_val.get('covered', 0)}-{ats_val.get('total', 0) - ats_val.get('covered', 0)} ({ats_val.get('pct', 0)*100:.0f}%)")

        ta = research.get("team_away", {})
        lines.append(f"\n--- {away.get('name', 'Away Team')} ---")
        if rec := ta.get("record"):
            lines.append(f"Record: {rec.get('wins', 0)}-{rec.get('losses', 0)} ({rec.get('pct', 0):.3f})")
        if stats := ta.get("stats"):
            if stats.get("ppg"):
                lines.append(f"PPG: {stats['ppg']} | OPPG: {stats['oppg']} | Pt Diff: {stats['pt_diff']}")
        if star := ta.get("star_player"):
            lines.append(f"Star: {star.get('name', '')} ({star.get('position', '')}) — {star.get('ppg', 0)} PPG, {star.get('rpg', 0)} RPG, {star.get('apg', 0)} APG")
        if form := ta.get("recent_form"):
            results = " ".join(g.get("result", "-") for g in form[:5])
            lines.append(f"Last 5: {results}")
        if ats_val := ta.get("ats_recent"):
            lines.append(f"ATS (last {ats_val.get('total', 0)}): {ats_val.get('covered', 0)}-{ats_val.get('total', 0) - ats_val.get('covered', 0)} ({ats_val.get('pct', 0)*100:.0f}%)")

        if bets := research.get("betting_lines"):
            lines.append("\n--- BETTING LINES ---")
            for key, val in bets.items():
                if val is not None:
                    label = key.replace("_", " ").title()
                    lines.append(f"  {label}: {val:.1f}" if isinstance(val, float) else f"  {label}: {val}")

        if preds := research.get("model_predictions"):
            lines.append("\n--- MODEL PREDICTIONS ---")
            if preds.get("predicted_home_score"):
                lines.append(f"  Score: {home.get('abbr', 'H')} {preds['predicted_home_score']} — {away.get('abbr', 'A')} {preds['predicted_away_score']}")
            if preds.get("predicted_total"):
                lines.append(f"  Total: {preds['predicted_total']:.1f}")
            if preds.get("predicted_margin"):
                mar = preds["predicted_margin"]
                fav = home.get('abbr') if mar > 0 else away.get('abbr')
                lines.append(f"  Margin: {abs(mar):.1f} ({fav})")
            if preds.get("spread_pick"):
                lines.append(f"  Spread pick: {preds['spread_pick']}")
            if preds.get("ou_pick"):
                lines.append(f"  O/U pick: {preds['ou_pick']} (conf: {preds.get('ou_conf', 0):.2f})")
            if preds.get("ml_pick"):
                lines.append(f"  ML pick: {preds['ml_pick']} (conf: {preds.get('ml_conf', 0):.2f})")

        if h2h := research.get("head_to_head"):
            lines.append(f"\n--- SEASON SERIES ---")
            lines.append(f"  Games: {h2h.get('games_played', 0)} | Home wins: {h2h.get('home_wins', 0)}")

        if standings := research.get("standings"):
            for conf, teams in standings.items():
                lines.append(f"\n--- {conf.upper()} STANDINGS ---")
                for tm_data in teams[:5]:
                    lines.append(f"  {tm_data.get('abbr', '')}: {tm_data.get('wins', 0)}-{tm_data.get('losses', 0)} ({tm_data.get('pct', 0):.3f})")

        # Enrichment — same pattern as NFL
        enrichment = research.get("article_enrichment")
        if enrichment:
            enriched_summary = enrichment.get("enriched_summary", "") if isinstance(enrichment, dict) else ""
            if enriched_summary.strip():
                lines.append("")
                lines.append("--- RECENT ARTICLES CONTEXT ---")
                lines.append(f"  {enriched_summary}")

        return "\n".join(lines)

    # ── JSON conversion ──────────────────────────────────────

    @staticmethod
    def _convert_for_json(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: NBAGameWriteupGenerator._convert_for_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [NBAGameWriteupGenerator._convert_for_json(v) for v in obj]
        if isinstance(obj, Decimal):
            return float(obj)
        return obj

    # ── Store ─────────────────────────────────────────────────

    async def store(
        self,
        game_id: int,
        writeup: dict[str, Any],
        qc_results: list[dict[str, Any]],
        db: AsyncSession | None = None,
    ) -> int:
        """Insert or update the write-up in nba.game_writeups."""
        db = db or self._db
        if db is None:
            raise RuntimeError("No database session available for store()")

        research_brief_json = json.dumps(
            writeup.get("research_brief"), default=str
        ) if writeup.get("research_brief") else None
        qc_json = json.dumps(
            qc_results or writeup.get("quality_checks"), default=str
        ) if (qc_results or writeup.get("quality_checks")) else None

        status = self._derive_status(qc_results)
        is_hist = writeup.get("is_historical", False)

        hist_game_date = None
        if is_hist:
            gi = (writeup.get("research_brief", {}) or {}).get("game_info", {})
            date_str = gi.get("date", "")
            if date_str:
                try:
                    hist_game_date = datetime.fromisoformat(str(date_str))
                except (ValueError, TypeError):
                    pass

        version = 1
        existing = await db.execute(
            text("SELECT id, version FROM nba.game_writeups WHERE game_id = :gid"),
            {"gid": game_id},
        )
        ex = existing.mappings().one_or_none()

        if ex:
            version = ex["version"] + 1
            await db.execute(
                text("""
                    UPDATE nba.game_writeups
                    SET title = :title,
                        public_content = :pub,
                        premium_content = :prem,
                        status = :status,
                        version = :ver,
                        is_historical = :hist,
                        historical_game_date = :hgd,
                        research_brief = CAST(:rb AS jsonb),
                        quality_checks = CAST(:qc AS jsonb),
                        generated_by = :gb,
                        total_tokens = :tt,
                        updated_at = NOW()
                    WHERE id = :eid
                """),
                {
                    "eid": ex["id"],
                    "title": writeup.get("title", ""),
                    "pub": writeup.get("public_content", ""),
                    "prem": writeup.get("premium_content", ""),
                    "status": status,
                    "ver": version,
                    "hist": is_hist,
                    "hgd": hist_game_date,
                    "rb": research_brief_json or "{}",
                    "qc": qc_json or "[]",
                    "gb": writeup.get("generated_by") or "deepseek",
                    "tt": writeup.get("total_tokens") or 0,
                },
            )
            await db.commit()
            return ex["id"]

        result = await db.execute(
            text("""
                INSERT INTO nba.game_writeups
                    (game_id, title, public_content, premium_content,
                     status, version, is_historical, historical_game_date,
                     research_brief, quality_checks, generated_by, total_tokens,
                     created_at, updated_at)
                VALUES
                    (:gid, :title, :pub, :prem,
                     :status, :ver, :hist, :hgd,
                     CAST(:rb AS jsonb), CAST(:qc AS jsonb), :gb, :tt,
                     NOW(), NOW())
                RETURNING id
            """),
            {
                "gid": game_id,
                "title": writeup.get("title", ""),
                "pub": writeup.get("public_content", ""),
                "prem": writeup.get("premium_content", ""),
                "status": status,
                "ver": version,
                "hist": is_hist,
                "hgd": hist_game_date,
                "rb": research_brief_json or "{}",
                "qc": qc_json or "[]",
                "gb": writeup.get("generated_by") or "deepseek",
                "tt": writeup.get("total_tokens") or 0,
            },
        )
        await db.commit()
        return result.scalar()

    # ── status derivation ────────────────────────────────────

    def _derive_status(self, qc_results: list[dict[str, Any]]) -> str:
        if not qc_results:
            return "draft"
        if any(r.get("failed", False) for r in qc_results):
            return "review"
        return "published"

    # ── Public version generation ─────────────────────────────

    async def generate_public(
        self,
        game_id: int,
        research: dict[str, Any],
        is_historical: bool = False,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Generate a public (no picks) write-up."""
        result = await super().generate_public(game_id, research, is_historical)
        qc_results = result.pop("qc_results", [])
        return result, qc_results


# Import research functions at module level
from app.writeups.nba.research import get_research_brief, get_public_research_brief
