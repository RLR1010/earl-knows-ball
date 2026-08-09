"""Auto Generation config API — manage continuous/automated article templates.

Powers the "Auto Generation" admin page. Each config describes an article the
system should regenerate on a cadence:

  - cadence:      'daily' | 'weekly'
  - scope_type:   'team' (team-specific) | 'sport' (general to the whole sport)
  - team_*:       set when scope_type == 'team'
  - template_article_id: optional link to an existing original_articles row that
    seeded this template (created via "Save as continuous template" from the
    Original Articles edit page).

Endpoints (all under /api/admin, so they live on the compute machine):

  GET    /api/admin/auto-generation                 list all configs (all sports)
  POST   /api/admin/auto-generation                 create a config
  POST   /api/admin/auto-generation/from-article    import an original article as a config
  PATCH  /api/admin/auto-generation/{config_id}     edit a config
  DELETE /api/admin/auto-generation/{config_id}     delete a config
  GET    /api/admin/auto-generation/teams/{sport}   teams for the sport dropdown
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

logger = logging.getLogger("auto_generation")

SPORTS = ("mlb", "nfl", "nba")
CADENCES = ("daily", "weekly")
SCOPES = ("team", "sport")
REASONINGS = ("minimal", "low", "medium", "high", "xhigh")
VISIBILITIES = ("public", "premium")
TITLE_MODES = ("fixed", "llm")
admin_router = APIRouter(prefix="/api/admin/auto-generation", tags=["auto-generation-admin"])


DEFAULT_WORD_RANGE = (400, 700)


def _coerce_word_range(word_min, word_max):
    """Return a sane (min, max) pair assuming word_min < word_max."""
    if word_min is None and word_max is None:
        return DEFAULT_WORD_RANGE[0], DEFAULT_WORD_RANGE[1]
    lo = word_min if word_min is not None else DEFAULT_WORD_RANGE[0]
    hi = word_max if word_max is not None else DEFAULT_WORD_RANGE[1]
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class CreateConfigRequest(BaseModel):
    sport: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    instructions: Optional[str] = None
    cadence: str = "daily"
    scope_type: str = "sport"
    team_id: Optional[int] = None
    team_abbr: Optional[str] = None
    team_name: Optional[str] = None
    template_article_id: Optional[int] = None
    status: str = "active"
    reasoning: str = "medium"
    visibility: str = "public"
    word_min: Optional[int] = None
    word_max: Optional[int] = None
    title_mode: str = "fixed"


class FromArticleRequest(BaseModel):
    """Used when saving an existing original article as a continuous template."""

    sport: str = Field(..., min_length=1)
    article_id: int = Field(..., gt=0)
    title: str = Field(..., min_length=1)
    description: Optional[str] = None
    instructions: Optional[str] = None
    cadence: str = "daily"
    scope_type: str = "sport"
    team_id: Optional[int] = None
    team_abbr: Optional[str] = None
    team_name: Optional[str] = None
    reasoning: str = "medium"
    visibility: str = "public"
    word_min: Optional[int] = None
    word_max: Optional[int] = None
    title_mode: str = "fixed"


class UpdateConfigRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    instructions: Optional[str] = None
    cadence: Optional[str] = None
    scope_type: Optional[str] = None
    team_id: Optional[int] = None
    team_abbr: Optional[str] = None
    team_name: Optional[str] = None
    status: Optional[str] = None
    reasoning: Optional[str] = None
    visibility: Optional[str] = None
    word_min: Optional[int] = None
    word_max: Optional[int] = None
    title_mode: Optional[str] = None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(r) -> dict:
    if r is None:
        return {}
    # r may be a Row (has ._mapping) or a RowMapping (dict-like). Handle both.
    if hasattr(r, "_mapping"):
        m = dict(r._mapping)
    else:
        m = dict(r)
    for k in ("created_at", "updated_at", "last_generated_at"):
        if m.get(k) is not None and hasattr(m[k], "isoformat"):
            m[k] = m[k].isoformat()
    return m


def _validate_sport(sport: str) -> str:
    sport = (sport or "").strip().lower()
    if sport not in SPORTS:
        raise HTTPException(status_code=400, detail=f"Unsupported sport '{sport}'.")
    return sport


def _validate_enum(value: str, allowed: tuple, field: str, default: str = None) -> str:
    v = (value or default or "").strip().lower()
    if v not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid {field} '{value}'. Expected one of: {', '.join(allowed)}.")
    return v


async def _fetch_team(db: AsyncSession, sport: str, team_id: Any) -> tuple:
    """Return (team_abbr, team_name) for a team_id in <sport>.teams, or None."""
    cols = {"teams": {}}  # noqa: F841  placeholder to make linters happy
    try:
        result = await db.execute(
            text(f"SELECT id, name, abbreviation FROM {sport}.teams WHERE id = :tid LIMIT 1"),
            {"tid": int(team_id)},
        )
        row = result.mappings().first()
        if row is None:
            return None
        return row.get("abbreviation"), row.get("name")
    except Exception:  # noqa: BLE001  — schema/column differences across sports
        return None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@admin_router.get("")
async def list_configs(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text(
            """
            SELECT * FROM public.auto_generation_configs
            ORDER BY sport ASC, cadence ASC, title ASC
            """
        )
    )
    rows = result.mappings().all()
    return [_row(r) for r in rows]


@admin_router.get("/teams/{sport}")
async def list_teams(sport: str, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(sport)
    try:
        result = await db.execute(
            text(f"SELECT id, name, abbreviation FROM {sport}.teams ORDER BY name"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("list_teams failed for %s", sport)
        raise HTTPException(status_code=500, detail=f"Could not load teams for {sport}: {exc}") from exc
    rows = result.mappings().all()
    return [
        {"id": r["id"], "name": r["name"], "abbreviation": r.get("abbreviation")}
        for r in rows
    ]


@admin_router.post("")
async def create_config(req: CreateConfigRequest, db: AsyncSession = Depends(get_db)):
    sport = _validate_sport(req.sport)
    cadence = _validate_enum(req.cadence, CADENCES, "cadence", "daily")
    scope_type = _validate_enum(req.scope_type, SCOPES, "scope_type", "sport")

    team_abbr, team_name = req.team_abbr, req.team_name
    if scope_type == "team" and req.team_id is not None:
        resolved = await _fetch_team(db, sport, req.team_id)
        if resolved:
            team_abbr, team_name = resolved
    elif scope_type != "team":
        req.team_id, team_abbr, team_name = None, None, None

    result = await db.execute(
        text(
            """
            INSERT INTO public.auto_generation_configs
                (sport, title, description, instructions, cadence, scope_type,
                 team_id, team_abbr, team_name, template_article_id, status,
                 reasoning, visibility, word_min, word_max,
                 title_mode,
                 created_at, updated_at)
            VALUES
                (:sport, :title, :description, :instructions, :cadence, :scope_type,
                 :team_id, :team_abbr, :team_name, :template_article_id, :status,
                 :reasoning, :visibility, :word_min, :word_max,
                 :title_mode,
                 NOW(), NOW())
            RETURNING *
            """
        ),
        {
            "sport": sport,
            "title": req.title.strip(),
            "description": req.description,
            "instructions": req.instructions,
            "cadence": cadence,
            "scope_type": scope_type,
            "team_id": req.team_id if scope_type == "team" else None,
            "team_abbr": team_abbr if scope_type == "team" else None,
            "team_name": team_name if scope_type == "team" else None,
            "template_article_id": req.template_article_id,
            "status": _validate_enum(req.status, ("active", "inactive", "paused"), "status", "active"),
            "reasoning": _validate_enum(req.reasoning, REASONINGS, "reasoning", "medium"),
            "visibility": _validate_enum(req.visibility, VISIBILITIES, "visibility", "public"),
            "word_min": _coerce_word_range(req.word_min, req.word_max)[0],
            "word_max": _coerce_word_range(req.word_min, req.word_max)[1],
            "title_mode": _validate_enum(req.title_mode, TITLE_MODES, "title_mode", "fixed"),
        },
    )
    await db.commit()
    row = result.mappings().first()
    return _row(row)


@admin_router.post("/from-article")
async def create_from_article(req: FromArticleRequest, db: AsyncSession = Depends(get_db)):
    """Import an existing original article as a continuous generation config."""
    sport = _validate_sport(req.sport)
    cadence = _validate_enum(req.cadence, CADENCES, "cadence", "daily")
    scope_type = _validate_enum(req.scope_type, SCOPES, "scope_type", "sport")

    # Resolve denormalized team fields if team-scoped.
    team_abbr, team_name = req.team_abbr, req.team_name
    if scope_type == "team" and req.team_id is not None:
        resolved = await _fetch_team(db, sport, req.team_id)
        if resolved:
            team_abbr, team_name = resolved

    # Verify the source article exists (best-effort; soft if table schema differs).
    template_id = req.article_id
    try:
        src = await db.execute(
            text(f"SELECT id FROM public.original_articles WHERE id = :aid LIMIT 1"),
            {"aid": req.article_id},
        )
        if src.mappings().first() is None:
            raise HTTPException(status_code=404, detail="Source original article not found.")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 — table may not exist yet in dev
        template_id = None

    result = await db.execute(
        text(
            """
            INSERT INTO public.auto_generation_configs
                (sport, title, description, instructions, cadence, scope_type,
                 team_id, team_abbr, team_name, template_article_id, status,
                 reasoning, visibility, word_min, word_max,
                 title_mode,
                 created_at, updated_at)
            VALUES
                (:sport, :title, :description, :instructions, :cadence, :scope_type,
                 :team_id, :team_abbr, :team_name, :template_article_id, 'active',
                 :reasoning, :visibility, :word_min, :word_max,
                 :title_mode,
                 NOW(), NOW())
            RETURNING *
            """
        ),
        {
            "sport": sport,
            "title": req.title.strip(),
            "description": req.description,
            "instructions": req.instructions,
            "cadence": cadence,
            "scope_type": scope_type,
            "team_id": req.team_id if scope_type == "team" else None,
            "team_abbr": team_abbr if scope_type == "team" else None,
            "team_name": team_name if scope_type == "team" else None,
            "template_article_id": template_id,
            "reasoning": _validate_enum(req.reasoning, REASONINGS, "reasoning", "medium"),
            "visibility": _validate_enum(req.visibility, VISIBILITIES, "visibility", "public"),
            "word_min": _coerce_word_range(req.word_min, req.word_max)[0],
            "word_max": _coerce_word_range(req.word_min, req.word_max)[1],
            "title_mode": _validate_enum(req.title_mode, TITLE_MODES, "title_mode", "fixed"),
        },
    )
    await db.commit()
    row = result.mappings().first()
    return _row(row)


@admin_router.patch("/{config_id}")
async def update_config(config_id: int, req: UpdateConfigRequest, db: AsyncSession = Depends(get_db)):
    # Load existing row to merge partial updates.
    existing = await db.execute(
        text("SELECT * FROM public.auto_generation_configs WHERE id = :id"),
        {"id": config_id},
    )
    row = existing.mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Auto-generation config not found.")
    cur = dict(row)

    if req.cadence is not None:
        cur["cadence"] = _validate_enum(req.cadence, CADENCES, "cadence", cur["cadence"])
    if req.scope_type is not None:
        cur["scope_type"] = _validate_enum(req.scope_type, SCOPES, "scope_type", cur["scope_type"])
    if req.status is not None:
        cur["status"] = _validate_enum(req.status, ("active", "inactive", "paused"), "status", cur["status"])
    if req.reasoning is not None:
        cur["reasoning"] = _validate_enum(req.reasoning, REASONINGS, "reasoning", cur["reasoning"])
    if req.visibility is not None:
        cur["visibility"] = _validate_enum(req.visibility, VISIBILITIES, "visibility", cur["visibility"])
    if req.title_mode is not None:
        cur["title_mode"] = _validate_enum(req.title_mode, TITLE_MODES, "title_mode", cur["title_mode"])
    if req.word_min is not None:
        cur["word_min"] = req.word_min
    if req.word_max is not None:
        cur["word_max"] = req.word_max
    cur["word_min"], cur["word_max"] = _coerce_word_range(cur.get("word_min"), cur.get("word_max"))

    for k in ("title", "description", "instructions", "team_id"):
        v = getattr(req, k, None)
        if v is not None:
            cur[k] = v

    # If scope becomes non-team, clear team fields. If team-scoped and we have a
    # team_id but no names, try to resolve them.
    if cur["scope_type"] != "team":
        cur["team_id"] = None
        cur["team_abbr"] = None
        cur["team_name"] = None
    else:
        if req.team_abbr is not None:
            cur["team_abbr"] = req.team_abbr
        if req.team_name is not None:
            cur["team_name"] = req.team_name
        if cur.get("team_id") is not None and (not cur.get("team_abbr") or not cur.get("team_name")):
            resolved = await _fetch_team(db, cur["sport"], cur["team_id"])
            if resolved:
                cur["team_abbr"], cur["team_name"] = resolved

    await db.execute(
        text(
            """
            UPDATE public.auto_generation_configs
            SET title=:title, description=:description, instructions=:instructions,
                cadence=:cadence, scope_type=:scope_type, team_id=:team_id,
                team_abbr=:team_abbr, team_name=:team_name, status=:status,
                reasoning=:reasoning, visibility=:visibility,
                word_min=:word_min, word_max=:word_max, title_mode=:title_mode,
                updated_at=NOW()
            WHERE id=:id
            """
        ),
        {
            "id": config_id,
            "title": (req.title or cur["title"]).strip(),
            "description": req.description if req.description is not None else cur["description"],
            "instructions": req.instructions if req.instructions is not None else cur["instructions"],
            "cadence": cur["cadence"],
            "scope_type": cur["scope_type"],
            "team_id": cur["team_id"],
            "team_abbr": cur["team_abbr"],
            "team_name": cur["team_name"],
            "status": cur["status"],
            "reasoning": cur["reasoning"],
            "visibility": cur["visibility"],
            "word_min": cur["word_min"],
            "word_max": cur["word_max"],
            "title_mode": cur["title_mode"],
        },
    )
    await db.commit()
    return _row((await db.execute(
        text("SELECT * FROM public.auto_generation_configs WHERE id = :id"),
        {"id": config_id},
    )).mappings().first())


@admin_router.delete("/{config_id}")
async def delete_config(config_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text("DELETE FROM public.auto_generation_configs WHERE id = :id RETURNING id"),
        {"id": config_id},
    )
    await db.commit()
    if result.mappings().first() is None:
        raise HTTPException(status_code=404, detail="Auto-generation config not found.")
    return {"ok": True, "deleted_id": config_id}
