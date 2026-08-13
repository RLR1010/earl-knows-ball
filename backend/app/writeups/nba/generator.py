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


def _format_title_date(value, fallback: str = "") -> str:
    """Safely format a game date into "Aug 9, 2026" for the prop title."""
    if not value:
        return fallback
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).strftime("%b %-d, %Y")
            except ValueError:
                continue
    return fallback


def _format_nba_splits(splits: dict) -> list[str]:
    """Format NBA player-split lines into readable strings for the prompt.

    splits keys are '<split_type>.<scope>' (scope: career|season), values are
    dicts with per-game rates.
    """
    labels = {
        "home": "Home", "away": "Away", "vs_east": "vs East",
        "vs_west": "vs West", "starter": "as Starter", "bench": "off Bench",
        "rest0": "Back-to-back", "rest_ge1": "1+ rest",
    }
    lines = []
    for key in sorted(splits):
        if "." not in key:
            continue
        st, scope = key.split(".", 1)
        v = splits[key]
        if not isinstance(v, dict) or "games" not in v:
            continue
        scope_txt = "Career" if scope == "career" else "Season"
        lab = labels.get(st, st.replace("_", " ").title())
        lines.append(
            f"{lab} ({scope_txt}, {v.get('games')}g): "
            f"{v.get('pts')}/g pts, {v.get('reb')}/g reb, {v.get('ast')}/g ast, "
            f"{v.get('stl')}/g stl, {v.get('blk')}/g blk, {v.get('tov')}/g tov"
        )
    return lines



class NBAGameWriteupGenerator(BaseWriteupGenerator):
    """Generator for NBA game write-ups."""

    SPORT = "nba"
    schema = "nba"

    # ── Generate (overridden to inject db) ─────────────────────

    async def generate(
        self,
        db: AsyncSession,
        game_id: int,
        is_historical: bool = False,
        as_of_date: datetime | None = None,
        reasoning: str = "minimal",
        usage_log: Optional[list[dict]] = None,
    ):
        """Full pipeline with DB session. Follows NFL pattern."""
        self._db = db
        result = await super().generate(
            game_id, is_historical, as_of_date, reasoning=reasoning, usage_log=usage_log
        )
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

Bullet lists work for key points in moderation. Keep it article-like — no blockquotes, no emoji, no chat-style formatting.

