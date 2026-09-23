"""Power Rankings — public read API (sport-parameterised).

Serves weekly, points-denominated team power ratings + a short LLM blurb per
team. Backing tables (written by the weekly job on compute):

    {sport}.power_ratings          (season, week, team_id, rating, rank, deltas, ...)
    {sport}.power_ranking_blurbs   (season, week, team_id, blurb, ...)

Endpoints (mounted under /power-rankings in main.py):
  GET /power-rankings/{sport}/current          -> latest published week
  GET /power-rankings/{sport}/weeks            -> available (season, week) snapshots
  GET /power-rankings/{sport}/weeks/{week}     -> a specific week (?season= optional)
  GET /power-rankings/{sport}/team/{abbr}      -> one team's rating history
  GET /power-rankings/{sport}/meta             -> model/methodology summary

Read-only + DB-shared, so this is a user-facing (api) router. Sports without a
published table yet degrade to ok:false / 404 rather than erroring.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.security import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/power-rankings", tags=["power-rankings"])

_SPORTS = {"nfl", "nba", "mlb"}

_METHODOLOGY = {
    "nfl": (
        "Earl's NFL Power Rating is a single number in POINTS on a neutral field: a team's "
        "rating minus its opponent's rating approximates the spread. It is built from a "
        "ridge-regularised least-squares fit on game margins (an SRS/Massey-style model) with "
        "a fitted home-field term, recency weighting (recent games count more), and shrinkage "
        "toward last season's final ratings that fades as real games accrue. Updates use FINAL "
        "games only and never look ahead."
    ),
    "nba": (
        "Earl's NBA Power Rating is a single number in POINTS on a neutral court: a team's "
        "rating minus its opponent's rating approximates the spread. It is built from a "
        "ridge-regularised least-squares fit on game margins (an SRS/Massey-style model) with "
        "a fitted home-court term, recency weighting (recent games count more), and shrinkage "
        "toward the prior season's final ratings that fades as games accrue. Weeks are cut "
        "from the season's first played game. Updates use FINAL games only and never look ahead."
    ),
    "mlb": (
        "Earl's MLB Power Rating is a single number in RUNS on a neutral field: a team's "
        "rating minus its opponent's rating approximates the expected margin. It is built "
        "from a ridge-regularised least-squares fit on game margins (an SRS/Massey-style "
        "model) with a fitted home-field term, recency weighting (recent games count more), "
        "and shrinkage toward the prior season's final ratings that fades as games accrue. "
        "Updates use FINAL regular-season games only and never look ahead."
    ),
}


def _validate(sport: str) -> str:
    s = sport.lower()
    if s not in _SPORTS:
        raise HTTPException(status_code=404, detail=f"unknown sport '{sport}'")
    return s


async def _resolve_week(db, sport: str, season: Optional[int], week: Optional[int]):
    """Return (season, week) — defaults to the most recent published snapshot."""
    if season is not None and week is not None:
        return season, week
    row = (await db.execute(text(f"""
        SELECT season, week FROM {sport}.power_ratings
        ORDER BY season DESC, week DESC LIMIT 1
    """))).first()
    if not row:
        return None, None
    return (season or row[0]), (week or row[1])


def _row_dict(r) -> dict[str, Any]:
    m = dict(r._mapping)
    comps = m.pop("components", None) or {}
    if isinstance(comps, str):
        import json
        try:
            comps = json.loads(comps)
        except Exception:
            comps = {}
    m["components"] = comps
    return m


async def _week_payload(db, sport: str, season: int, week: int):
    rows = (await db.execute(text(f"""
        SELECT p.rank, p.prev_rank, p.rank_delta, p.rating, p.rating_delta, p.sos,
               p.games_played, p.prior_rating, p.components, p.model_version,
               p.wins, p.losses, p.ties, p.injury_adj, p.market_rating, p.market_delta,
               p.team_id, p.team_abbr, p.as_of_date, p.generated_at,
               t.name AS team_name, b.blurb
        FROM {sport}.power_ratings p
        LEFT JOIN {sport}.teams t ON t.id = p.team_id
        LEFT JOIN {sport}.power_ranking_blurbs b
               ON b.season = p.season AND b.week = p.week AND b.team_id = p.team_id
        WHERE p.season = :s AND p.week = :w
        ORDER BY p.rank
    """), {"s": season, "w": week})).all()
    if not rows:
        return None
    first = dict(rows[0]._mapping)
    teams = []
    for r in rows:
        d = _row_dict(r)
        rd = d["rank_delta"]
        w, l, t_ = d.get("wins"), d.get("losses"), d.get("ties")
        rec_str = None
        if w is not None and l is not None:
            rec_str = f"{w}-{l}" + (f"-{t_}" if t_ else "")
        teams.append({
            "rank": d["rank"], "prev_rank": d["prev_rank"], "rank_delta": rd,
            # rank_movement: positive = climbed the board (prev_rank - rank);
            # rank_delta is stored as (rank - prev_rank), i.e. positive = dropped.
            "rank_movement": (None if rd is None else -rd),
            "rating": d["rating"], "rating_delta": d["rating_delta"], "sos": d["sos"],
            "wins": w, "losses": l, "ties": t_, "record": rec_str,
            "injury_adj": d.get("injury_adj"),
            "injuries": (d.get("components") or {}).get("injuries") or [],
            "market_rating": d.get("market_rating"),
            "market_delta": d.get("market_delta"),
            "games_played": d["games_played"], "prior_rating": d["prior_rating"],
            "team": {"id": d["team_id"], "abbr": d["team_abbr"], "name": d["team_name"] or d["team_abbr"]},
            "blurb": d["blurb"],
            "components": d["components"],
        })
    return {
        "ok": True, "sport": sport, "season": season, "week": week,
        "as_of_date": str(first["as_of_date"]) if first["as_of_date"] else None,
        "model_version": first["model_version"],
        "generated_at": first["generated_at"].isoformat() if first["generated_at"] else None,
        "teams": teams,
    }


@router.get("/{sport}/current")
async def current(db: AsyncSession = Depends(get_db), sport: str = ""):
    s = _validate(sport)
    try:
        season, week = await _resolve_week(db, s, None, None)
        if season is None:
            return {"ok": False, "sport": s, "teams": []}
        payload = await _week_payload(db, s, season, week)
        return payload or {"ok": False, "sport": s, "teams": []}
    except ProgrammingError:
        await db.rollback()
        return {"ok": False, "sport": s, "teams": []}


@router.get("/{sport}/weeks")
async def weeks(db: AsyncSession = Depends(get_db), sport: str = ""):
    s = _validate(sport)
    try:
        rows = (await db.execute(text(f"""
            SELECT DISTINCT season, week, as_of_date FROM {sport}.power_ratings
            ORDER BY season DESC, week DESC
        """))).all()
    except ProgrammingError:
        await db.rollback()
        rows = []
    return {"ok": True, "sport": s, "weeks": [
        {"season": r[0], "week": r[1],
         "as_of_date": str(r[2]) if r[2] else None} for r in rows]}


@router.get("/{sport}/weeks/{week}")
async def week_detail(db: AsyncSession = Depends(get_db), sport: str = "",
                      week: int = 0, season: Optional[int] = Query(None)):
    s = _validate(sport)
    season, week = await _resolve_week(db, s, season, week)
    if season is None:
        raise HTTPException(status_code=404, detail="no power ratings published")
    try:
        payload = await _week_payload(db, s, season, week)
    except ProgrammingError:
        await db.rollback()
        payload = None
    if not payload:
        raise HTTPException(status_code=404, detail=f"no snapshot for week {week}")
    return payload


@router.get("/{sport}/team/{abbr}")
async def team_history(db: AsyncSession = Depends(get_db), sport: str = "", abbr: str = ""):
    s = _validate(sport)
    abbr = abbr.upper()
    try:
        rows = (await db.execute(text(f"""
            SELECT p.season, p.week, p.as_of_date, p.rank, p.prev_rank, p.rank_delta,
                   p.rating, p.rating_delta, p.sos, p.games_played, p.wins, p.losses, p.ties,
                   p.injury_adj, p.team_abbr, t.name AS team_name, p.components, p.market_delta
            FROM {sport}.power_ratings p
            LEFT JOIN {sport}.teams t ON t.id = p.team_id
            WHERE upper(p.team_abbr) = :a
            ORDER BY p.season ASC, p.week ASC
        """), {"a": abbr})).all()
    except ProgrammingError:
        await db.rollback()
        rows = []
    if not rows:
        raise HTTPException(status_code=404, detail=f"no power ratings for '{abbr}'")
    latest = rows[-1]
    comps = latest[16]
    if isinstance(comps, (str, bytes)):
        import json as _json
        try:
            comps = _json.loads(comps)
        except Exception:
            comps = {}
    injuries = (comps or {}).get("injuries") or []
    blurb = (await db.execute(text(f"""
        SELECT blurb FROM {sport}.power_ranking_blurbs
        WHERE season = :s AND week = :w AND upper(team_abbr) = :a
    """), {"s": latest[0], "w": latest[1], "a": abbr})).first()
    return {
        "ok": True, "sport": s, "team": {"abbr": latest[12], "name": latest[13] or latest[12]},
        "current": {"season": latest[0], "week": latest[1], "rank": latest[3],
                    "rating": latest[6], "rating_delta": latest[7],
                    "injury_adj": latest[13], "injuries": injuries,
                    "market_delta": latest[17],
                    "blurb": blurb[0] if blurb else None},
        "history": [
            {"season": r[0], "week": r[1], "as_of_date": str(r[2]) if r[2] else None,
             "rank": r[3], "rating": r[6], "rating_delta": r[7], "sos": r[8],
             "injury_adj": r[13], "market_delta": r[17],
             "record": (f"{r[10]}-{r[11]}" + (f"-{r[12]}" if r[12] else "") if r[10] is not None else None)}
            for r in rows
        ],
    }


@router.get("/{sport}/meta")
async def meta(db: AsyncSession = Depends(get_db), sport: str = ""):
    s = _validate(sport)
    season, week = (None, None)
    try:
        season, week = await _resolve_week(db, s, None, None)
    except ProgrammingError:
        await db.rollback()
    return {
        "ok": True, "sport": s, "methodology": _METHODOLOGY.get(s),
        "season": season, "week": week,
    }


# --- admin: social-card generation (compute role; Playwright) --------------
admin_router = APIRouter(
    prefix="/power-rankings",
    tags=["power-rankings-admin"],
    dependencies=[Depends(require_admin)],
)


@admin_router.post("/{sport}/generate-card")
async def generate_card(
    sport: str, season: int = Query(...), week: int = Query(...)
):
    """Render the weekly power-rankings social card to a PNG on disk."""
    import asyncio as _asyncio

    s = _validate(sport)
    from app.social.power_rankings_card import generate

    path, url = await _asyncio.to_thread(generate, s, season, week)
    return {"ok": True, "sport": s, "season": season, "week": week,
            "path": path, "url": url}
