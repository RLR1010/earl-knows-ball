"""NFL writeup generator — produces public and premium game previews.

Subclasses BaseWriteupGenerator with NFL-specific system prompts
and custom message building for the rich nested research structure.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.writeups.base_generator import BaseWriteupGenerator
from app.writeups.nfl.research import (
    get_research_brief,
    get_public_research_brief,
)

logger = logging.getLogger("writeups.nfl.generator")


class NFLWriteupGenerator(BaseWriteupGenerator):
    """Generates NFL game preview writeups (public & premium)."""

    SPORT = "nfl"

    def premium_system_prompt(self, is_historical: bool = False) -> str:
        """System prompt for premium (with picks) NFL writeups."""
        tense_note = self._tense_note(is_historical)

        return f"""You are a senior NFL handicapper and analyst for Earl Knows Ball, a premium sports betting analysis site. You write detailed game previews that help bettors make informed decisions.

You will receive RESEARCH DATA below — structured JSON with team stats, betting lines, model predictions, player profiles, injuries, and situational context. Use this data to write a comprehensive premium betting preview.

The article must cover:
- Team analysis: offensive and defensive identity, strengths and weaknesses
- Key matchups: QB play, offensive line vs defensive front, skill player advantages
- Model's pick: explain what the numbers say (ATS, OU, ML) and why
- Betting angles: line movement, public betting direction, value spots
- Situational factors: rest days, home/away splits, division implications, weather
- Injuries: impact of key players in/out
- Final verdict: concise recommendation with the pick, confidence, and a short rationale

Write with the voice of a sharp handicapper — analytical, confident, data-driven. Pull specific numbers from the research data (yards per game, turnover margins, pace metrics). Explain WHY the numbers support your read.

⚠️ HUMAN VOICE DIRECTIVES (CRITICAL):
The article content must read like a real human sports handicapper wrote it — natural, flowing prose, not AI-generated text. Vary sentence length and structure. Avoid robotic formulas like "Let's break down..." or "Here's a deep dive..." — write naturally. This should read as if a seasoned handicapper sat down and wrote their analysis, not a language model.

FORMATTING: This renders as a web article via markdown. Use `##` section headers to organize the analysis. Use `**` for emphasis on key numbers/angles. Bullet lists work for key points in moderation. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Quarterback | CMP% | YDS | TD | INT |
|-------------|------|-----|----|-----|
| C.J. Stroud | 66.3 | 4871 | 32 | 13 |

Keep it article-like — no blockquotes, no emoji, no chat-style formatting.

Output format (preferred): Return valid JSON with these keys:
  - "title": A punchy, engaging title for the premium section (include team names, max ~80 chars)
  - "content": The full premium article content formatted in markdown

If you cannot return JSON, write the article starting with `## Title` on line 1, then the markdown body.
{tense_note}"""

    def public_system_prompt(self, is_historical: bool = False) -> str:
        """System prompt for public (no picks) NFL writeups."""
        tense_note = self._tense_note(is_historical)

        return f"""You are a football writer for Earl Knows Ball, a sports analysis site. Write a game preview/article for the general public.

This is a game preview — not a betting analysis. Write in the style of a well-informed beat writer: insightful, engaging, and authoritative.

Write about:
- The matchup: what makes this game interesting
- Team identity: how each team wins games
- Key players to watch: QBs, playmakers, defensive stars
- Storylines: division implications, streaks, narratives
- What to expect: style of game, key matchups on the field

⚠️ HUMAN VOICE DIRECTIVES (CRITICAL):
Write like a real human sports journalist — natural, flowing prose, not an AI-generated article. Vary sentence length and structure. No robotic formulas like "Let's break down..." or "Here's what you need to know..." — write naturally. This should read as if a beat writer pounded it out on their laptop, not a language model.

FORMATTING: This renders as a web article via markdown. Use `##` for the title on line 1. Use `##` section headers to organize the body. Use `**` for emphasis sparingly. Bullet lists work for key points. For tables, use proper pipe-and-dash markdown syntax with a separator row:

