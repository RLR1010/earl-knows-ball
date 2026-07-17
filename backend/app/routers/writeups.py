"""Write-up API endpoints — trigger generation, list, preview, publish."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.writeups.mlb.generator import MLBWriteupGenerator
from app.writeups.nfl.generator import NFLWriteupGenerator
from app.writeups.nba.generator import NBAGameWriteupGenerator

logger = logging.getLogger("writeups")
router = APIRouter(prefix="/writeups", tags=["writeups"])


# ──────────────────────────────────────────────
#  Public write-up (no picks / no betting data)
# ──────────────────────────────────────────────


@router.get("/{sport}/{game_id}/public")
async def get_public_writeup(
    sport: str,
    game_id: int,
    as_of_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Generate or return a cached public-only write-up.

    This endpoint:
    - Fetches a stripped research brief (no betting lines, ATS splits,
      line movement, or model predictions)
    - Makes a separate, shorter LLM call with a 1200-2000 word target
    - Does NOT include any premium / insider content

    If a public write-up already exists in the DB for this game, we
    return it directly. Otherwise we generate and cache it.
    """
    from datetime import datetime as dt_module
    from sqlalchemy import select

    as_of_dt = dt_module.fromisoformat(as_of_date) if as_of_date else None

    # Pick the right generator for the sport
    if sport == "mlb":
        from app.writeups.mlb.research import get_public_research_brief
        research_fn = get_public_research_brief
        generator_cls = MLBWriteupGenerator
    elif sport == "nfl":
        from app.writeups.nfl.research import get_public_research_brief
        research_fn = get_public_research_brief
        generator_cls = NFLWriteupGenerator
    elif sport == "nba":
        from app.writeups.nba.research import get_public_research_brief
        research_fn = get_public_research_brief
        generator_cls = NBAGameWriteupGenerator
    else:
        raise HTTPException(status_code=400, detail=f"Unknown sport: {sport}")

    generator = generator_cls()

    # Check if a cached public writeup exists
    table = f"{sport}.game_writeups"
    row = await db.execute(
        text(f"""
            SELECT id, game_id, title, public_content, version, status
            FROM {table}
            WHERE game_id = :game_id
            ORDER BY created_at DESC LIMIT 1
        """),
        {"game_id": game_id},
    )
    existing = row.first()

    if existing and existing.public_content:
        return {
            "id": existing.id,
            "game_id": existing.game_id,
            "title": existing.title,
            "content": existing.public_content,
            "version": existing.version,
            "cached": True,
        }

    # No cached version — generate fresh
    # Fetch stripped research brief directly
    stripped_research = await research_fn(db, game_id, as_of_dt)
    if "error" in stripped_research:
        raise HTTPException(status_code=502, detail=stripped_research["error"])

    is_historical = stripped_research.get("is_historical", False)
    result = await generator.generate_public(game_id, stripped_research, is_historical)
    if "error" in result:
        raise HTTPException(status_code=502, detail=result["error"])

    return {
        "game_id": game_id,
        "title": result.get("title", ""),
        "content": result.get("public_content", ""),
        "cached": False,
    }


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
    is_historical: Optional[bool] = Query(None),  # deprecated — auto-detected from game status
    as_of_date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Generate a write-up for an MLB game.

    is_historical is now auto-detected from the game's status in the
    database (F = final → historical). If explicitly passed, it overrides
    auto-detection.

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
# ──────────────────────────────────────────────
#  NFL WRITEUP ENDPOINTS
# ──────────────────────────────────────────────