Length: 1000-1400 words. This is a HARD LIMIT — write 1000-1400 words, target ~1200. Do not exceed 1400 words. Be detailed and comprehensive, but every section must earn its length — cut filler rather than padding past 1400."""

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
- Keep it to 700-900 words.
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

    # ── Storage ─────────────────────────────────────────────
    # store() is inherited from BaseWriteupGenerator (shared across all three
    # sports; writes to self.schema = 'nba'). No NBA-specific store logic remains.

    def _derive_status(self, qc_results: list[dict[str, Any]]) -> str:
        """Write-ups go live immediately — no draft/review workflow."""
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

    # ── Premium: Prop Bets article (mirrors MLB flow) ──────────

    async def _post_store(self, db, game_id, research_brief) -> None:
        """After the main writeup is committed, generate+store the premium
        "Prop Bets" article if the game has DraftKings player props.
        Wrapped so a props failure never fails the main writeup.
        """
        try:
            await self._generate_props_article(db, game_id, research_brief)
        except Exception as e:  # noqa: BLE001
            logger.exception("NBA props article failed for game %s: %s", game_id, e)

    async def _generate_props_article(self, db, game_id, research_brief) -> None:
        """Generate + store the premium Prop Bets article on game %s.

        Mirrors MLB's generator.store() -> _generate_props_article: fetch the
        DraftKings player props for the game, build per-player research context
        (season stats, recent form, splits), then ask the LLM to write a
        premium prop-bet piece. Saves into the same game_writeups row's
        prop_* columns.
        """
        import app.writeups.props_article as shared
        import app.writeups.props_nba as nba_props

        cfg = shared.SPORT_CONFIGS["nba"]()

        props = await shared.fetch_game_props(db, cfg, game_id)
        if not props:
            logger.info("NBA props article: game %s has no prop odds — skipping", game_id)
            return
        logger.info("NBA props article: game %s has %d prop lines", game_id, len(props))

        research = research_brief or {}
        summary = research.get("game_info") or {}
        home_abbr = (summary.get("home_team") or {}).get("abbreviation") or "HOME"
        away_abbr = (summary.get("away_team") or {}).get("abbreviation") or "AWAY"
        game_date = _format_title_date(summary.get("date"))
        title = shared.build_title(away_abbr, home_abbr, game_date)

        prop_players = nba_props.extract_prop_players(props)

        # Season-stats lookup from the research brief's team rosters
        # (nested under team_home.roster / team_away.roster), keys by
        # accent-normalized name. Fall back to a direct DB query for any
        # prop player not in the brief rotation.
        season_lookup = nba_props.build_season_lookup(research)

        # Build per-player context. Season stats come from the brief roster
        # (or nba.player_season_stats as fallback); recent form from
        # nba.player_game_stats; splits from nba.player_splits.
        player_context = []
        for name in prop_players:
            team_id = None
            season = season_lookup.get(nba_props._norm(name))
            if not season:
                season = await nba_props.fetch_player_season_stats(db, name, team_id)
            recent = None
            player_id = season.get("player_id") if season else None
            if player_id is None:
                try:
                    player_id = await nba_props.resolve_player_id(db, name, team_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("NBA props: player id lookup failed for %s: %s", name, e)
            splits = {}
            if player_id:
                try:
                    recent = await nba_props.fetch_player_recent_stats(db, name, team_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("NBA props: recent stats failed for %s: %s", name, e)
                try:
                    splits = await nba_props.fetch_player_split_stats(db, name, team_id)
                except Exception as e:  # noqa: BLE001
                    logger.warning("NBA props: split stats failed for %s: %s", name, e)

            rec_txt = None
            if isinstance(recent, dict) and recent.get("recent_games"):
                lines = recent["recent_games"]
                parts = []
                for ln in lines[:5]:
                    parts.append(
                        f"{ln.get('points')}pts/{ln.get('rebounds_total')}reb/"
                        f"{ln.get('assists')}ast/{ln.get('steals')}stl/"
                        f"{ln.get('three_pointers_made')}x3pm"
                    )
                rec_txt = f"Last {len(lines)} games: " + ", ".join(parts)

            season_txt = None
            if season:
                season_txt = (
                    f"Season {season.get('games_played')}g: "
                    f"{season.get('points_per_game')}/g pts, "
                    f"{season.get('rebounds_per_game')}/g reb, "
                    f"{season.get('assists_per_game')}/g ast, "
                    f"{season.get('steals')}/g stl, {season.get('blocks')}/g blk, "
                    f"{season.get('turnovers')}/g tov, "
                    f"FG% {season.get('field_goal_pct')}, 3P% {season.get('three_point_pct')}, "
                    f"FT% {season.get('free_throw_pct')}, TS% {season.get('true_shooting_pct')}, "
                    f"+/ - {season.get('plus_minus')}, usage {season.get('usage_pct')}"
                )

            split_txt = None
            if splits:
                split_lines = _format_nba_splits(splits)
                if split_lines:
                    split_txt = "Splits: " + "; ".join(split_lines)

            player_context.append(
                {
                    "name": name,
                    "season": season_txt,
                    "recent": rec_txt,
                    "splits": split_txt,
                    "team_id": team_id,
                }
            )

        props_for_llm = props[: cfg.get("max_props_to_send", 20)]
        user_prompt = shared.build_user_prompt(
            cfg, props_for_llm, player_context, research
        )

        usage_log = []
        content = await self._call_deepseek(
            user_prompt,
            max_tokens=2000,
            reasoning="minimal",
            usage_log=usage_log,
            call="generate_props_article",
        )
        if not content:
            logger.warning("NBA props article: LLM returned no content for game %s — skipping", game_id)
            return

        total_tokens = sum(u.get("total_tokens") or 0 for u in usage_log)
        prop_research = {
            "game_id": int(game_id),
            "prop_count": len(props),
            "prop_lines_sent": len(props_for_llm),
            "props": props,
            "players": player_context,
        }
        await shared.save_props_article(
            db, cfg, game_id, title, content.strip(),
            generated_by="nba-prop-bets", total_tokens=total_tokens or 0,
            prop_research=prop_research,
        )
        logger.info("NBA props article saved for game %s (%d tokens)", game_id, total_tokens)


# Import research functions at module level
from app.writeups.nba.research import get_research_brief, get_public_research_brief
