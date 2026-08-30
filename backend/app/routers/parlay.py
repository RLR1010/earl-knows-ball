"""
Parlay builder — read-only endpoint that surfaces selectable legs for the
ParlayBuilder frontend component.

This is a PURE READ over stored `game_predictions` rows joined to `games` and
`teams`. It NEVER runs inference — per project rule, all inference is done on
the current-live model only. The parlay math + correlation flags happen in
`app/handicapping/parlay.py`; this endpoint just serves the raw legs.

Endpoints:
  GET /api/parlay/legs?sport=mlb|nfl|nba
      -> list of upcoming games, each with ml / spread / total legs built from
         the stored prediction, carrying calibrated probs, book odds, EV, and
         the `favorite_side` meta (used by the correlation flags).

Served by the API box (8001) alongside the other user-facing reads.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.core.security import get_current_user, get_optional_current_user, user_is_premium
from app.models import User
from app.handicapping.parlay import (
    LEG_ML, LEG_SPREAD, LEG_TOTAL, Leg, top_ev_combos,
)

router = APIRouter()

VALID_SPORTS = {"mlb", "nfl", "nba"}


def _run_line_col(sport: str) -> str:
    """MLB labels the ATS leg 'run line' and stores it in run_line_pick."""
    return "run_line_pick" if sport == "mlb" else "spread_pick"


def _side_from_pick(pick: Optional[str]) -> Optional[str]:
    """Extract the team abbreviation from an ATS/spread pick like 'CHC -1.5'."""
    if not pick:
        return None
    return pick.split()[0]


def _ml_side(game: Dict[str, Any], sport: str) -> Optional[str]:
    """Resolve ml_pick to a side ('home'/'away') by matching against the game's
    teams. ml_pick holds the team's ABBREVIATION for MLB/NFL, but the team's ID
    (as a string) for NBA — so match against both identifiers."""
    pick = game.get("ml_pick")
    if not pick:
        return None
    pk = str(pick).strip()
    home_key = str(game.get("home_team_id"))
    away_key = str(game.get("away_team_id"))
    if pk in (str(game.get("home_abbr")), home_key):
        return "home"
    if pk in (str(game.get("away_abbr")), away_key):
        return "away"
    return None


def _build_leg(
    game: Dict[str, Any],
    kind: str,
    sport: str,
) -> Optional[Dict[str, Any]]:
    """Build one normalized leg dict from a game+prediction row."""
    if kind == LEG_ML:
        pick = _ml_side(game, sport)                     # "home" / "away" / None
        prob = game.get("ml_conf_cal") or game.get("ml_conf")
        odds = game.get("ml_odds")
        ev = game.get("ml_ev")
        if pick not in ("home", "away"):
            return None
        side = game["home_abbr"] if pick == "home" else game["away_abbr"]
        team_label = game["home_name"] if pick == "home" else game["away_name"]
        label = f"{team_label} ML ({_fmt_odds(odds)})"
        # favorite_side = side the model actually favors on the moneyline.
        favorite_side = side
    elif kind == LEG_SPREAD:
        sp_col = _run_line_col(sport)
        pick = game.get(sp_col)
        prob = game.get("rl_conf_cal") if sport == "mlb" else game.get("ats_conf_cal")
        # fall back to raw confidence if calibrated missing
        if prob is None:
            prob = game.get("rl_conf") if sport == "mlb" else game.get("margin_conf")
        odds = game.get("ats_odds")
        ev = game.get("ats_ev")
        side = _side_from_pick(pick)
        if not pick or not side:
            return None
        label = f"{pick} ({_fmt_odds(odds)})"
        favorite_side = None  # not derivable from ATS odds alone
    else:  # LEG_TOTAL
        pick = game.get("ou_pick")                      # "Over" / "Under"
        prob = game.get("ou_conf_cal") or game.get("ou_conf")
        odds = game.get("ou_odds")
        ev = game.get("ou_ev")
        if pick not in ("Over", "Under", "over", "under"):
            return None
        label = f"{pick} total ({_fmt_odds(odds)})"
        side = None
        favorite_side = None

    # resolve favorite_side from ML leg was already set above; also allow it on
    # spread/total via the ML leg's favorite (handled in combine via meta).
    return {
        "game_id": game["game_id"],
        "sport": sport,
        "kind": kind,
        "label": label,
        "pick": pick,
        "side": side,
        "prob": float(prob) if prob is not None else None,
        "odds": odds,
        "ev": ev,
        "model_file": game.get("ats_model_file") if kind in (LEG_SPREAD, LEG_ML) else game.get("ou_model_file"),
        "is_calibrated": bool(game.get("_cal_available", False)),
        "favorite_side": favorite_side,
        "game_label": f"{game['home_abbr']} @ {game['away_abbr']}",
        "game_date": str(game["game_date"]),
    }


def _fmt_odds(odds: Optional[Any]) -> str:
    if odds is None:
        return "n/a"
    v = int(odds)
    return f"+{v}" if v > 0 else str(v)


# SQL template per sport. Columns differ:
#   MLB: run_line_pick / rl_conf(_cal) + ml_odds
#   NFL/NBA: spread_pick / margin_conf + ats_conf_cal
_LEGS_SQL = {
    "mlb": """
        SELECT
            g.id  AS game_id,
            g.date AS game_date,
            g.status AS status,
            g.home_team_id AS home_team_id, g.away_team_id AS away_team_id,
            ht.abbreviation AS home_abbr, ht.name AS home_name,
            at.abbreviation AS away_abbr, at.name AS away_name,
            p.ml_pick, p.ml_conf, p.ml_conf_cal, p.ml_odds, p.ml_ev,
            p.run_line_pick, p.rl_conf, p.rl_conf_cal, p.ats_odds, p.ats_ev,
            p.ou_pick, p.ou_conf, p.ou_conf_cal, p.ou_odds, p.ou_ev,
            p.ats_model_file, p.ou_model_file,
            COALESCE(p.ml_conf_cal IS NOT NULL OR p.rl_conf_cal IS NOT NULL OR p.ou_conf_cal IS NOT NULL, false) AS cal
        FROM mlb.game_predictions p
        JOIN mlb.games g ON g.id = p.game_id
        JOIN mlb.teams ht ON ht.id = g.home_team_id
        JOIN mlb.teams at ON at.id = g.away_team_id
        WHERE p.source = 'api' AND g.date > now()
        AND g.date < now() + interval '2 days'
        ORDER BY g.date
    """,
    "nfl": """
        SELECT
            g.id  AS game_id,
            g.date AS game_date,
            g.status AS status,
            g.home_team_id AS home_team_id, g.away_team_id AS away_team_id,
            ht.abbreviation AS home_abbr, ht.name AS home_name,
            at.abbreviation AS away_abbr, at.name AS away_name,
            p.ml_pick, p.ml_conf, p.ml_conf_cal, p.ml_odds, p.ml_ev,
            p.spread_pick, p.margin_conf, p.ats_conf_cal, p.ats_odds, p.ats_ev,
            p.ou_pick, p.ou_conf, p.ou_conf_cal, p.ou_odds, p.ou_ev,
            p.ats_model_file, p.ou_model_file,
            COALESCE(p.ml_conf_cal IS NOT NULL OR p.ats_conf_cal IS NOT NULL OR p.ou_conf_cal IS NOT NULL, false) AS cal
        FROM nfl.game_predictions p
        JOIN nfl.games g ON g.id = p.game_id
        JOIN nfl.teams ht ON ht.id = g.home_team_id
        JOIN nfl.teams at ON at.id = g.away_team_id
        WHERE p.source = 'api' AND g.date > now()
        AND g.date < now() + interval '7 days'
        ORDER BY g.date
    """,
    "nba": """
        SELECT
            g.id  AS game_id,
            g.date AS game_date,
            g.status AS status,
            g.home_team_id AS home_team_id, g.away_team_id AS away_team_id,
            ht.abbreviation AS home_abbr, ht.name AS home_name,
            at.abbreviation AS away_abbr, at.name AS away_name,
            p.ml_pick, p.ml_conf, p.ml_conf_cal, p.ml_odds, p.ml_ev,
            p.spread_pick, p.margin_conf, p.ats_conf_cal, p.ats_odds, p.ats_ev,
            p.ou_pick, p.ou_conf, p.ou_conf_cal, p.ou_odds, p.ou_ev,
            p.ats_model_file, p.ou_model_file,
            COALESCE(p.ml_conf_cal IS NOT NULL OR p.ats_conf_cal IS NOT NULL OR p.ou_conf_cal IS NOT NULL, false) AS cal
        FROM nba.game_predictions p
        JOIN nba.games g ON g.id = p.game_id
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at ON at.id = g.away_team_id
        WHERE p.source = 'api' AND g.date > now()
        AND g.date < now() + interval '7 days'
        ORDER BY g.date
    """,
}

# What kind of game a leg belongs to — used to drive the "same-game" builder.
_GAME_TYPE_LABEL = {
    "mlb": "run line",
    "nfl": "spread",
    "nba": "spread",
}


@router.get("/parlay/legs")
async def parlay_legs(
    sport: str = Query(..., description="mlb | nfl | nba"),
    db: AsyncSession = Depends(get_db),
    current_user: "User | None" = Depends(get_optional_current_user),
):
    """Return upcoming games as selectable parlay legs (ML / spread / total).

    Each game yields up to three legs carrying the model's calibrated
    probability, the book's odds, per-leg EV, and the `favorite_side` meta
    used by the correlation flags in the parlay engine.

    Premium gate: parlay building is a premium feature.
    """
    if not user_is_premium(current_user):
        raise HTTPException(status_code=403, detail="Premium subscription required")
    sport = (sport or "").strip().lower()
    if sport not in VALID_SPORTS:
        return {"error": f"sport must be one of {sorted(VALID_SPORTS)}", "games": []}

    sql = _LEGS_SQL[sport]
    result = await db.execute(text(sql))
    rows = result.fetchall()
    cols = result.keys()

    games: Dict[Any, Dict[str, Any]] = {}
    for r in rows:
        row = dict(zip(cols, r))
        gid = row["game_id"]
        row["_cal_available"] = bool(row.get("cal"))

        # Build legs first so we can source favorite_side from the ML leg.
        legs = {}
        for kind in (LEG_ML, LEG_SPREAD, LEG_TOTAL):
            leg = _build_leg(row, kind, sport)
            if leg is not None:
                legs[kind] = leg

        # favorite_side for correlation flags = the side the MODEL favors on
        # the moneyline (same side as the ML pick). Used to warn on
        # favorite-ML + Over / underdog-ML + Under same-game pairs.
        fav_side = None
        ml_leg = legs.get(LEG_ML)
        if ml_leg is not None:
            fav_side = ml_leg.get("side")

        game = {
            "game_id": gid,
            "sport": sport,
            "game_label": f"{row['home_abbr']} @ {row['away_abbr']}",
            "home_abbr": row["home_abbr"], "home_name": row["home_name"],
            "away_abbr": row["away_abbr"], "away_name": row["away_name"],
            "date": str(row["game_date"]),
            "status": row["status"],
            "favorite_side": fav_side,
            "legs": legs,
        }

        games[gid] = game

    # Empirical same-game correlation table (computed from settled results).
    # Keyed by pair_key so the client can look up same-game leg pairs.
    correlations: Dict[str, Dict[str, Any]] = {}
    try:
        cr = await db.execute(text(
            f"SELECT pair_key, kind_a, kind_b, n, p_a, p_b, "
            f"p_joint, p_indep, corr, is_block, note "
            f"FROM {sport}.correlations"
        ))
        for crow in cr.mappings():
            correlations[str(crow["pair_key"])] = {
                "kind_a": crow["kind_a"],
                "kind_b": crow["kind_b"],
                "n": crow["n"],
                "p_a": crow["p_a"],
                "p_b": crow["p_b"],
                "p_joint": crow["p_joint"],
                "p_indep": crow["p_indep"],
                "corr": crow["corr"],
                "is_block": crow["is_block"],
            }
    except Exception:
        # table may not exist yet; correlations are optional
        correlations = {}

    return {
        "sport": sport,
        "count": len(games),
        "games": list(games.values()),
        "correlations": correlations,
    }


@router.get("/parlay/combos")
async def parlay_combos(
    sport: str = Query(..., description="mlb | nfl | nba"),
    n: int = 5,
    max_legs: int = 4,
    min_ev: float = 0.0,
    db: AsyncSession = Depends(get_db),
    current_user: "User | None" = Depends(get_optional_current_user),
):
    """Return the top-EV cross-game parlays Earl can build right now.

    Uses the same legs as /parlay/legs, then searches the highest-EV
    combinations (independent, cross-game only) and returns them ranked so the
    UI can offer one-tap “Add combo” suggestions.

    Premium gate: parlay building is a premium feature.
    """
    sport = (sport or "").strip().lower()
    if sport not in VALID_SPORTS:
        return {"error": f"sport must be one of {sorted(VALID_SPORTS)}", "combos": []}
    if not user_is_premium(current_user):
        raise HTTPException(status_code=403, detail="Premium subscription required")
    max_legs = max(2, min(6, max_legs))
    n = max(1, min(12, n))

    legs_payload = await parlay_legs(sport=sport, db=db, current_user=current_user)
    if "error" in legs_payload:
        return legs_payload

    # flatten game legs dicts -> engine Leg objects
    all_legs: List[Leg] = []
    for game in legs_payload.get("games", []):
        for leg in game.get("legs", {}).values():
            if not leg:
                continue
            all_legs.append(Leg(
                game_id=leg["game_id"],
                sport=sport,
                kind=leg["kind"],
                label=leg["label"],
                pick=leg["pick"],
                side=leg.get("side"),
                prob=leg.get("prob") or 0.5,
                odds=leg.get("odds"),
                ev=leg.get("ev"),
                model_file=leg.get("model_file"),
                is_calibrated=leg.get("is_calibrated", False),
                meta={"favorite_side": game.get("favorite_side")},
            ))

    combos = top_ev_combos(all_legs, n=n, max_legs=max_legs, min_ev=min_ev)
    return {
        "sport": sport,
        "n_legs_searched": len(all_legs),
        "count": len(combos),
        "combos": combos,
    }


# ---------------------------------------------------------------------------
# Saved parlay tickets (premium, user-scoped, cross-sport)
# ---------------------------------------------------------------------------

class TicketLeg(BaseModel):
    """A single parlay leg as persisted on a saved ticket.

    Mirrors the frontend ParlayLeg shape exactly so a saved ticket round-trips
    verbatim. `sport` can be mlb | nfl | nba so one ticket can mix legs across
    sports. `game_label`/`game_date` snapshot display info at save time so a
    saved ticket renders even after the game resolves or drops off the
    “upcoming” legs list.
    """
    game_id: int
    sport: str
    kind: str  # ml | spread | total
    label: str = ""
    pick: str = ""
    side: Optional[str] = None
    prob: Optional[float] = None
    odds: Optional[float] = None
    decimal: Optional[float] = None
    ev: Optional[float] = None
    model_file: Optional[str] = None
    is_calibrated: bool = False
    favorite_side: Optional[str] = None
    game_label: str = ""
    game_date: str = ""


class TicketIn(BaseModel):
    """Body for saving / updating a ticket.

    `ticket_id` is optional; when present the existing ticket is upserted
    (same user only), otherwise a new ticket is created.
    """
    name: Optional[str] = "My Parlay"
    legs: List[TicketLeg] = []
    ticket_id: Optional[int] = None


def _require_premium_user(current_user: Optional[User]):
    if not user_is_premium(current_user):
        raise HTTPException(status_code=403, detail="Premium subscription required")
    return current_user.id


@router.post("/parlay/tickets")
async def save_parlay_ticket(
    body: TicketIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Save (or update) a parlay ticket.

    A ticket can mix legs from any sport (mlb/nfl/nba). Returns the stored
    ticket including its id and updated_at.
    """
    user_id = _require_premium_user(current_user)
    legs_json = [leg.model_dump() for leg in body.legs] if body.legs else []

    if body.ticket_id:
        # update an existing ticket (owner only)
        row = await db.execute(
            text("SELECT id FROM parlay_tickets WHERE id = :id AND user_id = :uid"),
            {"id": body.ticket_id, "uid": user_id},
        )
        if row.scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail="Ticket not found")
        result = await db.execute(
            text(
                """UPDATE parlay_tickets
                   SET name = :name, legs = :legs, updated_at = now()
                   WHERE id = :id AND user_id = :uid
                   RETURNING id, name, legs, created_at, updated_at"""
            ),
            {"name": body.name or "My Parlay", "legs": json.dumps(legs_json), "id": body.ticket_id, "uid": user_id},
        )
    else:
        result = await db.execute(
            text(
                """INSERT INTO parlay_tickets (user_id, name, legs)
                   VALUES (:uid, :name, :legs)
                   RETURNING id, name, legs, created_at, updated_at"""
            ),
            {"uid": user_id, "name": body.name or "My Parlay", "legs": json.dumps(legs_json)},
        )
    await db.commit()
    row = result.fetchone()
    return _ticket_row(row)


