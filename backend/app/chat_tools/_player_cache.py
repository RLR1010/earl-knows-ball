"""Shared, TTL-bounded snapshot cache for the sport player reference tables.

The MLB/NBA chat tool resolvers used to do a full-table DB scan + in-Python fuzzy
scoring on every call. The DB scan was only part of the cost; the bigger cost was
running NFD normalization + `difflib.SequenceMatcher` over EVERY player on every
query (MLB = ~6210 players, ~700ms-1s of pure Python). 

This module caches the player *table snapshot* (NOT the resolved answer) AND
precomputes all the expensive per-player normalization artifacts once per fill, so
a resolve is a sub-ms in-memory lookup + score over a small candidate set.

Design intent:
  * Cache holds RAW player rows plus precomputed normalized name / token set /
    suffix-stripped core name — so resolvers never re-run NFD/suffix/tokenize.
  * A token -> [player indices] index lets the resolver only score players that
    share at least one token with the query (the cheap gate), skipping the
    SequenceMatcher for the ~99% of players with zero overlap.
  * TTL-bounded (default 10 min) so new signings / call-ups / trades / season
    flips are picked up automatically within a few minutes.
  * Explicit `invalidate_*()` for ingest/scheduler hooks to force immediate refresh
    after roster/season ingestion (avoids even the TTL lag).
"""

from __future__ import annotations

import time
import unicodedata
from typing import Any, Dict, List, Optional

from sqlalchemy import text

# Each cache: {"ts": monotonic_epoch, "rows": [...], "by_token": {...}}.
_NBA_CACHE: Optional[dict] = None
_MLB_CACHE: Optional[dict] = None

DEFAULT_TTL_SECONDS = 600  # 10 minutes

_nba_lock = False
_mlb_lock = False


def _norm(s: str) -> str:
    """NFD + strip combining marks (accent-insensitive). Shared with resolvers."""
    return "".join(c for c in unicodedata.normalize("NFD", s.lower())
                   if not unicodedata.combining(c))


def _strip_suffix(s: str) -> str:
    """Drop name suffixes (Jr., Sr., II, III, IV) so they don't penalize/duplicate."""
    parts = (s or "").split()
    if parts and parts[-1].strip("., ").upper() in {"JR", "SR", "II", "III", "IV", "V"}:
        return " ".join(parts[:-1])
    return s or ""


# ─────────────────────────────── NBA ───────────────────────────────

async def get_nba_players(db, ttl: float = DEFAULT_TTL_SECONDS) -> List[tuple]:
    """Return all NBA players as (id, name) tuples, cached (TTL-bounded)."""
    global _NBA_CACHE, _nba_lock
    now = time.monotonic()
    if _NBA_CACHE and not (now - _NBA_CACHE["ts"] > ttl):
        return _NBA_CACHE["rows"]

    if _nba_lock and _NBA_CACHE:
        return _NBA_CACHE["rows"]

    _nba_lock = True
    try:
        rows = (await db.execute(text(
            "SELECT id, name FROM nba.players ORDER BY name"
        ))).all()
        data = [(r.id, r.name) for r in rows]
        _NBA_CACHE = {"ts": now, "rows": data}
        return data
    finally:
        _nba_lock = False


def invalidate_nba_players() -> None:
    """Drop the cached NBA player snapshot. Call after NBA roster/season ingest."""
    global _NBA_CACHE
    _NBA_CACHE = None


# ─────────────────────────────── MLB ───────────────────────────────

async def get_mlb_players(db, ttl: float = DEFAULT_TTL_SECONDS) -> List[dict]:
    """Return all MLB players (one-shot with cached normalized artifacts + token
    index). Each dict carries the same 7 fields `_search_players` scores against
    PLUS precomputed `_core` (suffix-stripped NFD name) and `_tokens` (token set), so
    the resolver only re-runs the fuzzy scorer on plausible candidates.
    """
    global _MLB_CACHE, _mlb_lock
    now = time.monotonic()
    if _MLB_CACHE and not (now - _MLB_CACHE["ts"] > ttl):
        return _MLB_CACHE["rows"]

    if _mlb_lock and _MLB_CACHE:
        return _MLB_CACHE["rows"]

    _mlb_lock = True
    try:
        # Most recent season id in the games feed (used to mark which players have
        # current-season data). Falls back to 0 if empty.
        latest = (await db.execute(text(
            "SELECT COALESCE(MAX(season_id), 0) FROM mlb.games"
        ))).scalar()
        rows = (await db.execute(text(_MLB_SNAPSHOT_SQL), {"sid": latest})).fetchall()

        data = []
        for r in rows:
            name = r[1] or ""
            core = _norm(_strip_suffix(name))
            tokens = frozenset(_norm(name).split())
            core_tokens = core.split()
            d = {
                "player_id": r[0],
                "name": name,
                "position": r[2],
                "team_id": r[3],
                "bats": r[4],
                "team_abbr": r[5],
                "has_season_data": bool(r[6]),
                "_core": core,
                "_tokens": tokens,
                # last-name token (suffix-stripped) — cheap gate field for typos.
                "_last": core_tokens[-1] if core_tokens else "",
            }
            data.append(d)

        # token -> [dict indices]: the cheap gate for the resolver.
        by_token: Dict[str, List[int]] = {}
        for i, d in enumerate(data):
            core_tokens = d["_core"].split()
            tokens = set(d["_tokens"]) | set(core_tokens)
            for t in tokens:
                by_token.setdefault(t, []).append(i)

        _MLB_CACHE = {"ts": now, "rows": data, "by_token": by_token}
        return data
    finally:
        _mlb_lock = False


def get_mlb_token_index() -> Dict[str, List[int]]:
    """Return the token->index map for the last-filled MLB cache (or {})."""
    return _MLB_CACHE["by_token"] if _MLB_CACHE else {}


def invalidate_mlb_players() -> None:
    """Drop the cached MLB player snapshot. Call after MLB roster/season ingest."""
    global _MLB_CACHE
    _MLB_CACHE = None


_MLB_SNAPSHOT_SQL = """
    SELECT p.id, p.name, p.position, p.team_id, p.bats,
           t.abbreviation AS team_abbr,
           EXISTS (
               SELECT 1 FROM mlb.batting_game_stats bgs
               JOIN mlb.games g ON g.id = bgs.game_id
               WHERE bgs.player_id = p.id AND g.season_id = :sid
           ) AS has_season_data
    FROM mlb.players p
    LEFT JOIN mlb.teams t ON t.id = p.team_id
"""
