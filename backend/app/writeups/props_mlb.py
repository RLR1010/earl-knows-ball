"""MLB prop-bets article: data helpers specific to baseball.

Relies on the shared machinery in :mod:`props_article` for prompt building,
per-sport config and the final DB update, then adds the baseball-specific
season/recent stat lookups for prop players.
"""

from __future__ import annotations

import logging
import unicodedata

from sqlalchemy import text

logger = logging.getLogger("earl.props_article.mlb")

# How many recent completed games to pull per prop player as "recent stats".
RECENT_GAMES = 10


def _norm(name: str) -> str:
    """Lowercase + strip accents for accent-insensitive matching."""
    name = (name or "").strip().lower()
    name = unicodedata.normalize("NFD", name)
    return "".join(c for c in name if unicodedata.category(c) != "Mn")


def extract_prop_players(props: list[dict]) -> dict[str, int | None]:
    """Return {player_name: team_id} for the unique players in the prop list."""
    players: dict[str, int | None] = {}
    for p in props:
        name = p.get("player_name")
        if not name:
            continue
        key = name.strip()
        if key and key not in players:
            players[key] = p.get("team_id")
    return players


def build_season_lookup(research: dict) -> dict[str, dict]:
    """Build {normalized player name: season stat dict} from the research brief.

    The brief's ``home_roster`` and ``away_roster`` come from
    ``get_team_hitting_stats`` and already contain each hitter's season
    stats, so we reuse them instead of a fresh DB query. Keys are accent-
    stripped + lowercased via ``_norm``.
    """
    lookup: dict[str, dict] = {}
    for roster_key in ("home_roster", "away_roster"):
        roster = research.get(roster_key) or []
        for hitter in roster:
            if not isinstance(hitter, dict):
                continue
            name = hitter.get("name")
            if not name:
                continue
            lookup.setdefault(_norm(name), hitter)
    return lookup


async def fetch_player_recent_stats(db, player_id: int) -> list[dict] | None:
    """Last `RECENT_GAMES` completed batting lines for a player."""
    if not player_id:
        return None
    rows = await db.execute(
        text(
            f"""
            SELECT g.date, g.home_team_id, g.away_team_id,
                   bgs.avg, bgs.obp, bgs.slg, bgs.ops,
                   bgs.at_bats, bgs.hits, bgs.home_runs, bgs.runs_batted_in,
                   bgs.runs, bgs.base_on_balls, bgs.strikeouts,
                   bgs.stolen_bases, bgs.total_bases
            FROM mlb.batting_game_stats bgs
            JOIN mlb.games g ON g.id = bgs.game_id
            WHERE bgs.player_id = :pid
              AND g.status::text = 'FINAL'
            ORDER BY g.date DESC
            LIMIT {RECENT_GAMES}
            """
        ),
        {"pid": player_id},
    )
    return [dict(r) for r in rows.mappings().all()]


async def fetch_player_split_stats(db, player_id: int) -> dict:
    """Return a batter's split stats (L/R, home/away, day/night, city) from
    ``mlb.player_splits`` for prop-bet context.

    Returns a dict keyed by split_type with the current-season and career
    AVG/OBP/SLG/OPS + PA/HR, suitable for citing in a prop article.
    """
    rows = (
        await db.execute(
            text(
                """
                SELECT split_type, season_id, plate_appearances, avg, obp, slg, ops,
                       home_runs, runs_batted_in
                FROM mlb.player_splits
                WHERE player_id = :pid
                ORDER BY split_type, season_id NULLS FIRST
                """
            ),
            {"pid": player_id},
        )
    ).mappings().all()
    if not rows:
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        scope = "career" if r["season_id"] is None else "season"
        st = r["split_type"]
        key = f"{st}.{scope}"
        out[key] = {
            "pa": r["plate_appearances"],
            "avg": r["avg"],
            "obp": r["obp"],
            "slg": r["slg"],
            "ops": r["ops"],
            "hr": r["home_runs"],
            "rbi": r["runs_batted_in"],
        }
    return out


async def resolve_player_id(db, player_name: str, team_id: int | None) -> int | None:
    """Resolve a player's id from ``mlb.players`` by (accent-insensitive) name + team."""
    norm = _norm(player_name)
    if not norm:
        return None
    if team_id is not None:
        row = (
            await db.execute(
                text(
                    """
                    SELECT id FROM mlb.players
                    WHERE lower(regexp_replace(name, '[\u0300-\u036f]', '', 'g')) = :norm
                    ORDER BY CASE WHEN team_id = :team_id THEN 0 ELSE 1 END
                    LIMIT 1
                    """
                ),
                {"norm": norm, "team_id": int(team_id)},
            )
        ).mappings().first()
    else:
        row = (
            await db.execute(
                text(
                    """
                    SELECT id FROM mlb.players
                    WHERE lower(regexp_replace(name, '[\u0300-\u036f]', '', 'g')) = :norm
                    LIMIT 1
                    """
                )
            )
        )
        row = row.mappings().first()
    return row["id"] if row else None
