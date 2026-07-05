"""MLB stats endpoints — batting, pitching, team stats by season."""
import json

from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
import datetime
import decimal
import httpx
from sqlalchemy import select, text
from app.database import get_db, async_session
from app.models.mlb import MLBPlayer, MLBBettingLine
from app.handicapping.mlb.mlb_splits import MLBSplitAnalyzer
from app.handicapping.mlb.mlb_situational import MLBSituationalAnalyzer

router = APIRouter(prefix="/api")


# ── MLB Team Roster ────────────────────────────────────────────────

def _safe_int(val):
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# Map DB team abbreviation -> MLB Stats API team ID
MLB_API_TEAM_IDS = {
    "LAA": 108, "ARI": 109, "BAL": 110, "BOS": 111, "CHC": 112,
    "CIN": 113, "CLE": 114, "COL": 115, "DET": 116, "HOU": 117,
    "KC": 118, "LAD": 119, "WSH": 120, "NYM": 121, "OAK": 133,
    "PIT": 134, "SD": 135, "SEA": 136, "SF": 137, "STL": 138,
    "TB": 139, "TEX": 140, "TOR": 141, "MIN": 142, "PHI": 143,
    "ATL": 144, "CWS": 145, "MIA": 146, "NYY": 147, "MIL": 158,
}

POSITION_MAP = {
    "P": "P", "C": "C", "1B": "1B", "2B": "2B", "3B": "3B",
    "SS": "SS", "LF": "LF", "CF": "CF", "RF": "RF",
    "OF": "OF", "DH": "DH", "IF": "IF", "UT": "UT",
    "SP": "P", "RP": "P", "CL": "P",
}


