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

    schema = "mlb"

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
    # store() is inherited from BaseWriteupGenerator and shared by all three
    # sports (uses self.schema). MLB only adds a post-store hook for the
    # premium Prop Bets article.

    async def _post_store(self, db, game_id: int, research_brief: dict) -> None:
        """After the main write-up commits, generate the premium Prop Bets
        article (stored in the SAME game_writeups row's prop_* columns), then
        render the social/og card. Neither must ever break write-up generation."""
        try:
            await self._generate_props_article(db, game_id, research_brief or {})
        except Exception as e:  # noqa: BLE001
            logger.warning("MLB props article generation failed for game %s: %s", game_id, e)
        # Social card: render the og:image PNG + set preview_image. Non-blocking
        # (runs the sync Playwright render in a thread) and fully guarded so a
        # card failure never fails write-up generation.
        try:
            await self._render_social_card(game_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("MLB social card render failed for game %s: %s", game_id, e)

    async def _render_social_card(self, game_id: int) -> None:
        """Render the MLB game_writeups social card for *game_id* off the event
        loop. Guarded upstream in _post_store; guards here only skip when the
        game lacks both team abbrs with rolling stats."""
        import asyncio
        from sqlalchemy import create_engine, text as _text
        from app.core.config import settings
        from app.social import cards as _cards

        sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")

        def _work() -> str | None:
            engine = create_engine(sync_url)
            try:
                with engine.connect() as conn:
                    row = conn.execute(
                        _text(
                            "SELECT at.abbreviation AS away, ht.abbreviation AS home "
                            "FROM mlb.games g "
                            "JOIN mlb.teams ht ON ht.id = g.home_team_id "
                            "JOIN mlb.teams at ON at.id = g.away_team_id "
                            "WHERE g.id = :gid LIMIT 1"
                        ),
                        {"gid": int(game_id)},
                    ).mappings().first()
                    if not row or not row["away"] or not row["home"]:
                        logger.info("MLB social card: game %s missing team abbrs \u2014 skipping", game_id)
                        return None
                    # Require rolling stats for BOTH teams before rendering.
                    for abbr in (row["away"], row["home"]):
                        has = conn.execute(
                            _text(
                                "SELECT 1 FROM mlb.team_rolling_stats trs "
                                "JOIN mlb.teams t ON t.id = trs.team_id "
                                "WHERE upper(t.abbreviation) = :a AND trs.ra5 IS NOT NULL LIMIT 1"
                            ),
                            {"a": (abbr or "").upper()},
                        ).first()
                        if not has:
                            logger.info(
                                "MLB social card: no rolling stats for %s \u2014 skipping game %s",
                                abbr, game_id,
                            )
                            return None
                return _cards.generate_game_card("mlb", int(game_id), engine)
            finally:
                engine.dispose()

        rel = await asyncio.to_thread(_work)
        if rel:
            logger.info("MLB social card rendered for game %s: %s", game_id, rel)

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
                try:
                    splits = await mlb_props.fetch_player_split_stats(db, player_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("MLB props: split stats failed for %s: %s", name, e)
                    splits = {}
            else:
                splits = {}
            player_context.append(
                {"name": name, "season": season, "recent": recent, "splits": splits, "team_id": team_id}
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