| Quarterback | CMP% | YDS | TD | INT |
|-------------|------|-----|----|-----|
| C.J. Stroud | 66.3 | 4871 | 32 | 13 |

Keep it article-like — no blockquotes, no emoji, no chat-style formatting.
{tense_note}"""

    async def generate(
        self,
        db: AsyncSession,
        game_id: int,
        is_historical: bool = False,
        as_of_date: Optional[date] = None,
    ) -> Any:
        """Full pipeline with DB session."""
        self._db = db
        result = await super().generate(game_id, is_historical, as_of_date)
        self._db = None
        return result

    async def research_brief(
        self,
        game_id: int,
        as_of_date: Optional[date] = None,
    ) -> dict[str, Any]:
        """Fetch the full research brief for a game (includes model picks)."""
        db = getattr(self, "_db", None)
        return await get_research_brief(db, game_id, as_of_date)

    async def get_public_research(
        self,
        game_id: int,
        as_of_date: Optional[date] = None,
    ) -> dict[str, Any]:
        """Fetch the public (no picks) research brief."""
        db = getattr(self, "_db", None)
        return await get_public_research_brief(db, game_id, as_of_date)

    def sport_context(self) -> str:
        return "NFL football"

    # ── Store Override ───────────────────────────────────────

    @staticmethod
    def _convert_for_json(obj: Any) -> Any:
        """Recursively convert non-serializable types (Decimal, etc.)."""
        if isinstance(obj, dict):
            return {k: NFLWriteupGenerator._convert_for_json(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [NFLWriteupGenerator._convert_for_json(v) for v in obj]
        if isinstance(obj, Decimal):
            return float(obj)
        return obj

    async def store(
        self,
        game_id: int,
        writeup: dict[str, Any],
        qc_results: list[dict[str, Any]],
    ) -> int:
        """Insert or update the write-up in `nfl.game_writeups`."""
        db = self._db
        from app.models.nfl.writeup import NFLGameWriteup

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

        research_data = NFLWriteupGenerator._convert_for_json(writeup.get("research_brief") or None)
        qc_data = NFLWriteupGenerator._convert_for_json(qc_results or writeup.get("quality_checks") or None)

        existing_row = await db.execute(
            text("SELECT id, version FROM nfl.game_writeups WHERE game_id = :gid"),
            {"gid": game_id},
        )
        existing = existing_row.mappings().one_or_none()

        if existing:
            writeup_obj = await db.get(NFLGameWriteup, existing["id"])
            if writeup_obj:
                writeup_obj.version = existing["version"] + 1
                writeup_obj.title = writeup.get("title", "")
                writeup_obj.public_content = writeup.get("public_content", "")
                writeup_obj.premium_content = writeup.get("premium_content", "")
                writeup_obj.research_brief = research_data
                writeup_obj.quality_checks = qc_data
                writeup_obj.status = status
                writeup_obj.is_historical = is_hist
                writeup_obj.historical_game_date = hist_game_date
                db.add(writeup_obj)
        else:
            writeup_obj = NFLGameWriteup(
                game_id=game_id,
                version=1,
                title=writeup.get("title", ""),
                public_content=writeup.get("public_content", ""),
                premium_content=writeup.get("premium_content", ""),
                research_brief=research_data,
                quality_checks=qc_data,
                status=status,
                is_historical=is_hist,
                historical_game_date=hist_game_date,
            )
            db.add(writeup_obj)

        await db.flush()
        await db.commit()
        return writeup_obj.id

    def _derive_status(self, qc_results: list[dict[str, Any]]) -> str:
        """Auto-set status based on quality checks."""
        if not qc_results:
            return "draft"
        passed = sum(1 for q in qc_results if q.get("passed"))
        total = len(qc_results)
        if passed == total:
            return "review"
        if passed >= total / 2:
            return "draft"
        return "draft"

    # ── Message Builder Override ─────────────────────────────

    def _build_messages(self, research: dict) -> str:
        """Build the user prompt from the NFL research dict.

        Overrides the base class to handle the rich nested structure
        returned by get_research_brief().
        """
        lines = ["--- RESEARCH DATA ---"]
        game_info = research.get("game_info", {})

        # Game header
        home_team = game_info.get("home_team", {})
        away_team = game_info.get("away_team", {})
        home_name = home_team.get("name", "Home")
        away_name = away_team.get("name", "Away")
        home_abbr = home_team.get("abbr", "")
        away_abbr = away_team.get("abbr", "")

        lines.append(f"Game: {away_name} ({away_abbr}) @ {home_name} ({home_abbr})")
        lines.append(f"Date: {game_info.get('formatted_time')}")
        lines.append(f"Week: {game_info.get('week')}  Season Type: {game_info.get('season_type')}")
        lines.append(f"Venue: {game_info.get('venue')}  Roof: {game_info.get('roof_type')}")
        lines.append(f"Surface: {game_info.get('surface')}")

        # Weather
        weather = game_info.get("weather")
        if weather:
            lines.append(f"Weather: {weather.get('temperature')}°F, Wind {weather.get('wind_speed')}mph, {weather.get('condition')}")

        # Betting lines
        betting = research.get("betting_lines", {})
        if betting and "error" not in betting:
            lines.append("\n--- BETTING LINES ---")
            lines.append(f"Spread: {betting.get('spread')} (line: {betting.get('spread_line')})")
            lines.append(f"Over/Under: {betting.get('over_under')} (line: {betting.get('ou_line')})")
            lines.append(f"Moneyline: {away_abbr} {betting.get('away_moneyline')} | {home_abbr} {betting.get('home_moneyline')}")

            movement = betting.get("line_movement", {})
            if movement.get("spread"):
                s = movement["spread"]
                lines.append(f"Spread Movement: opened {s.get('opened')}, current {s.get('current')} (moved {s.get('movement')})")
            if movement.get("over_under"):
                o = movement["over_under"]
                lines.append(f"OU Movement: opened {o.get('opened')}, current {o.get('current')} (moved {o.get('movement')})")

            # Model predictions
            preds = betting.get("model_predictions")
            if preds:
                lines.append("\n--- MODEL PREDICTIONS ---")
                lines.append(f"Home ATS probability: {preds.get('home_ats_prob')}")
                lines.append(f"Away ATS probability: {preds.get('away_ats_prob')}")
                lines.append(f"Over probability: {preds.get('over_prob')}")
                lines.append(f"Home ML: {preds.get('home_ml_prob')}  Away ML: {preds.get('away_ml_prob')}")
                ps = preds.get("predicted_score", {})
                lines.append(f"Predicted Score: {away_abbr} {ps.get('away')} - {home_abbr} {ps.get('home')}")
                lines.append(f"ATS Pick: {preds.get('ats_pick')}  ML Pick: {preds.get('ml_pick')}")
                lines.append(f"Confidence: {preds.get('confidence')}")

        # Teams
        teams = research.get("teams", {})
        for side in ("home", "away"):
            team_data = teams.get(side, {})
            team_name = home_name if side == "home" else away_name
            team_abbr = home_abbr if side == "home" else away_abbr
            label = f"{team_name} ({team_abbr})"

            lines.append(f"\n{'='*60}")
            lines.append(f"  {label}")
            lines.append(f"{'='*60}")

            # Record
            record = team_data.get("record", {})
            if record:
                lines.append(f"Record: {record.get('overall')} (Home: {record.get('home')}, Away: {record.get('away')}, Div: {record.get('division')}, Conf: {record.get('conference')})")
                lines.append(f"Win%: {record.get('win_pct')}")

            # Season stats
            stats = team_data.get("season_stats", {})
            if stats:
                off = stats.get("offense", {})
                defense = stats.get("defense", {})
                lines.append(f"\n  Offense: {off.get('ppg')} PPG, {off.get('ypg')} YPG ({off.get('pass_ypg')} pass / {off.get('rush_ypg')} rush)")
                lines.append(f"  Pass: {off.get('pass_att_per_game')} att/game, {off.get('pass_td_per_game')} TD/game, {off.get('int_per_game')} INT/game")
                lines.append(f"  Rush: {off.get('rush_att_per_game')} att/game, {off.get('rush_td_per_game')} TD/game")
                lines.append(f"  Sacks allowed: {off.get('sacks_per_game')}/game")
                lines.append(f"  Turnovers: {off.get('turnovers_per_game')}/game")
                lines.append(f"\n  Defense: {defense.get('oppg')} PPG allowed, {defense.get('def_ypg')} YPG ({defense.get('def_pass_ypg')} pass / {defense.get('def_rush_ypg')} rush)")
                lines.append(f"  Def INTs: {defense.get('def_int_per_game')}/game, Sacks: {defense.get('def_sacks_per_game')}/game")
                lines.append(f"  Takeaways: {stats.get('takeaways_per_game')}/game")
                lines.append(f"  TO Diff: {stats.get('turnover_diff_per_game')}/game")

            # Rankings
            rankings = team_data.get("rankings", {})
            if rankings:
                lines.append(f"\n  Rankings (out of {rankings.get('ppg', {}).get('total', '')} teams):")
                for cat, info in rankings.items():
                    if isinstance(info, dict) and "rank" in info:
                        lines.append(f"    {cat}: #{info['rank']} ({info.get('value')})")

            # QB
            qb = team_data.get("qb")
            if qb:
                lines.append(f"\n  QB: {qb.get('name')}")
                qbs = qb.get("season_stats", {})
                if qbs:
                    lines.append(f"    Season: {qbs.get('pass_yds')} yds, {qbs.get('pass_td')} TD, {qbs.get('pass_int')} INT, {qbs.get('comp_pct')}% cmp, {qbs.get('qb_rating')} rating")
                    lines.append(f"    Rush: {qbs.get('rush_yds')} yds, {qbs.get('rush_td')} TD, {qbs.get('yds_per_game')} yds/game")

                recent = qb.get("recent_games", [])
                if recent:
                    lines.append(f"    Recent games:")
                    for rg in recent:
                        lines.append(f"      Wk {rg.get('week')} vs {rg.get('opponent')}: {rg.get('result')} {rg.get('score')} — {rg.get('pass_yds')}yds/{rg.get('pass_td')}/{rg.get('pass_int')}, {rg.get('comp_pct')}% cmp, rush {rg.get('rush_yds')}/{rg.get('rush_td')}")

            # Key players
            players = team_data.get("key_players", [])
            if players:
                lines.append(f"\n  Key Skill Players:")
                for p in players[:6]:
                    avg = p.get("avg_per_game", {})
                    if p.get("position") in ("RB",):
                        lines.append(f"    {p['name']} ({p['position']}): {avg.get('rush_yds')} rush yds/game, {avg.get('yards_per_carry')} ypc, {avg.get('recv_yds')} recv yds/game")
                    elif p.get("position") in ("WR", "TE"):
                        lines.append(f"    {p['name']} ({p['position']}): {avg.get('receptions')} rec/game, {avg.get('recv_yds')} yds/game, {avg.get('yards_per_reception')} ypr, {avg.get('targets')} tgt/game")

            # Recent form
            form = team_data.get("recent_form", [])
            if form:
                lines.append(f"\n  Recent Form (last {len(form)}):")
                for fg in form:
                    lines.append(f"    Wk {fg.get('week')}: {fg.get('result')} {fg.get('score')} {'vs' if fg.get('location') == 'home' else 'at'} {fg.get('opponent')} ({fg.get('date', '')[:10] if fg.get('date') else ''})")

            # Pace
            pace = team_data.get("pace")
            if pace:
                lines.append(f"\n  Pace: {pace.get('plays_per_game')} plays/game")
                lines.append(f"  Play-calling: {pace.get('pass_play_pct')}% pass, {pace.get('rush_play_pct')}% rush ({pace.get('pass_attempts_per_game')} pass att/g, {pace.get('rush_attempts_per_game')} rush att/g)")

        # Defensive matchups
        matchups = research.get("defensive_matchups", {})
        for matchup_key, matchup_data in matchups.items():
            if matchup_data:
                lines.append(f"\n--- {matchup_key.replace('_', ' ').title()} ---")
                od = matchup_data.get("offense_vs_defense", {})
                if od:
                    lines.append(f"  Pass: Off {od.get('off_pass_ypg')} vs Def allows {od.get('def_pass_allowed')} (advantage: {od.get('pass_advantage')})")
                    lines.append(f"  Run: Off {od.get('off_rush_ypg')} vs Def allows {od.get('def_rush_allowed')} (advantage: {od.get('run_advantage')})")
                tendency = matchup_data.get("offense_tendency", {})
                if tendency:
                    lines.append(f"  Offense: {tendency.get('pass_ypg_pct')}% pass, {tendency.get('rush_ypg_pct')}% run ({tendency.get('pass_att_pg')} pass att/g, {tendency.get('rush_att_pg')} rush att/g)")
                dst = matchup_data.get("defense_strength", {})
                if dst:
                    lines.append(f"  Defense: {dst.get('sacks_pg')} sacks/game, {dst.get('int_pg')} INTs/game")

        # Head-to-head
        h2h = research.get("head_to_head", {})
        if h2h and h2h.get("total_games", 0) > 0:
            lines.append(f"\n--- HEAD-TO-HEAD (last {h2h.get('total_games')}) ---")
            lines.append(f"  {h2h.get('team1')}: {h2h.get('team1_wins')}W - {h2h.get('team2')}: {h2h.get('team2_wins')}W")
            for hg in h2h.get("games", [])[:5]:
                lines.append(f"    {hg.get('season')} Wk {hg.get('week')}: {hg.get('winner')} won {hg.get('score')} at {hg.get('venue')}")

        # Injuries
        injuries = research.get("injuries", {})
        if injuries:
            lines.append("\n--- INJURIES ---")
            for side in ("home", "away"):
                team_name_for_side = home_name if side == "home" else away_name
                team_inj = injuries.get(side, [])
                if team_inj:
                    lines.append(f"\n  [{team_name_for_side}]")
                    for ij in team_inj:
                        lines.append(f"    {ij.get('player')} ({ij.get('position')}): {ij.get('injury')} — Status: {ij.get('game_status')} (Practice: {ij.get('practice_status')})")

        # Situational
        situ = research.get("situational", {})
        if situ:
            lines.append("\n--- SITUATIONAL ---")
            lines.append(f"  Division game: {situ.get('is_division_game')}  Conference game: {situ.get('is_conference_game')}")
            lines.append(f"  Roof: {situ.get('roof_type')}")
            for side in ("home", "away"):
                team_name_for_side = home_name if side == "home" else away_name
                s = situ.get(side, {})
                lines.append(f"  {team_name_for_side}: {s.get('rest_days')} days rest ({'short week!' if s.get('short_week') else 'normal'})")

        # Enrichment
        enrichment = research.get("article_enrichment")
        if enrichment:
            enriched_summary = enrichment.get("enriched_summary", "") if isinstance(enrichment, dict) else ""
            if enriched_summary.strip():
                lines.append(f"\n--- RECENT ARTICLES CONTEXT ---")
                lines.append(f"  {enriched_summary}")

        return "\n".join(lines)

    @staticmethod
    def _tense_note(is_historical: bool) -> str:
        if is_historical:
            return (
                "IMPORTANT — this is a historical preview. The game has already been played. "
                "Write in FUTURE TENSE as if the game hasn't happened yet. "
                "Do NOT reference the actual outcome of this game. "
                "Only use data that was available before the game date."
            )
        return ""