@router.get("/nfl/games")
async def list_nfl_games_for_content(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    status: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Return NFL games enriched with write-up status for the content admin."""
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
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    rows = await db.execute(
        text(f"""SELECT g.id, g.date, g.week, g.game_type AS season_type,
                 g.venue,
                 ht.abbreviation AS home_abbr, ht.name AS home_name,
                 at.abbreviation AS away_abbr, at.name AS away_name,
                 CAST(g.status AS text) AS game_status,
                 w.id AS writeup_id, w.title AS writeup_title,
                 w.status AS writeup_status, w.version AS writeup_version
          FROM nfl.games g
          JOIN nfl.teams ht ON ht.id = g.home_team_id
          JOIN nfl.teams at ON at.id = g.away_team_id
          LEFT JOIN nfl.game_writeups w ON w.game_id = g.id
          WHERE {where_clause}
          ORDER BY g.date ASC"""),
        params,
    )
    return [
        {
            "id": r.id, "date": r.date.isoformat() if r.date else None,
            "week": r.week, "season_type": r.season_type,
            "home_team": r.home_abbr, "home_team_name": r.home_name,
            "away_team": r.away_abbr, "away_team_name": r.away_name,
            "venue": r.venue, "game_status": r.game_status,
            "writeup_id": r.writeup_id, "writeup_title": r.writeup_title,
            "writeup_status": r.writeup_status, "writeup_version": r.writeup_version,
        }
        for r in rows.mappings()
    ]


@router.get("/nfl/nearest-game")
async def nfl_nearest_game_date(
    date: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Find nearest NFL game dates using Python date math."""
    rows = await db.execute(
        text('SELECT DISTINCT CAST(g.date AS date) AS game_day'
            ' FROM nfl.games g'
            ' WHERE g.status IN (\'FINAL\', \'SCHEDULED\', \'IN_PROGRESS\')'
            ' AND g.game_type = \'REG\''
            ' ORDER BY game_day')
    )
    all_dates = [r[0] for r in rows.fetchall() if r[0]]

    from datetime import date as dt_date
    target = dt_date.fromisoformat(date[:10])

    prev_date = None
    for d in reversed(all_dates):
        if d < target:
            prev_date = d
            break

    next_date = None
    for d in all_dates:
        if d > target:
            next_date = d
            break

    return {
        "prev_date": str(prev_date) if prev_date else None,
        "next_date": str(next_date) if next_date else None,
    }

@router.post("/nfl/preview/{game_id}")
async def preview_nfl_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Preview an NFL writeup (no DB save)."""
    from app.writeups.nfl.generator import NFLWriteupGenerator
    gen = NFLWriteupGenerator()
    gen._db = db
    research = await gen.research_brief(game_id)
    return {"research": research}


@router.post("/nfl/preview-public/{game_id}")
async def preview_public_nfl_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Preview a public NFL writeup (no DB save, no picks)."""
    from app.writeups.nfl.generator import NFLWriteupGenerator
    gen = NFLWriteupGenerator()
    gen._db = db
    research = await gen.get_public_research(game_id)
    return {"research": research}


@router.post("/nfl/generate/{game_id}")
async def generate_nfl_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate and save a premium NFL writeup."""
    from app.writeups.nfl.generator import NFLWriteupGenerator
    gen = NFLWriteupGenerator()
    result = await gen.generate(db, game_id)
    row = await db.execute(
        text("""SELECT id, game_id, title, public_content, premium_content,
                 status, version, is_historical,
                 published_at, created_at
          FROM nfl.game_writeups WHERE game_id = :gid
          ORDER BY created_at DESC LIMIT 1"""),
        {"gid": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"ok": True, "note": "generated but read-back returned nothing"}
    return {
        "writeup_id": r["id"], "game_id": r["game_id"], "title": r["title"],
        "public_content": r["public_content"], "premium_content": r["premium_content"],
        "status": r["status"], "version": r["version"],
        "is_historical": r["is_historical"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "ok": True,
    }


@router.post("/nfl/generate-public/{game_id}")
async def generate_public_nfl_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate and save a public-only NFL writeup (no picks)."""
    from app.writeups.nfl.generator import NFLWriteupGenerator
    gen = NFLWriteupGenerator()
    gen._db = db
    research = await gen.get_public_research(game_id)
    result = await gen.generate_public(game_id, research)
    row = await db.execute(
        text("""SELECT id, game_id, title, public_content, premium_content,
                 status, version, is_historical,
                 published_at, created_at
          FROM nfl.game_writeups WHERE game_id = :gid
          ORDER BY created_at DESC LIMIT 1"""),
        {"gid": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"ok": True, "note": "generated but read-back returned nothing"}
    return {
        "writeup_id": r["id"], "game_id": r["game_id"], "title": r["title"],
        "public_content": r["public_content"], "premium_content": r["premium_content"],
        "status": r["status"], "version": r["version"],
        "is_historical": r["is_historical"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "ok": True,
    }


@router.get("/nfl/writeups")
async def list_nfl_writeups(
    game_id: Optional[int] = Query(None),
    status_filter: Optional[str] = Query("published", alias="status"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List NFL writeups with optional filters."""
    where = []
    params: dict = {}
    if game_id:
        where.append("w.game_id = :gid")
        params["gid"] = game_id
    if status_filter:
        where.append("w.status = :status")
        params["status"] = status_filter
    where_clause = " AND ".join(where) if where else "TRUE"
    offset = (page - 1) * per_page
    rows = await db.execute(
        text(f"""SELECT w.id, w.game_id, w.title, w.status, w.version,
                 w.is_historical, w.published_at, w.created_at,
                 g.week, g.date,
                 ht.abbreviation AS home, at.abbreviation AS away
          FROM nfl.game_writeups w
          JOIN nfl.games g ON w.game_id = g.id
          JOIN nfl.teams ht ON g.home_team_id = ht.id
          JOIN nfl.teams at ON g.away_team_id = at.id
          WHERE {where_clause}
          ORDER BY w.updated_at DESC
          LIMIT :limit OFFSET :offset"""),
        {**params, "limit": per_page, "offset": offset},
    )
    items = [
        {
            "writeup_id": r["id"], "game_id": r["game_id"],
            "title": r["title"], "status": r["status"],
            "version": r["version"], "is_historical": r["is_historical"],
            "published_at": r["published_at"].isoformat() if r["published_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "week": r["week"], "matchup": f"{r['away']} @ {r['home']}",
            "date": r["date"].isoformat() if r["date"] else None,
        }
        for r in rows.mappings()
    ]
    return {"items": items, "page": page, "per_page": per_page}


@router.get("/nfl/{writeup_id}")
async def get_nfl_writeup(
    writeup_id: int,
    tier: str = Query("premium"),  # "public" or "premium"
    db: AsyncSession = Depends(get_db),
):
    """Get a specific NFL writeup by ID.

    *tier* controls which content version is returned in the ``content`` field.
    """
    row = await db.execute(
        text("""SELECT w.id, w.game_id, w.title, w.public_content, w.premium_content,
                 w.status, w.version, w.is_historical,
                 w.research_brief, w.quality_checks,
                 w.published_at, w.created_at,
                 g.week, g.date,
                 ht.abbreviation AS home, at.abbreviation AS away
          FROM nfl.game_writeups w
          JOIN nfl.games g ON w.game_id = g.id
          JOIN nfl.teams ht ON g.home_team_id = ht.id
          JOIN nfl.teams at ON g.away_team_id = at.id
          WHERE w.id = :wid"""),
        {"wid": writeup_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail="Writeup not found")
    rb = r.get("research_brief")
    qc = r.get("quality_checks")
    content = r["premium_content"] if tier == "premium" else r["public_content"]
    return {
        "id": r["id"], "game_id": r["game_id"],
        "title": r["title"],
        "content": content,
        "public_content": r["public_content"],
        "premium_content": r["premium_content"],
        "status": r["status"],
        "version": r["version"], "is_historical": r["is_historical"],
        "research_brief": json.loads(rb) if isinstance(rb, str) else rb,
        "quality_checks": json.loads(qc) if isinstance(qc, str) else qc,
        "week": r["week"], "matchup": f"{r['away']} @ {r['home']}",
        "game_date": r["date"].isoformat() if r["date"] else None,
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
    }


@router.get("/nfl/game/{game_id}")
async def get_nfl_writeup_by_game(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the latest NFL writeup for a specific game."""
    row = await db.execute(
        text("""SELECT w.id AS writeup_id, w.game_id, w.title,
                 w.public_content, w.premium_content,
                 w.status, w.version, w.is_historical,
                 w.published_at, w.created_at
          FROM nfl.game_writeups w
          WHERE w.game_id = :gid
          ORDER BY w.created_at DESC LIMIT 1"""),
        {"gid": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"game_id": game_id, "has_writeup": False, "public_content": "", "premium_content": ""}
    return {
        "writeup_id": r["writeup_id"], "game_id": r["game_id"],
        "title": r["title"], "public_content": r["public_content"],
        "premium_content": r["premium_content"], "status": r["status"],
        "version": r["version"], "is_historical": r["is_historical"],
        "published_at": r["published_at"].isoformat() if r["published_at"] else None,
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "has_writeup": True,
    }


@router.patch("/nfl/{writeup_id}")
async def update_nfl_writeup(
    writeup_id: int,
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    """Update an NFL writeup's content."""
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
        return {"id": writeup_id, "updated": False, "note": "no fields to update"}
    updates.append("version = version + 1")
    updates.append("updated_at = NOW()")
    set_clause = ", ".join(updates)
    await db.execute(text(f"UPDATE nfl.game_writeups SET {set_clause} WHERE id = :wid"), params)
    await db.commit()
    return {"id": writeup_id, "updated": True}


@router.patch("/nfl/{writeup_id}/status")
async def update_nfl_writeup_status(
    writeup_id: int,
    status: str,
    db: AsyncSession = Depends(get_db),
):
    """Update the status of an NFL writeup."""
    valid = ("draft", "review", "published", "archived")
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid}")
    published_clause = ", published_at = NOW()" if status == "published" else ""
    await db.execute(
        text(f"UPDATE nfl.game_writeups SET status = :status{published_clause}, updated_at = NOW() WHERE id = :wid"),
        {"wid": writeup_id, "status": status},
    )
    await db.commit()
    return {"id": writeup_id, "status": status, "ok": True}


# ══════════════════════════════════════════════
# NBA — writeups
# ══════════════════════════════════════════════


@router.get("/nba/games")
async def list_nba_games_for_content(
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    season_year: Optional[int] = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List NBA games for the content admin."""
    filters: list[str] = []
    params: dict = {}

    if season_year:
        filters.append("s.year = :syear")
        params["syear"] = season_year
    if from_date:
        filters.append("g.date::date >= :from_d")
        try:
            from datetime import date as _date_type
            params["from_d"] = _date_type.fromisoformat(str(from_date))
        except (ValueError, TypeError):
            params["from_d"] = str(from_date)
    if to_date:
        filters.append("g.date::date <= :to_d")
        try:
            from datetime import date as _date_type
            params["to_d"] = _date_type.fromisoformat(str(to_date))
        except (ValueError, TypeError):
            params["to_d"] = str(to_date)

    where_clause = " AND ".join(filters) if filters else "TRUE"

    query = f"""
        SELECT
            g.id,
            g.date,
            ht.name AS home_team,
            ht.abbreviation AS home_abbr,
            at.name AS away_team,
            at.abbreviation AS away_abbr,
            CAST(g.status AS text) AS game_status,
            g.home_score,
            g.away_score,
            w.id AS writeup_id,
            w.status AS writeup_status
        FROM nba.games g
        JOIN nba.teams ht ON g.home_team_id = ht.id
        JOIN nba.teams at ON g.away_team_id = at.id
        JOIN nba.seasons s ON g.season_id = s.id
        LEFT JOIN nba.game_writeups w ON w.game_id = g.id
        WHERE {where_clause}
        ORDER BY g.date ASC
        LIMIT :lim
    """
    params["lim"] = limit
    result = await db.execute(text(query), params)
    rows = result.fetchall()
    return [
        {
            "id": r[0],
            "date": str(r[1]) if r[1] else "",
            "home_team": r[2],
            "home_abbr": r[3],
            "away_team": r[4],
            "away_abbr": r[5],
            "status": r[6],
            "home_score": r[7],
            "away_score": r[8],
            "writeup_id": r[9],
            "writeup_status": r[10],
        }
        for r in rows
    ]


@router.get("/nba/nearest-game")
async def nba_nearest_game_date(
    date: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Find nearest NBA game dates — prev_date (most recent before target)
    and next_date (first after target). Returns {prev_date, next_date}.
    """
    if date:
        try:
            target_dt = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            target_dt = datetime.now(timezone.utc).date()
    else:
        target_dt = datetime.now(timezone.utc).date()

    # Most recent game day BEFORE target (any status)
    prev_result = await db.execute(
        text("""
            SELECT DISTINCT g.date::date AS d
            FROM nba.games g
            WHERE g.date::date < :target
            ORDER BY d DESC
            LIMIT 1
        """),
        {"target": target_dt},
    )
    prev_row = prev_result.fetchone()

    # First game day AFTER target (any status)
    next_result = await db.execute(
        text("""
            SELECT DISTINCT g.date::date AS d
            FROM nba.games g
            WHERE g.date::date >= :target
            ORDER BY d ASC
            LIMIT 1
        """),
        {"target": target_dt},
    )
    next_row = next_result.fetchone()

    return {
        "prev_date": str(prev_row[0]) if prev_row else None,
        "next_date": str(next_row[0]) if next_row else None,
    }


# ── Preview (simulate generation without saving) ──────────────


@router.post("/nba/preview/{game_id}")
async def preview_nba_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Preview NBA write-up research data without saving."""
    gen = NBAGameWriteupGenerator()
    gen._db = db
    research = await gen.research_brief(game_id)
    if "error" in research:
        raise HTTPException(status_code=404, detail=research["error"])
    return {"research_brief": research}


@router.post("/nba/preview-public/{game_id}")
async def preview_nba_public_writeup(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Preview public NBA write-up research data without saving."""
    gen = NBAGameWriteupGenerator()
    gen._db = db
    research = await gen.research_brief(game_id)
    if "error" in research:
        raise HTTPException(status_code=404, detail=research["error"])
    return {"research_brief": research}


# ── Generation ──────────────────────────────────────────────────


@router.post("/nba/generate/{game_id}")
async def generate_nba_writeup(
    game_id: int,
    historical: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Generate and store a premium NBA write-up."""
    gen = NBAGameWriteupGenerator()
    writeup, qc_results = await gen.generate(db, game_id, is_historical=historical)
    if "error" in writeup:
        raise HTTPException(status_code=502, detail=writeup["error"])
    writeup_id = await gen.store(game_id, writeup, qc_results, db=db)
    return {"id": writeup_id, "status": "created"}


@router.post("/nba/generate-public/{game_id}")
async def generate_nba_public_writeup(
    game_id: int,
    historical: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    """Generate and store a public NBA write-up."""
    gen = NBAGameWriteupGenerator()
    gen._db = db
    research = await gen.get_public_research(game_id)
    writeup, qc_results = await gen.generate_public(game_id, research, is_historical=historical)
    if "error" in writeup:
        raise HTTPException(status_code=502, detail=writeup["error"])
    writeup_id = await gen.store(game_id, writeup, qc_results, db=db)
    return {"id": writeup_id, "status": "created"}


# ── List / Get ─────────────────────────────────────────────────


@router.get("/nba/writeups")
async def list_nba_writeups(
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List NBA write-ups."""
    filters = []
    params: dict = {}
    if status:
        filters.append("w.status = :status")
        params["status"] = status
    where = " AND ".join(filters) if filters else "TRUE"
    result = await db.execute(
        text(f"""
            SELECT w.id, w.game_id, w.title, w.status, w.version,
                   w.created_at, w.updated_at, w.published_at,
                   g.date, ht.name AS home, ht.abbreviation AS home_abbr,
                   at.name AS away, at.abbreviation AS away_abbr
            FROM nba.game_writeups w
            JOIN nba.games g ON w.game_id = g.id
            JOIN nba.teams ht ON g.home_team_id = ht.id
            JOIN nba.teams at ON g.away_team_id = at.id
            WHERE {where}
            ORDER BY COALESCE(w.published_at, w.created_at) DESC
            LIMIT :lim
        """),
        {**params, "lim": limit},
    )
    rows = result.fetchall()
    return [
        {
            "id": r[0],
            "game_id": r[1],
            "title": r[2],
            "status": r[3],
            "version": r[4],
            "created_at": str(r[5]) if r[5] else "",
            "updated_at": str(r[6]) if r[6] else "",
            "published_at": str(r[7]) if r[7] else "",
            "game_date": str(r[8])[:10] if r[8] else "",
            "home_team": r[9],
            "home_abbr": r[10],
            "away_team": r[11],
            "away_abbr": r[12],
        }
        for r in rows
    ]


@router.get("/nba/{writeup_id}")
async def get_nba_writeup(
    writeup_id: int,
    tier: str = Query("premium"),  # "public" or "premium"
    db: AsyncSession = Depends(get_db),
):
    """Get a specific NBA write-up by ID. Matches MLB pattern for frontend compatibility."""
    result = await db.execute(
        text("""
            SELECT w.id, w.game_id, w.title, w.public_content, w.premium_content,
                   w.status, w.version, w.is_historical, w.generated_by,
                   w.total_tokens, w.published_at, w.created_at, w.updated_at,
                   w.research_brief, w.quality_checks,
                   g.date, ht.name AS home, ht.abbreviation AS home_abbr,
                   at.name AS away, at.abbreviation AS away_abbr
            FROM nba.game_writeups w
            JOIN nba.games g ON w.game_id = g.id
            JOIN nba.teams ht ON g.home_team_id = ht.id
            JOIN nba.teams at ON g.away_team_id = at.id
            WHERE w.id = :wid
        """),
        {"wid": writeup_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Write-up not found")

    content = row[4] if tier == "premium" else row[3]

    return {
        "id": row[0],
        "game_id": row[1],
        "title": row[2],
        "content": content,
        "matchup": f"{row[18]} @ {row[16]}",
        "status": row[5],
        "version": row[6],
        "is_historical": row[7],
        "generated_by": row[8],
        "total_tokens": row[9],
        "published_at": str(row[10]) if row[10] else "",
        "created_at": str(row[11]) if row[11] else "",
        "updated_at": str(row[12]) if row[12] else "",
        "research_brief": json.loads(row[13]) if isinstance(row[13], str) else row[13],
        "quality_checks": json.loads(row[14]) if isinstance(row[14], str) else row[14],
        "game_date": str(row[15])[:10] if row[15] else "",
        "home_team": row[16],
        "home_abbr": row[17],
        "away_team": row[18],
        "away_abbr": row[19],
    }


@router.get("/nba/game/{game_id}")
async def get_nba_writeup_by_game_id(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the NBA write-up for a specific game."""
    result = await db.execute(
        text("""
            SELECT w.id, w.game_id, w.title, w.public_content, w.premium_content,
                   w.status, w.version
            FROM nba.game_writeups w
            WHERE w.game_id = :gid
            ORDER BY w.created_at DESC
            LIMIT 1
        """),
        {"gid": game_id},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No write-up found for this game")
    return {
        "id": row[0],
        "game_id": row[1],
        "title": row[2],
        "public_content": row[3],
        "premium_content": row[4],
        "status": row[5],
        "version": row[6],
    }


@router.patch("/nba/{writeup_id}")
async def update_nba_writeup(
    writeup_id: int,
    title: Optional[str] = Query(None),
    public_content: Optional[str] = Query(None),
    premium_content: Optional[str] = Query(None),
    research_brief: Optional[str] = Query(None),
    quality_checks: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Update an NBA write-up."""
    updates: list[str] = []
    params: dict = {"wid": writeup_id}
    if title is not None:
        updates.append("title = :title")
        params["title"] = title
    if public_content is not None:
        updates.append("public_content = :pc")
        params["pc"] = public_content
    if premium_content is not None:
        updates.append("premium_content = :prc")
        params["prc"] = premium_content
    if research_brief is not None:
        updates.append("research_brief = :rb::jsonb")
        params["rb"] = research_brief
    if quality_checks is not None:
        updates.append("quality_checks = :qc::jsonb")
        params["qc"] = quality_checks
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates.append("version = version + 1")
    updates.append("updated_at = NOW()")
    set_clause = ", ".join(updates)
    await db.execute(text(f"UPDATE nba.game_writeups SET {set_clause} WHERE id = :wid"), params)
    await db.commit()
    return {"id": writeup_id, "updated": True}


@router.patch("/nba/{writeup_id}/status")
async def update_nba_writeup_status(
    writeup_id: int,
    status: str,
    db: AsyncSession = Depends(get_db),
):
    """Update the status of an NBA writeup."""
    valid = ("draft", "review", "published", "archived")
    if status not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid}")
    published_clause = ", published_at = NOW()" if status == "published" else ""
    await db.execute(
        text(f"UPDATE nba.game_writeups SET status = :status{published_clause}, updated_at = NOW() WHERE id = :wid"),
        {"wid": writeup_id, "status": status},
    )
    await db.commit()
    return {"id": writeup_id, "status": status, "ok": True}

