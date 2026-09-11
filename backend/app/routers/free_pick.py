"""Free Pick of the Day/Week — feature one premium game writeup (unlocked to all).

Endpoints (mounted under /writeups in main.py):
  GET    /writeups/free-pick                    -> active free pick (public, powers homepage section)
  GET    /writeups/admin/free-pick/candidates   -> published premium writeups to choose from (admin)
  POST   /writeups/admin/free-pick/{sport}/{game_id}   -> set THE free pick (keeps recent picks free), (re)generate card
  DELETE /writeups/admin/free-pick/{sport}/{game_id}   -> unset the free pick

CURRENT vs HISTORICAL (2026-09-10):
  `is_free_feature` marks a writeup as PERMANENTLY unlocked (public + indexable).
  The *current* free pick is whichever flagged writeup has the newest
  `free_featured_at` (see _ACTIVE_SQL). Publishing a new pick NO LONGER clears the
  flag on older picks: past picks stay unlocked so Google can keep crawling and
  indexing them (they remain in the sitemap + returned by the public writeup
  endpoint). We only PRUNE beyond `FREE_PICK_RETENTION` so the free set doesn't
  grow without bound — picks older than that are re-gated.
Only writeups with premium_content (a real paid analysis) are eligible.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.security import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/writeups")

# Allowlisted sport->schema (values are schema names used only in string interpolation below).
SPORTS = ("mlb", "nfl", "nba")

# How many of the most-recently-featured picks stay unlocked (public + indexable).
# New picks no longer re-gate older ones; we only prune beyond this window so the
# free set stays bounded. See module docstring.
FREE_PICK_RETENTION = 30

# Prefer premium writeups (have real paid pick content) over bare public ones.
_CANDIDATE_SQL = """
SELECT w.game_id AS id, w.game_id, w.title, w.slug, w.status, w.published_at,
       w.is_historical, w.is_free_feature, w.free_featured_at,
       w.preview_image, w.premium_social_card, w.seo_description, w.x_posted_at,
       g.date AS game_date,
       CASE WHEN w.premium_content IS NOT NULL AND length(w.premium_content) > 120 THEN TRUE ELSE FALSE END AS has_premium
FROM {schema}.game_writeups w
LEFT JOIN {schema}.games g ON g.id = w.game_id
WHERE w.is_historical = FALSE
  AND w.published_at IS NOT NULL
ORDER BY has_premium DESC, w.published_at DESC
LIMIT :limit
"""

_ACTIVE_SQL = """
SELECT w.game_id AS id, w.game_id, w.title, w.slug, w.published_at, w.is_historical,
       w.is_free_feature, w.free_featured_at, w.preview_image, w.premium_social_card,
       w.seo_description, w.social_caption,
       g.date AS game_date,
       (w.premium_content IS NOT NULL AND length(w.premium_content) > 120) AS has_premium