@router.get("/mlb/teams/{abbr}/roster")
async def mlb_team_roster(
    abbr: str,
    year: int = Query(2026),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the current active roster for an MLB team, grouped by position category.
    Fetches live from the MLB Stats API roster endpoint for the current season.
    Merges in our DB stats where available.
    """
    api_team_id = MLB_API_TEAM_IDS.get(abbr.upper())
    if not api_team_id:
        raise HTTPException(status_code=404, detail=f"Unknown team: {abbr}")

    # Fetch active roster from MLB Stats API
    roster_url = f"https://statsapi.mlb.com/api/v1/teams/{api_team_id}/roster?rosterType=fullSeason"
    roster_data = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(roster_url, params={"season": year})
            if resp.status_code == 200:
                roster_data = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MLB API error: {e}")

    if not roster_data or "roster" not in roster_data:
        return {"team_abbr": abbr.upper(), "year": year, "roster": {
            "starting_pitchers": [], "relief_pitchers": [], "catchers": [],
            "infielders": [], "outfielders": [], "designated_hitters": [],
        }}

    # Get team DB id for stat lookups
    team_r = await db.execute(
        text("SELECT id FROM mlb.teams WHERE abbreviation = :abbr"),
        {"abbr": abbr.upper()}
    )
    team_db_id = team_r.scalar_one_or_none()
    if not team_db_id:
        raise HTTPException(status_code=404, detail=f"Team {abbr} not found in DB")

    # Get current season DB id
    season_r = await db.execute(
        text("SELECT id FROM mlb.seasons WHERE year = :year"),
        {"year": year}
    )
    season_id = season_r.scalar_one_or_none()

    # Look up our player stats in bulk for this team/season
    pitching_stats_by_mlb_id = {}
    batting_stats_by_mlb_id = {}

    if season_id:
        ps_r = await db.execute(
            text("""
                SELECT p.mlb_id, ps.games_played, ps.games_started,
                       ps.wins, ps.losses, ps.saves, ps.era, ps.whip,
                       ps.innings_pitched, ps.strikeouts AS k,
                       ps.base_on_balls AS bb
                FROM mlb.pitching_stats ps
                JOIN mlb.players p ON p.id = ps.player_id
                WHERE ps.team_id = :team_id AND ps.season_id = :season_id
            """),
            {"team_id": team_db_id, "season_id": season_id}
        )
        for row in ps_r.mappings().all():
            d = dict(row)
            mlb_id = d.pop("mlb_id")
            for k, v in d.items():
                if isinstance(v, decimal.Decimal):
                    d[k] = float(v)
            pitching_stats_by_mlb_id[mlb_id] = d

        bs_r = await db.execute(
            text("""
                SELECT p.mlb_id, bs.games_played, bs.avg, bs.obp, bs.slg,
                       bs.ops, bs.home_runs, bs.runs_batted_in AS rbi,
                       bs.stolen_bases AS sb, bs.at_bats, bs.hits,
                       bs.runs, bs.base_on_balls AS bb, bs.strikeouts
                FROM mlb.batting_stats bs
                JOIN mlb.players p ON p.id = bs.player_id
                WHERE bs.team_id = :team_id AND bs.season_id = :season_id
            """),
            {"team_id": team_db_id, "season_id": season_id}
        )
        for row in bs_r.mappings().all():
            d = dict(row)
            mlb_id = d.pop("mlb_id")
            for k, v in d.items():
                if isinstance(v, decimal.Decimal):
                    d[k] = float(v)
            batting_stats_by_mlb_id[mlb_id] = d

    # Build roster from API data, merging our stats
    players = []
    for entry in roster_data["roster"]:
        person = entry.get("person", {})
        person_id = person.get("id")
        full_name = person.get("fullName", "")
        pos_abbr = entry.get("position", {}).get("abbreviation", "")
        pos = POSITION_MAP.get(pos_abbr, pos_abbr)
        jersey = _safe_int(entry.get("jerseyNumber"))
        status = entry.get("status", {}).get("code", "")

        # Look up our DB record for the player
        db_r = await db.execute(
            text("SELECT id, headshot_url FROM mlb.players WHERE mlb_id = :mid"),
            {"mid": person_id}
        )
        db_player = db_r.mappings().one_or_none()
        db_id = db_player["id"] if db_player else None
        headshot = db_player["headshot_url"] if db_player else None

        # Get stats for this player
        ps = pitching_stats_by_mlb_id.get(person_id, {})
        bs = batting_stats_by_mlb_id.get(person_id, {})

        is_pitcher = pos == "P"
        category = "pitcher" if is_pitcher else "batter"

        player = {
            "id": db_id,
            "mlb_id": person_id,
            "name": full_name,
            "position": pos,
            "jersey_number": jersey,
            "status": status,
            "headshot_url": headshot,
            "has_current_stats": 1 if (ps or bs) else 0,
            # Pitching stats
            "games_played": ps.get("games_played", 0),
            "games_started": ps.get("games_started", 0),
            "wins": ps.get("wins"),
            "losses": ps.get("losses"),
            "saves": ps.get("saves"),
            "era": ps.get("era"),
            "whip": ps.get("whip"),
            "innings_pitched": ps.get("innings_pitched"),
            "k_pitch": ps.get("k"),
            "bb_pitch": ps.get("bb"),
            # Batting stats
            "avg": bs.get("avg"),
            "obp": bs.get("obp"),
            "slg": bs.get("slg"),
            "ops": bs.get("ops"),
            "home_runs": bs.get("home_runs"),
            "rbi": bs.get("rbi"),
            "sb": bs.get("sb"),
            "at_bats": bs.get("at_bats"),
            "hits": bs.get("hits"),
            # Backward compat for existing frontend
            "k": ps.get("k"),
            "bb": ps.get("bb"),
            "bats": None,
            "throws": None,
            "years_exp": None,
            "height": None,
            "weight": None,
            "college": None,
        }
        players.append(player)

    # Group by position categories
    grouped = {
        "starting_pitchers": [],
        "relief_pitchers": [],
        "catchers": [],
        "infielders": [],
        "outfielders": [],
        "designated_hitters": [],
    }

    for p in players:
        pos = p["position"]
        if pos == "P":
            gs = p.get("games_started") or 0
            gp = p.get("games_played") or 0
            if gs > 0 and (gp == 0 or gs / gp >= 0.3):
                grouped["starting_pitchers"].append(p)
            else:
                grouped["relief_pitchers"].append(p)
        elif pos == "C":
            grouped["catchers"].append(p)
        elif pos in ("1B", "2B", "3B", "SS"):
            grouped["infielders"].append(p)
        elif pos in ("LF", "CF", "RF", "OF"):
            grouped["outfielders"].append(p)
        elif pos == "DH":
            grouped["designated_hitters"].append(p)
        else:
            grouped["infielders"].append(p)

    return {"team_abbr": abbr.upper(), "year": year, "roster": grouped}

    result = await db.execute(sql, {"abbr": abbr.upper(), "year": year})
    rows = result.mappings().all()

    # Convert Decimal to float for JSON
    players = []
    for r in rows:
        p = dict(r)
        for k, v in p.items():
            if isinstance(v, decimal.Decimal):
                p[k] = float(v) if v is not None else None
        players.append(p)

    # Group by position categories
    # If a player has current stats, we can determine SP vs RP
    # If they don't have current stats, they're historical/stashed
    grouped = {
        "starting_pitchers": [],
        "relief_pitchers": [],
        "catchers": [],
        "infielders": [],
        "outfielders": [],
        "designated_hitters": [],
    }

    for p in players:
        pos = p["position"]
        has_stats = p.get("has_current_stats", 0)
        if p["category"] == "pitcher":
            if has_stats:
                gs = p.get("games_started") or 0
                gp = p.get("games_played") or 0
                if gs > 0 and (gp == 0 or gs / gp >= 0.3):
                    grouped["starting_pitchers"].append(p)
                else:
                    grouped["relief_pitchers"].append(p)
            else:
                grouped["relief_pitchers"].append(p)
        elif pos == "C":
            grouped["catchers"].append(p)
        elif pos in ("1B", "2B", "3B", "SS"):
            grouped["infielders"].append(p)
        elif pos in ("LF", "CF", "RF", "OF"):
            grouped["outfielders"].append(p)
        elif pos == "DH":
            grouped["designated_hitters"].append(p)
        else:
            grouped["infielders"].append(p)

    return {"team_abbr": abbr.upper(), "year": year, "roster": grouped}


# ── MLB Player Search/Lookup ────────────────────────────────────────


class MLBPlayerOut(BaseModel):
    id: int
    name: str
    position: str
    team_abbr: str | None = None
    team_name: str | None = None
    status: str | None = None
    jersey_number: int | None = None
    height: int | None = None
    weight: int | None = None
    college: str | None = None
    bats: str | None = None
    throws: str | None = None
    years_exp: int | None = None
    birth_date: str | None = None
    headshot_url: str | None = None

    model_config = {"from_attributes": True}


@router.get("/mlb/players")
async def list_mlb_players(
    position: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """List MLB players, optionally filtered by position."""
    conditions = ["1=1"]
    params = {"limit": limit, "offset": offset}
    if position:
        conditions.append("p.position = :position")
        params["position"] = position.upper()
    if search:
        conditions.append("p.name ILIKE :search")
        params["search"] = f"%{search}%"
    where_clause = " AND ".join(conditions)

    sql = f"""
        SELECT p.id, p.name, p.position, p.status,
               p.jersey_number, p.height, p.weight, p.college,
               p.bats, p.throws, p.years_exp,
               p.birth_date::text AS birth_date,
               p.headshot_url,
               t.abbreviation AS team_abbr,
               t.name AS team_name
        FROM mlb.players p
        LEFT JOIN mlb.teams t ON t.id = p.team_id
        WHERE {where_clause}
        ORDER BY p.name
        LIMIT :limit OFFSET :offset
    """
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


@router.get("/mlb/players/{player_id}")
async def get_mlb_player(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get a single MLB player."""
    sql = text("""
        SELECT p.id, p.name, p.position, p.status,
               p.jersey_number, p.height, p.weight, p.college,
               p.bats, p.throws, p.years_exp,
               p.birth_date::text AS birth_date,
               p.headshot_url,
               t.abbreviation AS team_abbr,
               t.name AS team_name
        FROM mlb.players p
        LEFT JOIN mlb.teams t ON t.id = p.team_id
        WHERE p.id = :player_id
    """)
    result = await db.execute(sql, {"player_id": player_id})
    row = result.mappings().one_or_none()
    if not row:
        return {"error": "Player not found"}
    return dict(row)


@router.get("/mlb/players/{player_id}/profile")
async def get_mlb_player_profile(
    player_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Get a full MLB player profile with batting and pitching stats by season.
    Compatible with the frontend player page at /[sport]/players/[id].
    """
    # Get player basics
    sql = text("""
        SELECT p.id, p.name, p.position, p.mlb_id,
               p.jersey_number, p.bats, p.throws,
               p.height, p.weight, p.college,
               p.birth_date::text AS birth_date,
               p.years_exp, p.status, p.headshot_url,
               t.abbreviation AS team_abbr,
               t.name AS team_name
        FROM mlb.players p
        LEFT JOIN mlb.teams t ON t.id = p.team_id
        WHERE p.id = :player_id
    """)
    result = await db.execute(sql, {"player_id": player_id})
    player = result.mappings().one_or_none()
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")

    p = dict(player)

    # Get career batting stats by season
    bat_sql = text("""
        SELECT s.year, bs.games_played, bs.plate_appearances, bs.at_bats,
               bs.runs, bs.hits, bs.doubles, bs.triples, bs.home_runs,
               bs.runs_batted_in, bs.stolen_bases, bs.caught_stealing,
               bs.base_on_balls, bs.strikeouts, bs.hit_by_pitch,
               bs.avg, bs.obp, bs.slg, bs.ops
        FROM mlb.batting_stats bs
        JOIN mlb.seasons s ON s.id = bs.season_id
        WHERE bs.player_id = :player_id
        ORDER BY s.year DESC
    """)
    bat_r = await db.execute(bat_sql, {"player_id": player_id})
    batting_by_season = [dict(r) for r in bat_r.mappings().all()]
    for bs in batting_by_season:
        for k, v in bs.items():
            if isinstance(v, decimal.Decimal):
                bs[k] = float(v) if v is not None else None

    # Get career pitching stats by season
    pitch_sql = text("""
        SELECT s.year, ps.games_played, ps.games_started, ps.games_finished,
               ps.wins, ps.losses, ps.saves, ps.blown_saves, ps.holds,
               ps.innings_pitched, ps.hits, ps.runs, ps.earned_runs,
               ps.home_runs, ps.base_on_balls, ps.strikeouts,
               ps.era, ps.whip, ps.avg AS opp_avg,
               ps.strikeouts_per_9, ps.walks_per_9, ps.strikeout_walk_ratio,
               ps.complete_games, ps.shutouts
        FROM mlb.pitching_stats ps
        JOIN mlb.seasons s ON s.id = ps.season_id
        WHERE ps.player_id = :player_id
        ORDER BY s.year DESC
    """)
    pitch_r = await db.execute(pitch_sql, {"player_id": player_id})
    pitching_by_season = [dict(r) for r in pitch_r.mappings().all()]
    for ps in pitching_by_season:
        for k, v in ps.items():
            if isinstance(v, decimal.Decimal):
                ps[k] = float(v) if v is not None else None

    # Compute career totals
    career_batting = {}
    if batting_by_season:
        career_batting = {
            "games": sum(b.get("games_played") or 0 for b in batting_by_season),
            "plate_appearances": sum(b.get("plate_appearances") or 0 for b in batting_by_season),
            "at_bats": sum(b.get("at_bats") or 0 for b in batting_by_season),
            "runs": sum(b.get("runs") or 0 for b in batting_by_season),
            "hits": sum(b.get("hits") or 0 for b in batting_by_season),
            "doubles": sum(b.get("doubles") or 0 for b in batting_by_season),
            "triples": sum(b.get("triples") or 0 for b in batting_by_season),
            "home_runs": sum(b.get("home_runs") or 0 for b in batting_by_season),
            "rbi": sum(b.get("runs_batted_in") or 0 for b in batting_by_season),
            "stolen_bases": sum(b.get("stolen_bases") or 0 for b in batting_by_season),
            "walks": sum(b.get("base_on_balls") or 0 for b in batting_by_season),
            "strikeouts_batting": sum(b.get("strikeouts") or 0 for b in batting_by_season),
        }

    career_pitching = {}
    if pitching_by_season:
        ip_total = sum(ps.get("innings_pitched") or 0 for ps in pitching_by_season)
        er_total = sum(ps.get("earned_runs") or 0 for ps in pitching_by_season)
        h_total = sum(ps.get("hits") or 0 for ps in pitching_by_season)
        bb_p_total = sum(ps.get("base_on_balls") or 0 for ps in pitching_by_season)
        k_total = sum(ps.get("strikeouts") or 0 for ps in pitching_by_season)
        career_pitching = {
            "games": sum(ps.get("games_played") or 0 for ps in pitching_by_season),
            "games_started": sum(ps.get("games_started") or 0 for ps in pitching_by_season),
            "wins": sum(ps.get("wins") or 0 for ps in pitching_by_season),
            "losses": sum(ps.get("losses") or 0 for ps in pitching_by_season),
            "saves": sum(ps.get("saves") or 0 for ps in pitching_by_season),
            "innings_pitched": ip_total,
            "hits": h_total,
            "earned_runs": er_total,
            "strikeouts_pitching": k_total,
            "walks": bb_p_total,
            "era": round((er_total / ip_total) * 9, 2) if ip_total > 0 else None,
            "whip": round((bb_p_total + h_total) / ip_total, 2) if ip_total > 0 else None,
        }

    # Build seasons list for frontend
    # Frontend expects "recent_seasons" with year and games
    recent_seasons = []
    for bs in batting_by_season:
        recent_seasons.append({
            "year": bs["year"],
            "games": bs.get("games_played") or 0,
            "avg": bs.get("avg") or 0,
            "obp": bs.get("obp") or 0,
            "slg": bs.get("slg") or 0,
            "ops": bs.get("ops") or 0,
            "home_runs": bs.get("home_runs") or 0,
            "runs_batted_in": bs.get("runs_batted_in") or 0,
            "stolen_bases": bs.get("stolen_bases") or 0,
            "hits": bs.get("hits") or 0,
            "at_bats": bs.get("at_bats") or 0,
            "walks": bs.get("base_on_balls") or 0,
            "strikeouts": bs.get("strikeouts") or 0,
        })
    for ps in pitching_by_season:
        # Merge pitching stats into existing season entry if batter, or create new
        existing = next((s for s in recent_seasons if s["year"] == ps["year"]), None)
        if existing:
            existing["era"] = ps.get("era")
            existing["whip"] = ps.get("whip")
            existing["wins"] = ps.get("wins")
            existing["losses"] = ps.get("losses")
            existing["saves"] = ps.get("saves")
            existing["innings_pitched"] = ps.get("innings_pitched")
            existing["games_started"] = ps.get("games_started")
            existing["strikeouts_pitching"] = ps.get("strikeouts")
        else:
            recent_seasons.append({
                "year": ps["year"],
                "games": ps.get("games_played") or 0,
                "games_started": ps.get("games_started"),
                "era": ps.get("era"),
                "whip": ps.get("whip"),
                "wins": ps.get("wins"),
                "losses": ps.get("losses"),
                "saves": ps.get("saves"),
                "innings_pitched": ps.get("innings_pitched"),
                "strikeouts_pitching": ps.get("strikeouts"),
                "walks": ps.get("base_on_balls"),
            })

    # Sort by year descending
    recent_seasons.sort(key=lambda x: x["year"], reverse=True)

    # Determine primary position
    is_pitcher = p.get("position") == "P"

    return {
        "id": p["id"],
        "name": p["name"],
        "position": p["position"],
        "team_abbr": p.get("team_abbr"),
        "team_name": p.get("team_name"),
        "college": p.get("college"),
        "height": p.get("height"),
        "weight": p.get("weight"),
        "birth_date": p.get("birth_date"),
        "years_exp": p.get("years_exp"),
        "status": p.get("status"),
        "jersey_number": p.get("jersey_number"),
        "headshot_url": p.get("headshot_url"),
        "bats": p.get("bats"),
        "throws": p.get("throws"),
        "batting_stats": batting_by_season,
        "pitching_stats": pitching_by_season,
        "career_batting": career_batting or None,
        "career_pitching": career_pitching or None,
        "recent_seasons": recent_seasons,
        "first_year": min((s["year"] for s in recent_seasons), default=None),
        "last_year": max((s["year"] for s in recent_seasons), default=None),
        "stats": career_batting or career_pitching or None,
        "injuries": [],
        "transactions": [],
    }


# ── Stat sort whitelists ─────────────────────────────────────────────
BATTING_SORT_COLS = {
    "games_played", "plate_appearances", "at_bats", "runs", "hits",
    "doubles", "triples", "home_runs", "runs_batted_in",
    "stolen_bases", "base_on_balls", "strikeouts",
    "avg", "obp", "slg", "ops", "babip",
    "total_bases", "hit_by_pitch",
}

PITCHING_SORT_COLS = {
    "games_played", "games_started", "wins", "losses", "saves",
    "innings_pitched", "hits", "runs", "earned_runs", "home_runs",
    "base_on_balls", "strikeouts", "era", "whip",
    "avg", "obp", "slg", "ops",
    "strikeouts_per_9", "walks_per_9", "strikeout_walk_ratio",
    "blown_saves", "holds", "complete_games", "shutouts",
}

# ── Compatibility: frontend expects /mlb/stats/players & /mlb/stats/teams ──


@router.get("/mlb/stats/players")
async def mlb_stats_players(
    year: int = Query(...),
    position: str = Query("ALL"),
    sort: str = Query("home_runs"),
    order: str = Query("desc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return batting stats (frontend-compatible). Serves as the /mlb/stats/players endpoint."""
    # Map frontend sort keys to MLB batting column names
    sort_map = {
        "pass_yards": "home_runs",
        "pass_tds": "home_runs",
        "rush_yards": "runs",
        "receiving_yards": "hits",
        "fantasy_points_ppr": "runs_batted_in",
    }
    mapped_sort = sort_map.get(sort, sort)
    if mapped_sort not in BATTING_SORT_COLS:
        mapped_sort = "home_runs"
    direction = "DESC" if order == "desc" else "ASC"

    if position.upper() != "ALL":
        pos_filter = "AND p.position = :position"
    else:
        pos_filter = ""

    sql = f"""
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        t.abbreviation AS team_abbr,
        bs.games_played AS games,
        bs.at_bats AS pass_attempts,
        bs.hits AS pass_completions,
        bs.home_runs AS pass_tds,
        bs.strikeouts AS pass_int,
        bs.runs AS rush_yards,
        bs.runs_batted_in AS receiving_yards,
        bs.stolen_bases AS rush_tds,
        bs.doubles AS targets,
        bs.triples AS receptions,
        bs.base_on_balls AS fumbles,
        bs.avg AS comp_pct,
        bs.obp AS yards_per_att,
        bs.slg AS passer_rating,
        bs.ops AS yards_per_rec,
        bs.total_bases AS fantasy_points_ppr,
        bs.plate_appearances AS fantasy_points_std,
        bs.at_bats AS fantasy_points_half
    FROM mlb.batting_stats bs
    JOIN mlb.players p ON p.id = bs.player_id
    LEFT JOIN mlb.teams t ON t.id = bs.team_id
    JOIN mlb.seasons s ON s.id = bs.season_id
    WHERE s.year = :year AND bs.games_played >= :min_games {pos_filter}
    ORDER BY bs.{mapped_sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """
    params = {"year": year, "limit": limit, "offset": offset, "min_games": min_games}
    if position.upper() != "ALL":
        params["position"] = position.upper()

    result = await db.execute(text(sql), params)
    rows = result.mappings().all()

    count_sql = f"""
    SELECT COUNT(*) AS total FROM mlb.batting_stats bs
    JOIN mlb.seasons s ON s.id = bs.season_id
    JOIN mlb.players p ON p.id = bs.player_id
    WHERE s.year = :year AND bs.games_played >= :min_games {pos_filter}
    """
    count_result = await db.execute(text(count_sql), params)
    total = count_result.scalar()

    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset, "sort": mapped_sort, "order": order}


@router.get("/mlb/stats/teams")
async def mlb_stats_teams(
    year: int = Query(...),
    sort: str = Query("wins"),
    order: str = Query("desc"),
    limit: int = Query(30, ge=1, le=30),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    """Return MLB team standings for a season."""
    direction = "DESC" if order == "desc" else "ASC"

    # Aggregate team stats from games
    sql = f"""
    SELECT
        t.id AS team_id,
        t.name AS team_name,
        t.abbreviation AS team_abbr,
        t.league AS conference,
        t.division,
        COUNT(g.id) AS games,
        SUM(CASE WHEN g.status::text = 'final' AND (
            (g.home_team_id = t.id AND g.home_score > g.away_score)
            OR (g.away_team_id = t.id AND g.away_score > g.home_score)
        ) THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN g.status::text = 'final' AND (
            (g.home_team_id = t.id AND g.home_score < g.away_score)
            OR (g.away_team_id = t.id AND g.away_score < g.home_score)
        ) THEN 1 ELSE 0 END) AS losses,
        0 AS ties,
        SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS points_for,
        SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS points_against
    FROM mlb.teams t
    LEFT JOIN mlb.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
        AND g.season_id = (SELECT id FROM mlb.seasons WHERE year = :year)
        AND g.status::text = 'final'
    GROUP BY t.id, t.name, t.abbreviation, t.league, t.division
    ORDER BY wins {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """

    result = await db.execute(text(sql), {"year": year, "limit": limit, "offset": offset})
    rows = result.mappings().all()

    return {"data": [dict(r) for r in rows], "total": len(rows), "limit": limit, "offset": offset, "sort": sort, "order": order}


# ── MLB Batting Stats ─────────────────────────────────────────────────


@router.get("/mlb/stats/batting")
async def mlb_batting_stats(
    year: int = Query(...),
    sort: str = Query("home_runs"),
    order: str = Query("desc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in BATTING_SORT_COLS:
        sort = "home_runs"
    direction = "DESC" if order == "desc" else "ASC"

    sql = f"""
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        p.mlb_id,
        t.abbreviation AS team_abbr,
        bs.games_played,
        bs.plate_appearances,
        bs.at_bats,
        bs.runs,
        bs.hits,
        bs.doubles,
        bs.triples,
        bs.home_runs,
        bs.runs_batted_in,
        bs.stolen_bases,
        bs.caught_stealing,
        bs.base_on_balls,
        bs.intentional_walks,
        bs.strikeouts,
        bs.hit_by_pitch,
        bs.sacrifice_flies,
        bs.avg,
        bs.obp,
        bs.slg,
        bs.ops,
        bs.babip,
        bs.total_bases,
        bs.at_bats_per_home_run,
        bs.ground_into_double_play
    FROM mlb.batting_stats bs
    JOIN mlb.players p ON p.id = bs.player_id
    LEFT JOIN mlb.teams t ON t.id = bs.team_id
    JOIN mlb.seasons s ON s.id = bs.season_id
    WHERE s.year = :year AND bs.games_played >= :min_games
    ORDER BY bs.{sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(sql),
        {"year": year, "limit": limit, "offset": offset, "min_games": min_games},
    )
    rows = result.mappings().all()

    # Total count
    count_sql = """
    SELECT COUNT(*) AS total FROM mlb.batting_stats bs
    JOIN mlb.seasons s ON s.id = bs.season_id
    WHERE s.year = :year AND bs.games_played >= :min_games
    """
    count_result = await db.execute(text(count_sql), {"year": year, "min_games": min_games})
    total = count_result.scalar()

    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset, "sort": sort, "order": order}


# ── Pitching Stats ────────────────────────────────────────────────────


@router.get("/mlb/stats/pitching")
async def mlb_pitching_stats(
    year: int = Query(...),
    sort: str = Query("era"),
    order: str = Query("asc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in PITCHING_SORT_COLS:
        sort = "era"
    direction = "DESC" if order == "desc" else "ASC"

    sql = f"""
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        p.mlb_id,
        t.abbreviation AS team_abbr,
        ps.games_played,
        ps.games_started,
        ps.wins,
        ps.losses,
        ps.saves,
        ps.blown_saves,
        ps.holds,
        ps.innings_pitched,
        ps.hits,
        ps.runs,
        ps.earned_runs,
        ps.home_runs,
        ps.base_on_balls,
        ps.strikeouts,
        ps.era,
        ps.whip,
        ps.avg,
        ps.obp,
        ps.slg,
        ps.ops,
        ps.strikeouts_per_9,
        ps.walks_per_9,
        ps.strikeout_walk_ratio,
        ps.complete_games,
        ps.shutouts,
        ps.batters_faced,
        ps.hit_by_pitch,
        ps.wild_pitches
    FROM mlb.pitching_stats ps
    JOIN mlb.players p ON p.id = ps.player_id
    LEFT JOIN mlb.teams t ON t.id = ps.team_id
    JOIN mlb.seasons s ON s.id = ps.season_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    ORDER BY ps.{sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(sql),
        {"year": year, "limit": limit, "offset": offset, "min_games": min_games},
    )
    rows = result.mappings().all()

    count_sql = """
    SELECT COUNT(*) AS total FROM mlb.pitching_stats ps
    JOIN mlb.seasons s ON s.id = ps.season_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    """
    count_result = await db.execute(text(count_sql), {"year": year, "min_games": min_games})
    total = count_result.scalar()

    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset, "sort": sort, "order": order}


# ── MLB Game Schedule ─────────────────────────────────────────────────


@router.get("/mlb/games")
async def mlb_games(
    year: int = Query(...),
    date: str = Query(None),
    team_abbr: str = Query(None),
    db: AsyncSession = Depends(get_db),
):
    # Build query from our DB
    filters = ["s.year = :year"]
    params = {"year": year}

    if date:
        filters.append("(g.date AT TIME ZONE 'America/Chicago')::date = :date")
        params["date"] = datetime.date.fromisoformat(date)

    if team_abbr:
        filters.append("(ht.abbreviation = :team_abbr OR at.abbreviation = :team_abbr)")
        params["team_abbr"] = team_abbr.upper()

    where_clause = " AND ".join(filters)

    sql = f"""
    SELECT
        g.id,
        g.mlb_game_id,
        g.game_type,
        g.date,
        g.status::text,
        ht.abbreviation AS home_team,
        at.abbreviation AS away_team,
        g.home_score,
        g.away_score,
        g.venue,
        g.scheduled_innings,
        g.actual_innings,
        g.attendance,
        g.duration_minutes,
        g.day_night,
        g.home_pitcher_name,
        g.away_pitcher_name,
        c.closing_spread AS spread,
        c.closing_ou AS over_under,
        c.closing_home_ml AS home_moneyline,
        c.closing_away_ml AS away_moneyline,
        c.opening_spread,
        c.opening_ou AS opening_total,
        c.opening_home_ml AS opening_home_moneyline,
        c.opening_away_ml AS opening_away_moneyline,
        gp.predicted_margin,
        gp.ml_result AS pred_ml_result,
        gp.run_line_result AS pred_rl_result,
        gp.ou_result AS pred_ou_result
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    JOIN mlb.seasons s ON s.id = g.season_id
    LEFT JOIN mlb.betting_lines_consolidated c ON c.game_id = g.id
    LEFT JOIN mlb.game_predictions gp ON gp.game_id = g.id AND gp.source = 'api'
    WHERE {where_clause}
    ORDER BY g.date ASC
    """
    DECIMAL_FIELDS = ["spread", "over_under", "home_moneyline", "away_moneyline",
                      "opening_spread", "opening_total",
                      "opening_home_moneyline", "opening_away_moneyline"]
    result = await db.execute(text(sql), params)
    rows = result.mappings().all()
    games_list = []
    for r in rows:
        g = dict(r)
        # Cast Decimal to float for JSON serialization
        for field in DECIMAL_FIELDS:
            if isinstance(g.get(field), decimal.Decimal):
                g[field] = float(g[field])
        games_list.append(g)

    # If a date was requested, overlay live data from MLB API
    if date:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date}"
                )
                if resp.status_code == 200:
                    live_data = resp.json()
                    # Build map of mlb_game_id -> live status/score
                    live_by_id = {}
                    for date_entry in live_data.get("dates", []):
                        for game in date_entry.get("games", []):
                            pk = game.get("gamePk")
                            if not pk:
                                continue
                            status_code = game.get("status", {}).get("codedGameState", "S")
                            if status_code in ("F", "O", "FT"):
                                status = "FINAL"
                            elif status_code in ("I", "M", "MA"):
                                status = "IN_PROGRESS"
                            elif status_code == "PD":
                                status = "POSTPONED"
                            elif status_code in ("C", "CA"):
                                status = "CANCELLED"
                            else:
                                status = "SCHEDULED"

                            away = game.get("teams", {}).get("away", {})
                            home = game.get("teams", {}).get("home", {})
                            away_score = away.get("score")
                            home_score = home.get("score")

                            live_by_id[pk] = {
                                "status": status,
                                "away_score": away_score if away_score is not None else 0,
                                "home_score": home_score if home_score is not None else 0,
                            }

                    # Overlay live data onto our DB games
                    for g in games_list:
                        live = live_by_id.get(g["mlb_game_id"])
                        if live and live["status"] in ("FINAL", "IN_PROGRESS"):
                            g["status"] = live["status"]
                            g["away_score"] = live["away_score"]
                            g["home_score"] = live["home_score"]
        except Exception:
            pass

    # Attach lineups to each game
    try:
        from app.models.mlb import MLBLineup
        for g in games_list:
            lineup_r = await db.execute(
                select(MLBLineup).where(MLBLineup.game_id == g["id"]).order_by(MLBLineup.team_side, MLBLineup.batting_order)
            )
            lineup_rows = lineup_r.scalars().all()
            g["lineups"] = {
                "home": [{"order": l.batting_order, "name": l.player_name, "position": l.position} for l in lineup_rows if l.team_side == "home"],
                "away": [{"order": l.batting_order, "name": l.player_name, "position": l.position} for l in lineup_rows if l.team_side == "away"],
            }
    except Exception:
        pass

    return games_list


@router.get("/mlb/games/{game_id}/boxscore")
async def mlb_game_boxscore(
    game_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return game details + proxy the MLB Stats API boxscore."""
    # Get game from our DB
    sql = """
    SELECT
        g.id,
        g.mlb_game_id,
        g.game_type,
        g.date,
        g.status::text,
        ht.abbreviation AS home_team,
        ht.name AS home_team_name,
        at.abbreviation AS away_team,
        at.name AS away_team_name,
        g.home_score,
        g.away_score,
        g.venue,
        g.scheduled_innings,
        g.actual_innings,
        g.attendance,
        g.duration_minutes,
        g.day_night
    FROM mlb.games g
    JOIN mlb.teams ht ON ht.id = g.home_team_id
    JOIN mlb.teams at ON at.id = g.away_team_id
    WHERE g.id = :game_id
    """
    result = await db.execute(text(sql), {"game_id": game_id})
    game = result.mappings().one_or_none()
    if not game:
        raise HTTPException(status_code=404, detail="Game not found")

    game_dict = dict(game)

    # Fetch live feed + boxscore from MLB API if we have an mlb_game_id
    boxscore_data = None
    live_data = None
    if game_dict["mlb_game_id"]:
        gid = game_dict["mlb_game_id"]
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Live feed — has game status + linescore
                resp = await client.get(
                    f"https://statsapi.mlb.com/api/v1.1/game/{gid}/feed/live"
                )
                if resp.status_code == 200:
                    live_data = resp.json()
                # Boxscore — has player stats
                resp2 = await client.get(
                    f"https://statsapi.mlb.com/api/v1/game/{gid}/boxscore"
                )
                if resp2.status_code == 200:
                    boxscore_data = resp2.json()
        except Exception:
            pass

    # Sync DB status/scores from live feed
    if live_data:
        ls = live_data.get("liveData", {}).get("linescore", {})
        game_status = live_data.get("gameData", {}).get("status", {})
        detailed_state = (game_status.get("detailedState", "") or "").lower()
        abstract_state = (game_status.get("abstractGameState", "") or "").lower()

        # Get scores from linescore
        away_score = ls.get("teams", {}).get("away", {}).get("runs")
        home_score = ls.get("teams", {}).get("home", {}).get("runs")

        # Map status
        new_status = game_dict["status"].upper()
        if abstract_state == "final" or "final" in detailed_state:
            new_status = "FINAL"
        elif abstract_state == "live" or "in progress" in detailed_state:
            new_status = "IN_PROGRESS"
        elif abstract_state == "preview" or "pre-game" in detailed_state or "scheduled" in detailed_state:
            new_status = "SCHEDULED"
        elif "postponed" in detailed_state:
            new_status = "POSTPONED"
        elif "cancelled" in detailed_state:
            new_status = "CANCELLED"

        # Update DB if changed
        if (new_status != game_dict["status"].upper()) or \
           (away_score is not None and away_score != game_dict.get("away_score")) or \
           (home_score is not None and home_score != game_dict.get("home_score")):
            update_parts = []
            update_params = {"game_id": game_id}

            if new_status != game_dict["status"].upper():
                update_parts.append("status = :new_status")
                update_params["new_status"] = new_status

            if away_score is not None and away_score != game_dict.get("away_score"):
                update_parts.append("away_score = :away_score")
                update_params["away_score"] = away_score

            if home_score is not None and home_score != game_dict.get("home_score"):
                update_parts.append("home_score = :home_score")
                update_params["home_score"] = home_score

            if update_parts:
                update_sql = f"UPDATE mlb.games SET {', '.join(update_parts)} WHERE id = :game_id"
                await db.execute(text(update_sql), update_params)
                await db.commit()
                game_dict["status"] = new_status.lower()
                if away_score is not None:
                    game_dict["away_score"] = away_score
                if home_score is not None:
                    game_dict["home_score"] = home_score

    # Include betting lines if available (from consolidated table for best coverage)
    lines = []
    r = await db.execute(
        text("""
            SELECT
                'consolidated' as source,
                'consolidated' as sportsbook,
                closing_spread AS spread,
                closing_ou AS over_under,
                closing_home_ml AS home_moneyline,
                closing_away_ml AS away_moneyline,
                closing_spread_home_odds AS spread_home_odds,
                closing_spread_away_odds AS spread_away_odds,
                closing_over_odds AS over_odds,
                closing_under_odds AS under_odds,
                closing_home_implied_probability AS home_implied_probability,
                closing_away_implied_probability AS away_implied_probability,
                opening_spread,
                opening_ou AS opening_total,
                opening_home_ml AS opening_home_moneyline,
                opening_away_ml AS opening_away_moneyline
            FROM mlb.betting_lines_consolidated
            WHERE game_id = :game_id
            LIMIT 1
        """), {"game_id": game_id}
    )
    row = r.fetchone()
    if row:
        lines.append(dict(row._mapping))
        # Cast Decimals to float for JSON
        for key in ['spread', 'over_under', 'home_moneyline', 'away_moneyline',
                     'spread_home_odds', 'spread_away_odds',
                     'over_odds', 'under_odds',
                     'home_implied_probability', 'away_implied_probability',
                     'opening_spread', 'opening_total',
                     'opening_home_moneyline', 'opening_away_moneyline']:
            if key in lines[0] and isinstance(lines[0][key], decimal.Decimal):
                lines[0][key] = float(lines[0][key])

    # Pick card: from game_predictions table for completed games,
    # or generate fresh for upcoming games
    pick_card = None
    is_completed = game_dict["status"].lower() == "final"
    is_scheduled = game_dict["status"].lower() in ("scheduled", "in_progress")

    # Always try cached prediction first (fast DB read)
    from app.handicapping.calibrate_confidence import calibrate as _cal
    from app.models.mlb import MLBGamePrediction

    pred_r = await db.execute(
        select(MLBGamePrediction).where(
            MLBGamePrediction.game_id == game_id,
            MLBGamePrediction.source == "api",
        )
    )
    pred = pred_r.scalar_one_or_none()

    # If the game is completed but the prediction's actual scores don't match, update them
    if pred and is_completed:
        db_home = game_dict.get("home_score")
        db_away = game_dict.get("away_score")
        if db_home is not None and db_away is not None and (db_home + db_away > 0):
            actual_changed = pred.actual_home_runs != db_home or pred.actual_away_runs != db_away

            if actual_changed:
                pred.actual_home_runs = db_home
                pred.actual_away_runs = db_away
                pred.actual_total = db_home + db_away
                pred.actual_margin = db_home - db_away

            # Recompute ML pick from predicted margin (direction comes from predicted score, not ML model)
            predicted_margin = pred.predicted_margin or 0
            if not pred.ml_pick:
                # Derive ML pick from actual predicted score, not ATS margin alone
                # (home/away runs combine both ATS + OU models and are the displayed values)
                if pred.predicted_home_runs is not None and pred.predicted_away_runs is not None:
                    pred.ml_pick = "home" if pred.predicted_home_runs > pred.predicted_away_runs else "away"
                else:
                    pred.ml_pick = "home" if predicted_margin > 0 else "away"

            # Recompute all results from actual scores
            actual_margin = db_home - db_away
            predicted_total = pred.predicted_total or 0

            if actual_changed and lines and len(lines) > 0:
                sp = lines[0].get("spread")
                vegas_ou = lines[0].get("over_under")
                hm_ml = lines[0].get("home_moneyline")
                am_ml = lines[0].get("away_moneyline")
                if hm_ml is not None and am_ml is not None:
                    home_is_fav = hm_ml < am_ml
                    signed_home_spread = -sp if home_is_fav else sp
                else:
                    signed_home_spread = sp or 0

                if pred.run_line_pick and sp is not None:
                    spr = float(sp)
                    # Parse pick string "{TEAM_ABB} {+/-VALUE}" to determine
                    # whether the pick is on the home or away team
                    pick_parts = pred.run_line_pick.split()
                    if len(pick_parts) >= 2:
                        try:
                            pick_line_val = float(pick_parts[-1])
                        except (ValueError, TypeError):
                            pick_line_val = None
                    else:
                        pick_line_val = None
                    # home_run_line = spr, away_run_line = -spr
                    rl_pick_is_home = (
                        pick_line_val is not None
                        and abs(pick_line_val - spr) < 0.01
                    )

                    if abs(actual_margin) < 0.5:
                        pred.run_line_result = "Push"
                    else:
                        home_covers = (actual_margin + spr) > 0
                        predicted_home_covers = (predicted_margin + spr) > 0
                        if rl_pick_is_home:
                            pred.run_line_result = (
                                "Win" if predicted_home_covers == home_covers else "Loss"
                            )
                        else:
                            # Away covers when home doesn't cover
                            pred.run_line_result = (
                                "Win" if predicted_home_covers != home_covers else "Loss"
                            )
                if pred.ou_pick and vegas_ou is not None:
                    actual_total = db_home + db_away
                    if abs(actual_total - float(vegas_ou)) < 0.5:
                        pred.ou_result = "Push"
                    elif (predicted_total > float(vegas_ou)) == (actual_total > float(vegas_ou)):
                        pred.ou_result = "Win"
                    else:
                        pred.ou_result = "Loss"

            # ML result always recomputed when game is completed
            if actual_margin != 0:
                pred.ml_result = "Win" if (predicted_margin > 0) == (actual_margin > 0) else "Loss"

            if actual_changed:
                try:
                    await db.flush()
                    await db.commit()
                except Exception:
                    await db.rollback()

    # Compute the signed run line for each team from the consolidated line
    # Consolidated table follows standard convention: negative = home favored
    # home_run_line = spread (already signed for home team)
    # away_run_line = -spread (flip for away team)
    home_run_line = None
    away_run_line = None
    if lines and len(lines) > 0:
        raw_spread = lines[0].get("spread")
        hm = lines[0].get("home_moneyline")
        am = lines[0].get("away_moneyline")
        if raw_spread is not None and hm is not None and am is not None:
            home_run_line = raw_spread
            away_run_line = -raw_spread

    # Pre-compute enriched handicapper data (can return None for teams without history)
    situ_data = (await MLBSituationalAnalyzer(db).analyze_game(game_id)) if lines else None
    split_data = (await MLBSplitAnalyzer(db).analyze_game(game_id)) if lines else None

    if pred:
        pick_card = {
            "game_id": pred.game_id,
            "predictions": {
                "home_runs": pred.predicted_home_runs,
                "away_runs": pred.predicted_away_runs,
                "total": pred.predicted_total,
                "margin": pred.predicted_margin,
            },
            "actual": {
                "home_runs": pred.actual_home_runs,
                "away_runs": pred.actual_away_runs,
                "total": pred.actual_total,
                "margin": pred.actual_margin,
            },
            "results": {
                "run_line": pred.run_line_result,
                "over_under": pred.ou_result,
                "moneyline": pred.ml_result,
            },
            "confidence": {
                "rl": round(_cal(pred.rl_conf or 0, "ats", sport="mlb"), 2),
                "ou": round(_cal(pred.ou_conf or 0, "ou", sport="mlb"), 2),
                "ml": round(_cal(pred.ml_conf or 0, "ml", sport="mlb"), 2),
                "margin": round(_cal(pred.rl_conf or 0, "overall", sport="mlb"), 2),
                "raw": {
                    "rl": round(pred.rl_conf or 0, 2),
                    "ou": round(pred.ou_conf or 0, 2),
                    "ml": round(pred.ml_conf or 0, 2),
                },
            },
            "expected_value": {
                "rl": round(pred.ats_ev or 0, 2),
                "ou": round(pred.ou_ev or 0, 2),
                "ml": round(pred.ml_ev or 0, 2),
            },
            "picks": {
                "run_line": pred.run_line_pick,
                "over_under": pred.ou_pick,
                "moneyline": pred.ml_pick,
            },
            "lines": {
                "run_line": raw_spread,
                "home_run_line": home_run_line,
                "away_run_line": away_run_line,
                "over_under": lines[0].get("over_under") if lines else None,
                "home_moneyline": lines[0].get("home_moneyline") if lines else None,
                "away_moneyline": lines[0].get("away_moneyline") if lines else None,
            } if lines else None,
            # Enriched handicapper stats
            "team_stats": {
                "home": json.loads(pred.home_stats_json) if pred.home_stats_json else None,
                "away": json.loads(pred.away_stats_json) if pred.away_stats_json else None,
            },
            "situational": situ_data.to_dict() if situ_data else None,
            "splits": split_data.to_dict() if split_data else None,
            # Raw JSON columns from DB
            "stats_json": {
                "home_stats": json.loads(pred.home_stats_json) if pred.home_stats_json else None,
                "away_stats": json.loads(pred.away_stats_json) if pred.away_stats_json else None,
                "situational": json.loads(pred.situational_json) if pred.situational_json else None,
                "splits": json.loads(pred.splits_json) if pred.splits_json else None,
                "features": json.loads(pred.features_json) if pred.features_json else None,
            },
        }

    # Extract live linescore info for display
    linescore_info = None
    if live_data:
        ls = live_data.get("liveData", {}).get("linescore", {})
        if ls:
            linescore_info = {
                "currentInning": ls.get("currentInning"),
                "currentInningOrdinal": ls.get("currentInningOrdinal"),
                "inningState": ls.get("inningState"),
                "isTopInning": ls.get("isTopInning"),
                "inningHalf": ls.get("inningHalf"),
                "scheduledInnings": ls.get("scheduledInnings"),
                "defense": ls.get("defense"),
                "offense": ls.get("offense"),
                "balls": ls.get("balls"),
                "strikes": ls.get("strikes"),
                "outs": ls.get("outs"),
                "teams": ls.get("teams"),
            }

    # Attach lineups from mlb.lineups table with season stats
    lineups = None
    if game_dict.get("status", "").lower() in ("scheduled", "pregame", "in_progress"):
        from app.models.mlb import MLBLineup, MLBSeason, MLBBattingStats, MLBPitchingStats, MLBPlayer
        lineup_r = await db.execute(
            select(MLBLineup).where(MLBLineup.game_id == game_id).order_by(MLBLineup.team_side, MLBLineup.batting_order)
        )
        lineup_rows = lineup_r.scalars().all()
        if lineup_rows:
            gd = game_dict.get("date")
            season_year = datetime.datetime.now().year
            if gd and isinstance(gd, datetime.datetime):
                season_year = gd.year

            sr = await db.execute(select(MLBSeason).where(MLBSeason.year == season_year))
            season = sr.scalar_one_or_none()

            # Pre-fetch all player stats for this season into a name-keyed dict
            batting_by_name = {}
            pitching_by_name = {}
            if season:
                from sqlalchemy.orm import joinedload
                bat_r = await db.execute(
                    select(MLBBattingStats).options(joinedload(MLBBattingStats.player)).where(
                        MLBBattingStats.season_id == season.id
                    )
                )
                for bs in bat_r.unique().scalars().all():
                    pname = bs.player.name.lower() if bs.player else ""
                    batting_by_name[pname] = {
                        "avg": bs.avg, "obp": bs.obp, "slg": bs.slg, "ops": bs.ops,
                        "hr": bs.home_runs, "rbi": bs.runs_batted_in,
                    }

                pitch_r = await db.execute(
                    select(MLBPitchingStats).options(joinedload(MLBPitchingStats.player)).where(
                        MLBPitchingStats.season_id == season.id
                    )
                )
                for ps in pitch_r.unique().scalars().all():
                    pname = ps.player.name.lower() if ps.player else ""
                    pitching_by_name[pname] = {
                        "era": ps.era, "whip": ps.whip,
                        "w": ps.wins, "l": ps.losses,
                        "so": ps.strikeouts, "ip": ps.innings_pitched,
                    }

            def _entry(r) -> dict:
                info = {"order": r.batting_order, "name": r.player_name, "position": r.position}
                nkey = r.player_name.lower()
                if r.position == "SP":
                    info["stats"] = pitching_by_name.get(nkey, {})
                else:
                    info["stats"] = batting_by_name.get(nkey, {})
                return info

            lineups = {
                "home": [_entry(r) for r in lineup_rows if r.team_side == "home"],
                "away": [_entry(r) for r in lineup_rows if r.team_side == "away"],
            }

    return {
        "game": game_dict,
        "boxscore": boxscore_data,
        "linescore": linescore_info,
        "betting_lines": lines,
        "pick_card": pick_card,
        "splits": split_data.to_dict() if split_data else None,
        "lineups": lineups,
    }


# ── Injured List ────────────────────────────────────────────────────


@router.get("/mlb/injured-list")
async def mlb_injured_list(
    team_abbr: str | None = Query(None, description="Filter by team abbreviation"),
    db: AsyncSession = Depends(get_db),
):
    """Return all MLB players on the Injured List.

    Status is synced from the MLB Stats API 40-man roster during the
    mlb-stats-refresh task. Players with IL/Injured status codes are shown.
    """
    conditions = ["p.active = 1"]
    params = {}

    # Status codes indicating injury/IL
    conditions.append("""(
        p.status ILIKE '%Injured%' OR p.status ILIKE '%IL%'
        OR p.status ILIKE '%Day-to-Day%' OR p.status ILIKE '%Restricted%'
        OR p.status ILIKE '%Suspended%'
    )""")

    if team_abbr:
        conditions.append("t.abbreviation = :team_abbr")
        params["team_abbr"] = team_abbr.upper()

    where = " AND ".join(conditions)

    sql = f"""
        SELECT
            p.id, p.mlb_id, p.name, p.position, p.status,
            p.jersey_number, p.headshot_url,
            t.abbreviation AS team_abbr
        FROM mlb.players p
        JOIN mlb.teams t ON t.id = p.team_id
        WHERE {where}
        ORDER BY t.abbreviation, p.name
    """

    result = await db.execute(text(sql), params)
    rows = result.mappings().all()
    return [dict(r) for r in rows]


@router.get("/mlb/feature-definitions")
async def mlb_feature_definitions():
    """Return all MLB model feature definitions with display names and descriptions.

    These map feature slugs (used in features_json column of game_predictions)
    to human-readable descriptions. The frontend uses them for popover tooltips
    in the prediction stats modal.
    """
    from app.handicapping.mlb.data_loader import (
        FEATURES_CATALOG,
        COMPUTED_FEATURES_CATALOG,
        DISPLAY_NAMES,
    )

    merged: dict[str, str] = {}
    merged.update(FEATURES_CATALOG)
    merged.update(COMPUTED_FEATURES_CATALOG)

    features: list[dict] = []
    for slug, description in sorted(merged.items()):
        features.append({
            "slug": slug,
            "display_name": DISPLAY_NAMES.get(slug, slug.replace("_", " ").title()),
            "description": description,
        })

    return features
