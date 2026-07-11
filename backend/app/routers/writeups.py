"""Write-up API endpoints — trigger generation, list, preview, publish."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.writeups.mlb.generator import MLBWriteupGenerator

logger = logging.getLogger("writeups")
router = APIRouter(prefix="/writeups", tags=["writeups"])


# ──────────────────────────────────────────────
#  Games for content admin
# ──────────────────────────────────────────────

@router.get("/mlb/games")
async def list_mlb_games_for_content(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return MLB games enriched with write-up status for the content admin."""
    from datetime import datetime as dt_module

    conditions = []
    params: dict = {}

    if from_date:
        conditions.append("g.date >= :from_date")
        params["from_date"] = dt_module.strptime(from_date[:10], "%Y-%m-%d").date()
    if to_date:
        conditions.append("g.date <= :to_date")
        params["to_date"] = dt_module.strptime(to_date[:10], "%Y-%m-%d").date()
    if status:
        conditions.append("w.status = :status")
        params["status"] = status

    where = " AND " + " AND ".join(conditions) if conditions else ""

    # Build the WHERE clause properly after the FROM/JOIN block.
    # If no filters, use a 1=1 no-op condition.
    where_clause = " AND ".join(conditions) if conditions else "1=1"

    rows = await db.execute(
        text(f"""
            SELECT
                g.id, g.date, g.venue,
                g.home_team_id, g.away_team_id,
                ht.abbreviation AS home_abbr,
                ht.name AS home_name,
                at.abbreviation AS away_abbr,
                at.name AS away_name,
                w.id AS writeup_id,
                w.title AS writeup_title,
                w.status AS writeup_status,
                w.version AS writeup_version
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            LEFT JOIN mlb.game_writeups w ON w.game_id = g.id
            WHERE {where_clause}
            ORDER BY g.date ASC
        """),
        params,
    )

    return [
        {
            "id": r.id,
            "date": r.date.isoformat() if r.date else None,
            "home_team": r.home_abbr,
            "home_team_name": r.home_name,
            "away_team": r.away_abbr,
            "away_team_name": r.away_name,
            "venue": r.venue,
            "writeup_id": r.writeup_id,
            "writeup_title": r.writeup_title,
            "writeup_status": r.writeup_status,
            "writeup_version": r.writeup_version,
        }
        for r in rows.mappings()
    ]


# ──────────────────────────────────────────────
#  Generate
# ──────────────────────────────────────────────

