"""NBA prop-bet research helpers for the premium "Prop Bets" writeup article.

Mirrors ``props_mlb.py`` for the MLB prop article, but reads NBA season
stats from ``nba.player_season_stats`` (the NBA research brief carries no
per-player rosters like MLB does), recent games from
``nba.player_game_stats``, and splits from ``nba.player_splits``.

Name resolution is accent-insensitive (NFD) so the reader-facing names the
Odds API returns ("Jose Calderon") match the DB ("José Calderón").
"""
from __future__ import annotations

import logging
import unicodedata
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.nba_props")

# Column name for the current-season split rows (nba.player_season_stats)
SEASON_COL = "season_id"
MAX_MISSING_OK = "yes"  # season stats may legitimately be sparse


def _norm(name: str) -> str:
    """NFD-decompose, strip accents + lowercase for accent-insensitive match."""
    return unicodedata.normalize("NFD", name or "").encode("ascii", "ignore").decode().lower()


def build_season_lookup(research: dict) -> dict[str, dict]:
    """Build {normalized player name: season stat dict} from the research brief.

    NBA research brief nests rosters under ``team_home.roster`` /
    ``team_away.roster`` (built by ``_get_team_roster``, ordered by minutes).
    Returns a dict normalized to the same keys ``fetch_player_season_stats``
    produces, so downstream prompt formatting is identical.
    """
    # map roster field name -> fetch_player_season_stats key
    key_map = {
        "games_played": "games_played",
        "minutes": "minutes_played",
        "ppg": "points_per_game",
        "rpg": "rebounds_per_game",
        "apg": "assists_per_game",
        "spg": "steals",
        "bpg": "blocks",
        "tpg": "turnovers",
        "fg_pct": "field_goal_pct",
        "three_pct": "three_point_pct",
        "ft_pct": "free_throw_pct",
        "ts_pct": "true_shooting_pct",
        "plus_minus": "plus_minus",
    }
    lookup: dict[str, dict] = {}
    for team_key in ("team_home", "team_away"):
        roster = (research.get(team_key) or {}).get("roster") or []
        for player in roster:
            if not isinstance(player, dict):
                continue
            name = player.get("name")
            if not name:
                continue
            out = {"name": name}
            for src_key, dst_key in key_map.items():
                if src_key in player:
                    out[dst_key] = player[src_key]
            lookup.setdefault(_norm(name), out)
    return lookup


def extract_prop_players(props: list[dict]) -> dict[str, Optional[int]]:
    """Return a dict of unique prop player names -> player id (resolved later)."""
    names = {}
    for p in props or []:
        n = (p.get("player_name") or "").strip()
        if n:
            names.setdefault(n, None)
    return names


async def resolve_player_id(
    db: AsyncSession, player_name: str, team_id: Optional[int] = None
) -> Optional[int]:
    """Resolve a prop player name to nba.players.id (accent-insensitive).

    Mirrors the chat tool's NFD normalization ("Jose Calderon" -> "José
    Calderón"). team_id is accepted for signature parity; matching is by
    normalized name across all players (a player can have moved teams).
    """
    n = _norm(player_name)
    if not n:
        return None
    try:
        rows = (await db.execute(text("SELECT id, name FROM nba.players"))).all()
    except Exception as e:  # noqa: BLE001
        logger.warning("player list lookup failed: %s", e)
        return None
    best = None
    for pid, name in rows:
        nm = _norm(str(name))
        if nm == n:
            return pid
        if best is None and n in nm:
            best = pid
    return best



async def fetch_player_season_stats(
    db: AsyncSession, player_name: str, team_id: Optional[int] = None
) -> Optional[dict]:
    """Current-season (latest) per-game stats for a prop player.

    Returns a compact dict of per-game rates used in the prop prompt.
    """
    pid = await resolve_player_id(db, player_name, team_id)
    if not pid:
        return None
    try:
        row = (
            await db.execute(
                text(
                    """
                    SELECT season_id, games_played, minutes_played,
                           points_per_game, rebounds_per_game, assists_per_game,
                           steals, blocks, turnovers,
                           field_goal_pct, three_point_pct, free_throw_pct,
                           true_shooting_pct, plus_minus, efficiency,
                           usage_pct, assists_turnover_ratio
                    FROM nba.player_season_stats
                    WHERE player_id = :pid
                    ORDER BY season_id DESC NULLS LAST
                    LIMIT 1
                    """
                ),
                {"pid": pid},
            )
        ).mappings().first()
    except Exception as e:  # noqa: BLE001
        logger.warning("season stats lookup failed for %s: %s", player_name, e)
        return None
    if not row:
        return None
    d = dict(row)
    d["player_name"] = player_name
    return d


async def fetch_player_recent_stats(
    db: AsyncSession, player_name: str, team_id: Optional[int] = None
) -> Optional[dict]:
    """Recent-form (last ~5 games) per-game averages for a prop player."""
    pid = await resolve_player_id(db, player_name, team_id)
    if not pid:
        return None
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT points, rebounds_total, assists, steals, blocks,
                           turnovers, fouls_personal, three_pointers_made,
                           plus_minus, minutes, is_starter
                    FROM nba.player_game_stats
                    WHERE player_id = :pid
                    ORDER BY game_id DESC
                    LIMIT 5
                    """
                ),
                {"pid": pid},
            )
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("recent stats lookup failed for %s: %s", player_name, e)
        return None
    if not rows:
        return None
    return {
        "player_name": player_name,
        "recent_games": [dict(r) for r in rows],
    }


async def fetch_player_split_stats(
    db: AsyncSession, player_name: str, team_id: Optional[int] = None
) -> dict:
    """NBA split lines for a prop player, keyed '<split>.<scope>'.

    Scopes: career / season (current). Split types: home, away, vs_east,
    vs_west, starter, bench, rest0, rest_ge1. Returns per-game rates.
    """
    pid = await resolve_player_id(db, player_name, team_id)
    if not pid:
        return {}
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT split_type, season_id,
                           games, points_per_game, rebounds_per_game,
                           assists_per_game, steals_per_game, blocks_per_game,
                           turnovers_per_game, three_point_pct, true_shooting_pct
                    FROM nba.player_splits
                    WHERE player_id = :pid
                      AND split_type NOT LIKE 'month_%'
                    ORDER BY split_type, season_id NULLS FIRST
                    """
                ),
                {"pid": pid},
            )
        ).mappings().all()
    except Exception as e:  # noqa: BLE001
        logger.warning("split stats lookup failed for %s: %s", player_name, e)
        return {}
    out = {}
    for r in rows:
        scope = "career" if r["season_id"] is None else "season"
        key = f"{r['split_type']}.{scope}"
        out[key] = {
            "games": r["games"],
            "pts": r["points_per_game"],
            "reb": r["rebounds_per_game"],
            "ast": r["assists_per_game"],
            "stl": r["steals_per_game"],
            "blk": r["blocks_per_game"],
            "tov": r["turnovers_per_game"],
            "3p%": r["three_point_pct"],
            "ts%": r["true_shooting_pct"],
        }
    return out
