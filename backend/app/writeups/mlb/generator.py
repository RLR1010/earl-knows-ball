"""MLB write-up generator — ties research to DeepSeek generation, stores results."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import MLBGameWriteup
from app.writeups.base_generator import BaseWriteupGenerator, QCResults
from app.writeups.mlb.research import get_research_brief, get_public_research_brief

logger = logging.getLogger("writeups")


def _format_title_date(value, fallback: str = "") -> str:
    """Safely format a game date into "Aug 9, 2026" for the prop title."""
    if not value:
        return fallback
    # ISO datetime strings like "2026-08-09T20:20:00-04:00"
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(value, fmt).strftime("%b %-d, %Y")
            except ValueError:
                continue
        # last resort: pull the YYYY-MM-DD prefix
        import re

        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if m:
            return datetime.strptime(m.group(1), "%Y-%m-%d").strftime("%b %-d, %Y")
        return fallback
    if hasattr(value, "strftime"):
        return value.strftime("%b %-d, %Y")
    return fallback


class MLBWriteupGenerator(BaseWriteupGenerator):
    """MLB-specific write-up generator."""

    async def research_brief(
        self, game_id: int, as_of_date: Optional[datetime] = None
    ) -> dict[str, Any]:
        """Delegate to the MLB research module."""
        # We need a DB session. This is passed in via *generate()*.
        # The caller is responsible for providing it — we store it on self.
        if not hasattr(self, "_db") or self._db is None:
            return {"error": "No database session available"}
        return await get_research_brief(self._db, game_id, as_of_date)

    def sport_context(self) -> str:
        return "Major League Baseball"

    async def generate(
        self,
        db: AsyncSession,
        game_id: int,
        is_historical: bool = False,
        as_of_date: Optional[datetime] = None,
        reasoning: str = "minimal",  # thinking enabled + minimal reasoning (works)
        usage_log: Optional[list[dict[str, Any]]] = None,
    ) -> tuple[dict[str, Any], QCResults]:
        """Full pipeline with DB session."""
        self._db = db
        result = await super().generate(
            game_id, is_historical, as_of_date, reasoning=reasoning, usage_log=usage_log
        )
        self._db = None
        if "error" in result:
            return result, []
        qc_results = result.pop("qc_results", [])
        return result, qc_results

    # ── Storage ─────────────────────────────────────────────

    async def store(
        self,
        game_id: int,
        writeup: dict[str, Any],
        qc_results: list[dict[str, Any]],
    ) -> int:
        """Insert or update the write-up in the database. Returns the row id."""
        db = self._db
        import json

        # Nest the per-call usage log inside research_brief so it persists through
        # the existing JSONB column (it was previously computed in generate() but
        # never written to the DB — only the aggregated total_tokens was saved).
        research_brief = dict(writeup.get("research_brief") or {})
        if writeup.get("usage_log") is not None:
            research_brief["_usage_log"] = writeup["usage_log"]
        if writeup.get("total_tokens") is not None:
            research_brief["_total_tokens"] = writeup["total_tokens"]
        research_brief_json = json.dumps(
            research_brief, default=str
        ) if research_brief else None
        qc_json = json.dumps(
            qc_results or writeup.get("quality_checks"), default=str
        ) if (qc_results or writeup.get("quality_checks")) else None
        accuracy_json = json.dumps(
            writeup.get("accuracy_check"), default=str
        ) if writeup.get("accuracy_check") else None
        rejection_json = json.dumps(
            writeup.get("rejection_history") or [], default=str
        ) if (writeup.get("rejection_history") or []) else None

        status = self._derive_status(qc_results)
        is_hist = writeup.get("is_historical", False)

        hist_game_date = None
        if is_hist:
            game_summary = (writeup.get("research_brief", {}) or {}).get("game_summary", {})
            date_str = game_summary.get("date", "")
            if date_str:
                try:
                    hist_game_date = datetime.fromisoformat(date_str)
                except (ValueError, TypeError):
                    pass

        version = 1

        # Check existing
        existing = await db.execute(
            text("SELECT id, version FROM mlb.game_writeups WHERE game_id = :gid"),
            {"gid": game_id},
        )
        ex = existing.mappings().one_or_none()

        if ex:
            version = ex["version"] + 1
            result = await db.execute(
                text("""
                    UPDATE mlb.game_writeups SET
                        title = :title,
                        public_content = :pub,
                        premium_content = :prem,
                        research_brief = CAST(:rb AS jsonb),
                        quality_checks = CAST(:qc AS jsonb),
                        status = :status,
                        version = :version,
                        is_historical = :is_hist,
                        historical_game_date = :hist_date,
                        generated_by = :gen_by,
                        total_tokens = :tokens,
                        accuracy_check = CAST(:acc AS jsonb),
                        accuracy_check_tokens = :acc_tokens,
                        rejection_history = CAST(:rej AS jsonb),
                        seo_description = :seo_desc,
                        seo_keywords = :seo_kw,
                        slug = :slug,
                        published_at = NOW(),
                        updated_at = NOW()
                    WHERE game_id = :gid
                    RETURNING id
                """),
                {
                    "gid": game_id,
                    "title": writeup.get("title", ""),
                    "pub": writeup.get("public_content", ""),
                    "prem": writeup.get("premium_content", ""),
                    "rb": research_brief_json,
                    "qc": qc_json,
                    "status": status,
                    "version": version,
                    "is_hist": is_hist,
                    "hist_date": hist_game_date,
                    "gen_by": writeup.get("generated_by", self.MODEL),
                    "tokens": writeup.get("total_tokens"),
                    "acc": accuracy_json,
                    "acc_tokens": writeup.get("accuracy_check_tokens"),
                    "rej": rejection_json,
                    "seo_desc": writeup.get("seo_description"),
                    "seo_kw": writeup.get("seo_keywords"),
                    "slug": writeup.get("slug"),
                },
            )
            row_id = result.scalar()
        else:
            result = await db.execute(
                text("""
                    INSERT INTO mlb.game_writeups
                        (game_id, title, public_content, premium_content,
                         research_brief, quality_checks, status, version,
                         is_historical, historical_game_date,
                         generated_by, total_tokens,
                         accuracy_check, accuracy_check_tokens,
                         rejection_history,
                         seo_description, seo_keywords, slug, published_at)
                    VALUES
                        (:gid, :title, :pub, :prem,
                         CAST(:rb AS jsonb), CAST(:qc AS jsonb), :status, :version,
                         :is_hist, :hist_date,
                         :gen_by, :tokens,
                         CAST(:acc AS jsonb), :acc_tokens,
                         CAST(:rej AS jsonb),
                         :seo_desc, :seo_kw, :slug, NOW())
                    RETURNING id
                """),
                {
                    "gid": game_id,
                    "title": writeup.get("title", ""),
                    "pub": writeup.get("public_content", ""),
                    "prem": writeup.get("premium_content", ""),
                    "rb": research_brief_json,
                    "qc": qc_json,
                    "status": status,
                    "version": version,
                    "is_hist": is_hist,
                    "hist_date": hist_game_date,
                    "gen_by": writeup.get("generated_by", self.MODEL),
                    "tokens": writeup.get("total_tokens"),
                    "acc": accuracy_json,
                    "acc_tokens": writeup.get("accuracy_check_tokens"),
                    "rej": rejection_json,
                    "seo_desc": writeup.get("seo_description"),
                    "seo_kw": writeup.get("seo_keywords"),
                    "slug": writeup.get("slug"),
                },
            )
            row_id = result.scalar()

        await db.commit()

        # Generate a separate premium Prop Bets article for this game (post-
        # commit so the main row definitely exists). Skipped when the game has
        # no player prop odds. Failure here must NOT fail the main write-up.
        try:
            await self._generate_props_article(db, game_id, research_brief or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("MLB props article step failed for game %s: %s", game_id, e)

        return row_id

    def _derive_status(self, qc_results: list[dict[str, Any]]) -> str:
        """Write-ups go live immediately — no draft/review workflow."""
        return "published"

    async def generate_public(
        self,
        game_id: int,
        research: dict[str, Any],
        is_historical: bool = False,
    ) -> dict[str, Any]:
        """Generate a public-only write-up (no picks, no premium section).

        The caller is responsible for passing a stripped research brief
        (from get_public_research_brief). Makes a separate shorter LLM call
        with a 700-900 word target.
        """
        return await super().generate_public(game_id, research, is_historical)

    async def _generate_props_article(
        self,
        db: AsyncSession,
        game_id: int,
        research_brief: dict[str, Any],
    ) -> None:
        """Generate a separate premium Prop Bets article for *game_id*.

        Skips entirely (no LLM call) when the game has no player prop odds.
        Writes the resulting short article into the same game_writeups row's
        ``prop_*`` columns.
        """
        import app.writeups.props_article as shared
        import app.writeups.props_mlb as mlb_props

        cfg = shared.SPORT_CONFIGS["mlb"]()

        props = await shared.fetch_game_props(db, cfg, game_id)
        if not props:
            logger.info("MLB props article: game %s has no prop odds — skipping", game_id)
            return
        logger.info("MLB props article: game %s has %d prop lines", game_id, len(props))

        # Title from the research brief, e.g. "Prop Bets for HOU vs SD — Aug 9, 2026".
        research = research_brief
        summary = research.get("game_summary") or {}
        home_abbr = (summary.get("home_team") or {}).get("abbreviation") or "HOME"
        away_abbr = (summary.get("away_team") or {}).get("abbreviation") or "AWAY"
        game_date = summary.get("date")
        game_date = _format_title_date(game_date)
        title = shared.build_title(away_abbr, home_abbr, game_date)

        prop_players = mlb_props.extract_prop_players(props)
        season_lookup = mlb_props.build_season_lookup(research)

        # Build per-player context: season stats (from research rosters) +
        # recent game stats (last N batting lines from batting_game_stats).
        player_context = []
        for name, team_id in prop_players.items():
            norm = mlb_props._norm(name)
            season = season_lookup.get(norm)
            recent = None
            player_id = season.get("player_id") if season else None
            if player_id is None:
                try:
                    player_id = await mlb_props.resolve_player_id(db, name, team_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("MLB props: player id lookup failed for %s: %s", name, e)
            if player_id:
                try:
                    recent_lines = await mlb_props.fetch_player_recent_stats(db, player_id)
                    if recent_lines:
                        recent = {
                            "last_n": len(recent_lines),
                            "lines": recent_lines,
                        }
                except Exception as e:  # noqa: BLE001
                    logger.warning("MLB props: recent stats failed for %s: %s", name, e)
            player_context.append(
                {"name": name, "season": season, "recent": recent, "team_id": team_id}
            )

        system = shared.build_system_prompt()
        # Send a representative slice of the props to the LLM (games can have
        # 600+ lines; it only needs enough to pick a few).
        props_for_llm = props[: shared.MAX_PROPS_TO_SEND] if len(props) > shared.MAX_PROPS_TO_SEND else props
        user_prompt = shared.build_user_prompt(cfg, props_for_llm, player_context, research)

        usage_log: list[dict[str, Any]] = []
        content = await self._call_deepseek(
            system,
            user_prompt,
            max_tokens=2000,
            reasoning="minimal",
            usage_log=usage_log,
            call="generate_props_article",
        )
        if not content:
            logger.warning("MLB props article: LLM returned no content for game %s — skipping", game_id)
            return

        total_tokens = sum(u.get("total_tokens") or 0 for u in usage_log)

        # Persist the exact research that was shown to the LLM for the props
        # article (prop odds + per-player season/recent stats) into the row's
        # Research Context so it reflects everything the model saw.
        prop_research = {
            "game_id": int(game_id),
            "prop_count": len(props),
            "prop_lines_sent": len(props_for_llm),
            "props": props,
            "players": player_context,
        }
        await shared.save_props_article(
            db, cfg, game_id, title, content.strip(),
            generated_by="mlb-prop-bets", total_tokens=total_tokens or 0,
            prop_research=prop_research,
        )
        logger.info("MLB props article saved for game %s (%d tokens)", game_id, total_tokens)