FROM {schema}.game_writeups w
LEFT JOIN {schema}.games g ON g.id = w.game_id
WHERE w.is_free_feature = TRUE AND w.free_featured_at IS NOT NULL
ORDER BY w.free_featured_at DESC
LIMIT 1
"""


def _row_to_pick(r: dict, sport: str) -> dict:
    """Normalize a writeup row into the FreePick payload shape both admin + homepage use."""
    return {
        "sport": sport,
        "game_id": int(r["game_id"]),
        "writeup_id": int(r.get("id") or r["game_id"]),
        "title": r.get("title"),
        "slug": r.get("slug"),
        "status": r.get("status"),
        "published_at": r.get("published_at").isoformat() if r.get("published_at") else None,
        "is_historical": bool(r.get("is_historical")),
        "is_free_feature": bool(r.get("is_free_feature")),
        "free_featured_at": r.get("free_featured_at").isoformat() if r.get("free_featured_at") else None,
        "preview_image": r.get("preview_image"),
        "premium_social_card": r.get("premium_social_card"),
        "social_caption": r.get("social_caption"),
        "seo_description": r.get("seo_description"),
        "has_premium": bool(r.get("has_premium")),
        "game_date": r.get("game_date").isoformat() if r.get("game_date") else None,
    }


def _check_sport(sport: str) -> str:
    s = (sport or "").lower()
    if s not in SPORTS:
        raise HTTPException(status_code=400, detail=f"sport must be one of {list(SPORTS)}")
    return s


_RESOLVERS = {
    "mlb": ("app.social.cards", "game_team_cards"),
    "nfl": ("app.social.cards_nfl", "nfl_team_cards"),
    "nba": ("app.social.cards_nba", "nba_team_cards"),
}


def _resolve_matchup(sport: str, game_id: int):
    """Sync: resolve away/home team {abbr,name,record,logo_url} for the free-pick card's
    matchup display, reusing the lightweight team resolver the social card uses (own engine)."""
    from importlib import import_module
    from sqlalchemy import create_engine
    from app.core.config import settings

    mod_path, fn_name = _RESOLVERS[sport]
    fn = getattr(import_module(mod_path), fn_name)
    engine = create_engine(settings.database_url_sync)
    try:
        resolved = fn(int(game_id), engine)
    finally:
        engine.dispose()

    def side(t):
        t = t or {}
        meta = (t.get("meta") or "").split("·")[0].strip()
        return {
            "abbr": (t.get("abbr") or ""),
            "name": (t.get("name") or ""),
            "record": meta or "",
            "logo_url": t.get("logo_url") or "",
        }

    return {"away": side(resolved.get("away")), "home": side(resolved.get("home"))}


def _premium_card_work(sport: str, game_id: int, slug: str):
    """Synchronous bespoke Free-Pick premium card render (own engine, off the event loop)."""
    from sqlalchemy import create_engine
    from app.core.config import settings
    from app.social.free_pick_card import render_free_pick_card

    engine = create_engine(settings.database_url_sync)
    try:
        return render_free_pick_card(sport, int(game_id), slug=slug or "", conn_or_engine=engine)
    finally:
        engine.dispose()


async def _build_premium_card(db: AsyncSession, sport: str, game_id: int, slug: str = "") -> str:
    """Render the bespoke Free Pick promo card, persist premium_social_card, return rel path."""
    import asyncio
    result = await asyncio.to_thread(_premium_card_work, sport, int(game_id), slug or "")
    rel = result.get("premium_social_card")
    if rel:
        await db.execute(
            text(f"UPDATE {sport}.game_writeups SET premium_social_card = :rel WHERE game_id = :gid"),
            {"rel": rel, "gid": int(game_id)},
        )
        await db.commit()
    logger.info("free-pick premium card for %s %s -> %s", sport, game_id, rel)
    return rel


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
@router.get("/free-pick", response_model=Optional[dict])
async def get_free_pick(
    db: AsyncSession = Depends(get_db),
):
    """Return the single active free pick (full premium content unlocked), or 204/None if none set."""
    for sport in SPORTS:
        res = await db.execute(text(_ACTIVE_SQL.format(schema=sport)))
        r = res.mappings().first()
        if r:
            pick = _row_to_pick(dict(r), sport)
            # Pull + attach the full unlocked premium writeup content so the homepage
            # can render the giveaway inline without a second premium-gated call.
            detail = await _fetch_writeup_content(db, sport, pick["game_id"])
            pick["content"] = detail
            # Attach the matchup teams (away/home logo + record) for the homepage card art.
            try:
                pick["teams"] = await asyncio.to_thread(
                    _resolve_matchup, sport, int(pick["game_id"])
                )
            except Exception as e:
                logger.warning("free-pick matchup resolve failed (%s %s): %s", sport, pick["game_id"], e)
                pick["teams"] = None
            return pick
    raise HTTPException(status_code=404, detail="No free pick is currently featured")


async def _fetch_writeup_content(db: AsyncSession, sport: str, game_id: int) -> dict:
    rows = await db.execute(
        text(
            f"""SELECT title, slug, seo_description, public_content, premium_content,
                       prop_title, prop_content, social_caption, preview_image, premium_social_card,
                       published_at
                FROM {sport}.game_writeups WHERE game_id = :gid LIMIT 1"""
        ),
        {"gid": int(game_id)},
    )
    r = rows.mappings().first()
    if not r:
        return {}
    # free pick is open to everyone: serve the full premium content
    return {
        "title": r["title"],
        "slug": r["slug"],
        "seo_description": r["seo_description"],
        "content": r["premium_content"] or r["public_content"],
        "prop_title": r["prop_title"],
        "prop_content": r["prop_content"] or "",
        "preview_image": r["preview_image"],
        "premium_social_card": r["premium_social_card"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
    }


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------
@router.get("/admin/free-pick/candidates")
async def list_free_pick_candidates(
    sport: Optional[str] = Query(None),
    limit: int = Query(50, le=300),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
):
    """List published (premium) game writeups the admin can make the free pick. sport optional."""
    sports = [sport] if sport else list(SPORTS)
    out = []
    for s in sports:
        s = _check_sport(s)
        res = await db.execute(text(_CANDIDATE_SQL.format(schema=s)), {"limit": limit})
        for r in res.mappings():
            out.append(_row_to_pick(dict(r), s))
    return out


@router.post("/admin/free-pick/{sport}/{game_id}")
async def set_free_pick(
    sport: str,
    game_id: int,
    force_regenerate_card: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Make {sport}/game {game_id} THE free pick (single-active across all sports / all writeups).

    Always builds/refreshes the bespoke Free Pick premium social card into premium_social_card.
    Hitting Feature re-renders it (independent of the public preview_image / gw- card).
    """
    sport = _check_sport(sport)
    writeup = await db.execute(
        text(
            f"SELECT game_id, title, slug, published_at, premium_social_card FROM {sport}.game_writeups "
            "WHERE game_id = :gid LIMIT 1"
        ),
        {"gid": int(game_id)},
    )
    w = writeup.mappings().first()
    if not w:
        raise HTTPException(status_code=404, detail=f"No {sport} writeup for game {game_id}")
    if not (w["published_at"]):
        raise HTTPException(status_code=400, detail="Cannot feature an unpublished writeup")

    now = datetime.now(timezone.utc)

    # Set this one FIRST (so it is always kept), then prune older picks beyond the
    # retention window. Publishing a new pick does NOT re-gate recent/ past picks:
    # they stay unlocked so Google keeps crawling + indexing them (they remain in
    # the sitemap and are still readable anonymously). The CURRENT pick is derived
    # from free_featured_at recency (_ACTIVE_SQL), not from being the only flag.
    await db.execute(
        text(
            f"UPDATE {sport}.game_writeups "
            "SET is_free_feature = TRUE, free_featured_at = :now WHERE game_id = :gid"
        ),
        {"now": now, "gid": int(game_id)},
    )

    # Prune: across all sports, keep only the FREE_PICK_RETENTION newest featured
    # picks unlocked; re-gate anything older (drop its flag + featured timestamp).
    for s in SPORTS:
        await db.execute(
            text(
                f"""
                UPDATE {s}.game_writeups
                SET is_free_feature = FALSE, free_featured_at = NULL
                WHERE is_free_feature = TRUE
                  AND game_id NOT IN (
                    SELECT game_id FROM {s}.game_writeups
                    WHERE is_free_feature = TRUE AND free_featured_at IS NOT NULL
                    ORDER BY free_featured_at DESC
                    LIMIT :keep
                  )
                """
            ),
            {"keep": FREE_PICK_RETENTION},
        )
    await db.commit()

    # Build the bespoke Free Pick premium social card (distinct from the public gw- card) store
    # into premium_social_card so the homepage + shares show the giveaway art.
    if force_regenerate_card or not w["premium_social_card"]:
        try:
            rel = await _build_premium_card(db, sport, int(game_id), w["slug"] or "")
            logger.info("free-pick premium card for %s %s -> %s", sport, game_id, rel)
        except HTTPException as e:
            logger.warning("premium-card gen warning: %s", e.detail)
        except Exception as e:
            logger.warning("premium-card gen error: %s", e)

    # Re-read + attach content for the response.
    return await _get_active(db)


