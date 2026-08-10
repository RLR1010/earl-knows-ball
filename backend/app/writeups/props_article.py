"""Separate premium PROP BETS article generation.

Runs AFTER the main game writeup has been generated and stored. It:

  1. Fetches player prop odds for the game from the sport's
     ``player_daily_props`` table.
  2. If the game has NO prop odds, the whole step is skipped
     (no LLM call, no cost). Historical games have no props, so they
     self-skip via this check.
  3. For each prop player, gathers season + recent (last N games) context.
  4. Sends a second LLM call asking it to pick a handful of props it likes
     and justify each in a short, premium-tone writeup (max 500 words total).
  5. UPDATEs the same ``game_writeups`` row's ``prop_title``,
     ``prop_content``, ``prop_generated_by``, ``prop_total_tokens`` and
     ``prop_published_at`` columns.

``prop_content`` is premium-only and is never shown in the public article.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text

logger = logging.getLogger("earl.props_article")

# A prop bet "likes" up to this many props / this much text in the prop article.
PROP_ARTICLE_MAX_WORDS = 500
PROP_ARTICLE_MAX_PICKS = 5
# How many recent games used for the "recent stats" context per player.
PROP_RECENT_GAMES = 10
# Cap on how many prop lines to send to the LLM (games can have 600+ props;
# the LLM only needs to see a solid representative slice to pick a few).
MAX_PROPS_TO_SEND = 250


# ── Per-sport configuration ─────────────────────────────────────────


def _mlb_config() -> dict:
    return {
        "schema": "mlb",
        "props_table": "mlb.player_daily_props",
        "recent_table": "mlb.batting_game_stats",
        "player_table": "mlb.players",
        "team_table": "mlb.teams",
        "bookmaker": "DraftKings",
    }


def _nfl_config() -> dict:
    return {
        "schema": "nfl",
        "props_table": "nfl.player_daily_props",
        "recent_table": "nfl.player_game_stats",
        "player_table": "nfl.players",
        "team_table": "nfl.teams",
        "bookmaker": "DraftKings",
    }


def _nba_config() -> dict:
    return {
        "schema": "nba",
        "props_table": "nba.player_daily_props",
        "recent_table": "nba.player_game_stats",
        "player_table": "nba.players",
        "team_table": "nba.teams",
        "bookmaker": "DraftKings",
    }


SPORT_CONFIGS = {
    "mlb": _mlb_config,
    "nfl": _nfl_config,
    "nba": _nba_config,
}


# ── Data fetch helpers ──────────────────────────────────────────────


async def fetch_game_props(db, config: dict, game_id: int) -> list[dict]:
    """Return player props for *game_id* (single bookmaker, deduped).

    Returns an empty list when the game has no prop odds.
    """
    result = await db.execute(
        text(
            f"""
            SELECT player_name, team_id, prop_type, line, odds, direction
            FROM {config['props_table']}
            WHERE game_id = :game_id
              AND bookmaker = :bookmaker
            ORDER BY player_name, prop_type
            """
        ),
        {"game_id": str(game_id), "bookmaker": config["bookmaker"]},
    )
    rows = [dict(r) for r in result.mappings().all()]

    # De-duplicate exact repeats (same player + prop_type + line + direction
    # + odds). Both Over and Under sides are kept — the LLM needs to see the
    # full market to justify which side it likes.
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for row in rows:
        key = (
            row["player_name"].strip().lower(),
            row["prop_type"],
            row.get("line"),
            row.get("direction"),
            row.get("odds"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


async def fetch_player_season_stats(db, config: dict, player_name: str, team_id: int | None):
    """Fetch a player's season stats from the sport's batting/player stats table.

    MLB batters live in ``mlb.batting_stats`` keyed by player_id + season_id.
    NFL/NBA stats are handled by the per-sport generator (passed via
    ``season_stats_fn``). Returns a dict or None.
    """
    return None


async def build_player_context(
    db,
    config: dict,
    prop_players: dict[str, int | None],
    season_stats_fn=None,
    recent_stats_fn=None,
) -> list[dict]:
    """Return per-player context dicts for the prop writeup.

    Each dict has: name, season (dict), recent (dict).
    ``season_stats_fn`` / ``recent_stats_fn`` are optional per-sport callables:
        fn(db, player_name, team_id) -> dict | None
    """
    context = []
    for name, team_id in prop_players.items():
        entry = {"name": name, "team": team_id, "season": None, "recent": None}
        if season_stats_fn:
            try:
                entry["season"] = await season_stats_fn(db, name, team_id)
            except Exception as e:  # noqa: BLE001 - never let a stats miss kill the props step
                logger.warning("props: season stats lookup failed for %s: %s", name, e)
        if recent_stats_fn:
            try:
                entry["recent"] = await recent_stats_fn(db, name, team_id)
            except Exception as e:  # noqa: BLE001
                logger.warning("props: recent stats lookup failed for %s: %s", name, e)
        context.append(entry)
    return context


# ── Prompt builders ─────────────────────────────────────────────────


def build_title(home_abbr: str, away_abbr: str, game_date: str) -> str:
    """e.g. "Prop Bets for BOS vs NYY — Aug 9, 2026"."""
    return f"Prop Bets for {away_abbr} vs {home_abbr} — {game_date}"


def _format_odds(odds) -> str:
    if odds is None:
        return ""
    odds = int(odds)
    if odds > 0:
        return f"+{odds}"
    return str(odds)


def render_props_table(props: list[dict]) -> str:
    """Human-readable list of the prop odds for the LLM."""
    lines = []
    for p in props:
        odds_str = _format_odds(p["odds"])
        direction = (p.get("direction") or "").lower()
        verb = direction if direction in ("over", "under") else ""
        line_str = f" {verb} {p['line']}" if p.get("line") is not None else ""
        odds_part = f" ({odds_str})" if odds_str else ""
        lines.append(
            f"- {p['player_name']} — {p['prop_type']}{line_str}{odds_part}"
        )
    return "\n".join(lines) if lines else "(no props available)"


def build_system_prompt() -> str:
    return (
        "You are a sharp, professional sports handicapper writing a SHORT premium "
        "prop-bets article for a betting analytics product (Earl Knows Ball). "
        "You are given the prop odds for one MLB game and relevant player-season and "
        f"recent-game context. Pick a handful (up to {PROP_ARTICLE_MAX_PICKS}) of the "
        "props that you genuinely like, and justify each pick with concrete numbers "
        "from the provided player context — don't just restate the line. Call out any "
        "prop that looks like bad value. Write with the confident, stats-first voice "
        "of a real handicapper.\n\n"
        "Format: plain prose with a short intro paragraph, then one short paragraph "
        f"per pick. Hard limit: {PROP_ARTICLE_MAX_WORDS} words TOTAL for the whole "
        "article (not per pick). Lead each pick with the player, the prop, and the "
        "line (e.g. \"Rafael Devers over 1.5 total bases (+110)\"). This is PREMIUM "
        "subscriber content — be specific and useful.\n\n"
        "DO NOT invent numbers that are not in the provided data. If the evidence is "
        "thin, say so rather than fabricating."
    )


def build_user_prompt(cfg: dict, props: list[dict], player_context: list[dict], research_brief: dict) -> str:
    ctx_lines = []
    for ent in player_context:
        ctx_lines.append(f"Player: {ent['name']}")
        if ent["season"]:
            ctx_lines.append(f"  Season: {ent['season']}")
        if ent["recent"]:
            ctx_lines.append(f"  Recent (last {PROP_RECENT_GAMES} games): {ent['recent']}")
        if not ent["season"] and not ent["recent"]:
            ctx_lines.append("  (no detailed stats available)")

    return (
        f"PROP ODDS for this game:\n{render_props_table(props)}\n\n"
        f"PLAYER CONTEXT (season + recent stats):\n"
        + ("\n".join(ctx_lines) if ctx_lines else "(none)")
        + f"\n\nFULL CACHED RESEARCH BRIEF (game context, team stats, matchup):\n"
        + (research_brief if isinstance(research_brief, str) else repr(research_brief))
        + "\n\nWrite the premium prop-bets article now."
    )


# ── Update helper ───────────────────────────────────────────────────


async def save_props_article(
    db,
    config: dict,
    game_id: int,
    title: str,
    content: str,
    generated_by: str,
    total_tokens: int,
    prop_research: dict | None = None,
) -> None:
    """Write the prop article onto the game's existing game_writeups row.

    ``prop_research`` is the extra research context that was sent to the LLM
    for the props article (prop odds + per-player season/recent stats). When
    provided, it is merged into the row's ``research_brief`` JSON under a
    ``prop_research`` key, so the stored Research Context reflects everything
    that was actually shown to the model.
    """
    existing = await _load_research_brief(db, config["schema"], game_id)
    if prop_research:
        existing["prop_research"] = prop_research
    merged_json = json.dumps(existing, default=str) if existing else None

    await db.execute(
        text(
            f"""
            UPDATE {config['schema']}.game_writeups
            SET prop_title = :title,
                prop_content = :content,
                prop_generated_by = :generated_by,
                prop_total_tokens = :total_tokens,
                prop_published_at = NOW(),
                research_brief = :research_brief
            WHERE game_id = :game_id
            """
        ),
        {
            "game_id": int(game_id),
            "title": title,
            "content": content,
            "generated_by": generated_by,
            "total_tokens": total_tokens,
            "research_brief": merged_json,
        },
    )
    await db.commit()


async def _load_research_brief(db, schema: str, game_id: int) -> dict:
    """Awaited fetch of the current research_brief JSON for a game."""
    res = await db.execute(
        text(f"SELECT research_brief FROM {schema}.game_writeups WHERE game_id = :gid"),
        {"gid": int(game_id)},
    )
    row = res.mappings().first()
    if not row or not row["research_brief"]:
        return {}
    raw = row["research_brief"]
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    return parsed if isinstance(parsed, dict) else {}