@router.get("/parlay/tickets")
async def list_parlay_tickets(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List the current user's saved parlay tickets (newest first)."""
    user_id = _require_premium_user(current_user)
    rows = (await db.execute(
        text(
            """SELECT id, name, legs, created_at, updated_at
               FROM parlay_tickets WHERE user_id = :uid ORDER BY updated_at DESC"""
        ),
        {"uid": user_id},
    )).fetchall()
    return {"tickets": [_ticket_row(r) for r in rows]}


@router.get("/parlay/tickets/{ticket_id}")
async def get_parlay_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Load a single saved ticket (owner only)."""
    user_id = _require_premium_user(current_user)
    row = (await db.execute(
        text(
            """SELECT id, name, legs, created_at, updated_at
               FROM parlay_tickets WHERE id = :id AND user_id = :uid"""
        ),
        {"id": ticket_id, "uid": user_id},
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return _ticket_row(row)


@router.delete("/parlay/tickets/{ticket_id}")
async def delete_parlay_ticket(
    ticket_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a saved ticket (owner only)."""
    user_id = _require_premium_user(current_user)
    result = await db.execute(
        text("DELETE FROM parlay_tickets WHERE id = :id AND user_id = :uid"),
        {"id": ticket_id, "uid": user_id},
    )
    await db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"deleted": True, "ticket_id": ticket_id}


def _ticket_row(row):
    legs = row[2]
    if isinstance(legs, str):
        legs = json.loads(legs)
    return {
        "id": row[0],
        "name": row[1],
        "legs": legs,
        "created_at": row[3].isoformat() if row[3] else None,
        "updated_at": row[4].isoformat() if row[4] else None,
    }