@router.delete("/admin/free-pick/{sport}/{game_id}")
async def unset_free_pick(
    sport: str,
    game_id: int,
    db: AsyncSession = Depends(get_db),
    _admin=Depends(require_admin),
):
    """Retract the free pick for {sport}/{game_id}.

    Note: rotating to a new pick no longer re-gates older picks (they stay free
    for indexing). This manual unset un-flags the named pick only — use it to
    deliberately retire a specific pick.
    """
    sport = _check_sport(sport)
    await db.execute(
        text(
            f"UPDATE {sport}.game_writeups "
            "SET is_free_feature = FALSE, free_featured_at = NULL "
            "WHERE game_id = :gid AND is_free_feature = TRUE"
        ),
        {"gid": int(game_id)},
    )
    await db.commit()
    try:
        active = await _active_count(db)
        return {"ok": True, "active_free_picks": active}
    except Exception:
        return {"ok": True}


async def _get_active(db: AsyncSession) -> dict:
    # Return the GLOBALLY newest featured pick across all sports. Past free picks
    # stay flagged (is_free_feature=TRUE) for indexing, so we can no longer just
    # grab the first sport that has any flag — order by free_featured_at across sports.
    best: dict | None = None
    for sport in SPORTS:
        res = await db.execute(text(_ACTIVE_SQL.format(schema=sport)))
        r = res.mappings().first()
        if not r:
            continue
        pick = _row_to_pick(dict(r), sport)
        if best is None or (pick.get("free_featured_at") or "") > (best.get("free_featured_at") or ""):
            best = pick
    if best is not None:
        best["content"] = await _fetch_writeup_content(db, best["sport"], best["game_id"])
        return best
    return {"ok": True, "active_free_picks": 0, "content": None}


async def _active_count(db: AsyncSession) -> int:
    total = 0
    for sport in SPORTS:
        c = await db.execute(text(f"SELECT count(*) FROM {sport}.game_writeups WHERE is_free_feature = TRUE"))
        total += int(c.scalar())
    return total