@router.post("/mlb/generate/{game_id}")
async def generate_mlb_writeup(
    game_id: int,
    is_historical: bool = Query(False),
    as_of_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Generate a write-up for an MLB game.

    - *is_historical*: if True, the AI writes as if the game hasn't happened.
    - *as_of_date*: ISO-8601 date to filter research data (used for historical
      write-ups to only show data available before that date).
    """
    # Validate game exists
    game = await db.execute(
        text("SELECT id FROM mlb.games WHERE id = :gid"),
        {"gid": game_id},
    )
    if not game.scalar():
        raise HTTPException(status_code=404, detail=f"Game {game_id} not found")

    as_of_date_parsed = (
        datetime.fromisoformat(as_of_date) if as_of_date else None
    )

    # Auto-detect historical: if the game was in the past, treat as historical
    game_row = await db.execute(
        text("SELECT date FROM mlb.games WHERE id = :gid"),
        {"gid": game_id},
    )
    game_date = game_row.scalar()
    if game_date and game_date < datetime.now(timezone.utc) and not as_of_date_parsed:
        is_historical = True
        as_of_date_parsed = game_date
        # Subtract 1 second so research queries (which use <=) exclude this game.
        # Otherwise the previewed game's final result leaks into the form/stats.
        as_of_date_parsed -= timedelta(seconds=1)

    gen = MLBWriteupGenerator()
    writeup, qc_results = await gen.generate(
        db, game_id, is_historical=is_historical, as_of_date=as_of_date_parsed,
    )

    if "error" in writeup:
        raise HTTPException(status_code=500, detail=writeup["error"])

    return {
        "game_id": game_id,
        "title": writeup.get("title", ""),
        "public_content": writeup.get("public_content", ""),
        "premium_content": writeup.get("premium_content", ""),
        "status": gen._derive_status(qc_results),
        "quality_checks": qc_results,
        "is_historical": is_historical,
    }


# ──────────────────────────────────────────────
#  List write-ups
# ──────────────────────────────────────────────

@router.get("/mlb/list")
async def list_mlb_writeups(
    status: Optional[str] = Query(None),
    game_id: Optional[int] = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List MLB write-ups, optionally filtered by status or game."""
    conditions = []
    params: dict = {}

    if status:
        conditions.append("w.status = :status")
        params["status"] = status
    if game_id:
        conditions.append("w.game_id = :game_id")
        params["game_id"] = game_id

    where = " AND " + " AND ".join(conditions) if conditions else ""

    rows = await db.execute(
        text(f"""
            SELECT
                w.id, w.game_id, w.title, w.status, w.version,
                w.is_historical, w.generated_by,
                w.published_at, w.created_at, w.updated_at,
                g.date AS game_date,
                ht.abbreviation AS home_team,
                at.abbreviation AS away_team
            FROM mlb.game_writeups w
            JOIN mlb.games g ON g.id = w.game_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            {where}
            ORDER BY w.updated_at DESC
            LIMIT :limit
        """),
        {**params, "limit": limit},
    )

    return [
        {
            "id": r.id,
            "game_id": r.game_id,
            "title": r.title,
            "status": r.status,
            "version": r.version,
            "is_historical": r.is_historical,
            "generated_by": r.generated_by,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "game_date": r.game_date.isoformat() if r.game_date else None,
            "matchup": f"{r.away_team} @ {r.home_team}",
        }
        for r in rows.mappings()
    ]


# ──────────────────────────────────────────────
#  Get / preview a write-up
# ──────────────────────────────────────────────

@router.get("/mlb/{writeup_id}")
async def get_mlb_writeup(
    writeup_id: int,
    tier: str = Query("premium"),  # "public" or "premium"
    db: AsyncSession = Depends(get_db),
):
    """Get a single write-up by ID.

    *tier* controls which content version is returned.
    """
    row = await db.execute(
        text("""
            SELECT
                w.id, w.game_id, w.title,
                w.public_content, w.premium_content,
                w.status, w.version, w.is_historical,
                w.generated_by, w.published_at, w.created_at,
                w.quality_checks,
                w.research_brief,
                g.date AS game_date,
                ht.abbreviation AS home_team,
                at.abbreviation AS away_team
            FROM mlb.game_writeups w
            JOIN mlb.games g ON g.id = w.game_id
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE w.id = :wid
        """),
        {"wid": writeup_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"Write-up {writeup_id} not found")

    content = r["premium_content"] if tier == "premium" else r["public_content"]

    return {
        "id": r["id"],
        "game_id": r["game_id"],
        "title": r["title"],
        "content": content,
        "matchup": f"{r['away_team']} @ {r['home_team']}",
        "status": r["status"],
        "version": r["version"],
        "is_historical": r["is_historical"],
        "generated_by": r["generated_by"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "game_date": r["game_date"].isoformat() if r["game_date"] else None,
        "quality_checks": r["quality_checks"],
        "research_brief": r["research_brief"],
    }


# ──────────────────────────────────────────────
#  Get write-up by game ID
# ──────────────────────────────────────────────

@router.get("/mlb/by-game/{game_id}")
async def get_mlb_writeup_by_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a write-up for a specific MLB game ID, returning both public and premium content."""
    row = await db.execute(
        text("""
            SELECT
                w.id AS writeup_id, w.game_id, w.title,
                w.public_content, w.premium_content,
                w.status, w.version, w.is_historical,
                w.published_at, w.created_at
            FROM mlb.game_writeups w
            WHERE w.game_id = :gid
            ORDER BY w.created_at DESC
            LIMIT 1
        """),
        {"gid": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"game_id": game_id, "has_writeup": False, "public_content": "", "premium_content": ""}

    return {
        "writeup_id": r["writeup_id"],
        "game_id": r["game_id"],
        "title": r["title"],
        "public_content": r["public_content"],
        "premium_content": r["premium_content"],
        "status": r["status"],
        "version": r["version"],
        "is_historical": r["is_historical"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "has_writeup": True,
    }


# ──────────────────────────────────────────────
#  Update write-up content
# ──────────────────────────────────────────────

@router.patch("/mlb/{writeup_id}")
async def update_mlb_writeup(
    writeup_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Update a write-up's content (title, public_content, premium_content)."""
    updates = []
    params: dict = {"wid": writeup_id}

    if "title" in body:
        updates.append("title = :title")
        params["title"] = body["title"]
    if "public_content" in body:
        updates.append("public_content = :public_content")
        params["public_content"] = body["public_content"]
    if "premium_content" in body:
        updates.append("premium_content = :premium_content")
        params["premium_content"] = body["premium_content"]

    if not updates:
        return {"error": "No fields to update"}

    updates.append("updated_at = NOW()")
    set_clause = ", ".join(updates)

    result = await db.execute(
        text(f"UPDATE mlb.game_writeups SET {set_clause} WHERE id = :wid RETURNING id"),
        params,
    )
    await db.commit()

    if result.scalar() is None:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Write-up {writeup_id} not found")

    return {"id": writeup_id, "ok": True}


# ──────────────────────────────────────────────
#  Publish / update status
# ──────────────────────────────────────────────

@router.patch("/mlb/{writeup_id}/status")
async def update_writeup_status(
    writeup_id: int,
    status: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Update the status of a write-up (draft, review, published, archived)."""
    valid = ("draft", "review", "published", "archived")
    if status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of {valid}",
        )

    published_clause = ", published_at = NOW()" if status == "published" else ""
    await db.execute(
        text(f"""
            UPDATE mlb.game_writeups
            SET status = :status{published_clause}, updated_at = NOW()
            WHERE id = :wid
        """),
        {"wid": writeup_id, "status": status},
    )
    await db.commit()

    return {"id": writeup_id, "status": status, "ok": True}
