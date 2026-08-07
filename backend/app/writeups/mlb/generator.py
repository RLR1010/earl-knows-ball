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

        # Serialize JSONB fields for raw SQL inserts
        research_brief_json = json.dumps(
            writeup.get("research_brief"), default=str
        ) if writeup.get("research_brief") else None
        qc_json = json.dumps(
            qc_results or writeup.get("quality_checks"), default=str
        ) if (qc_results or writeup.get("quality_checks")) else None
        accuracy_json = json.dumps(
            writeup.get("accuracy_check"), default=str
        ) if writeup.get("accuracy_check") else None

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
                         seo_description, seo_keywords, slug, published_at)
                    VALUES
                        (:gid, :title, :pub, :prem,
                         CAST(:rb AS jsonb), CAST(:qc AS jsonb), :status, :version,
                         :is_hist, :hist_date,
                         :gen_by, :tokens,
                         CAST(:acc AS jsonb), :acc_tokens,
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
                    "seo_desc": writeup.get("seo_description"),
                    "seo_kw": writeup.get("seo_keywords"),
                    "slug": writeup.get("slug"),
                },
            )
            row_id = result.scalar()

        await db.commit()
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
        with a 1200-2000 word target.
        """
        return await super().generate_public(game_id, research, is_historical)
