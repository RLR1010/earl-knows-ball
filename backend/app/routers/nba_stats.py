"""NBA stats endpoints — player stats, team standings, game schedules."""

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
import datetime
import time
import threading
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import get_db
from sqlalchemy.orm import joinedload
from sqlalchemy import func
from app.routers.games import _records_as_of_batch
from app.routers.auth import get_optional_user
from app.core.security import user_is_premium
from app.models import User
from zoneinfo import ZoneInfo

_CHI_TZ = ZoneInfo("America/Chicago")


def _chi_today_iso() -> str:
    return datetime.datetime.now(_CHI_TZ).date().isoformat()


def _is_past_game(game) -> bool:
    """True when the game's date is before today (America/Chicago)."""
    d = getattr(game, "date", None)
    if d is None:
        return False
    try:
        dd = d.astimezone(_CHI_TZ).date() if getattr(d, "tzinfo", None) else d.date()
    except Exception:
        return False
    return dd < datetime.datetime.now(_CHI_TZ).date()
from app.models.nba import NBAGame, NBAPlayer, NBAPlayerSeasonStats, NBASeason, NBATeam

router = APIRouter()


# ---------------------------------------------------------------------------
# TTL cache for the schedule GET /nba/games response.
# The frontend schedule page auto-polls /nba/games every ~30s so completed-game
# pick results stay fresh. This 30s TTL collapses the polling bursts into
# roughly one DB query per worker per 30s, and we send Cache-Control: max-age=30.
# Per-worker on purpose (same trade-off as the MLB games/box cache).
# ---------------------------------------------------------------------------
_NBA_GAMES_TTL = 30
_nba_games_cache_lock = threading.Lock()
_nba_games_cache = {}  # key: (year, date, team_abbr) -> (expires_at_monotonic, list[dict])


def _nba_games_cached(key) -> list | None:
    with _nba_games_cache_lock:
        item = _nba_games_cache.get(key)
        if item and item[0] >= time.monotonic():
            return item[1]
        return None


def _nba_games_store(key, payload: list):
    with _nba_games_cache_lock:
        _nba_games_cache[key] = (time.monotonic() + _NBA_GAMES_TTL, payload)
        if len(_nba_games_cache) > 256:
            for k in sorted(_nba_games_cache, key=lambda k: _nba_games_cache[k][0])[:128]:
                del _nba_games_cache[k]

SORT_COLS = {
    "points", "points_per_game", "assists", "assists_per_game",
    "rebounds", "rebounds_per_game", "steals", "blocks",
    "field_goal_pct", "three_point_pct", "free_throw_pct",
    "games_played", "minutes_played", "turnovers",
    "efficiency",
}


@router.get("/nba/stats/players")
async def nba_player_stats(
    year: int = Query(...),
    sort: str = Query("points_per_game"),
    order: str = Query("desc"),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    min_games: int = Query(1, ge=0),
    db: AsyncSession = Depends(get_db),
):
    if sort not in SORT_COLS:
        sort = "points_per_game"
    direction = "DESC" if order == "desc" else "ASC"

    sql = f"""
    WITH latest_team AS (
        SELECT DISTINCT ON (pg.player_id) pg.player_id, gg.season_id, pg.team_id
        FROM nba.player_game_stats pg
        JOIN nba.games gg ON gg.id = pg.game_id
        WHERE gg.season_id = (SELECT id FROM nba.seasons WHERE year = :year)
        ORDER BY pg.player_id, gg.date DESC
    )
    SELECT
        p.id AS player_id,
        p.name AS player_name,
        p.position,
        t.abbreviation AS team_abbr,
        ps.games_played,
        ps.games_started,
        ps.minutes_played,
        ps.points,
        ps.points_per_game,
        ps.field_goals_made,
        ps.field_goals_attempted,
        ps.field_goal_pct,
        ps.three_points_made,
        ps.three_points_attempted,
        ps.three_point_pct,
        ps.free_throws_made,
        ps.free_throws_attempted,
        ps.free_throw_pct,
        ps.rebounds,
        ps.offensive_rebounds,
        ps.defensive_rebounds,
        ps.rebounds_per_game,
        ps.assists,
        ps.assists_per_game,
        ps.turnovers,
        ps.steals,
        ps.blocks,
        ps.personal_fouls,
        ps.plus_minus,
        ps.efficiency
    FROM nba.player_season_stats ps
    JOIN nba.players p ON p.id = ps.player_id
    JOIN nba.seasons s ON s.id = ps.season_id
    LEFT JOIN latest_team lt ON lt.player_id = p.id AND lt.season_id = ps.season_id
    LEFT JOIN nba.teams t ON t.id = lt.team_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    ORDER BY ps.{sort} {direction} NULLS LAST
    LIMIT :limit OFFSET :offset
    """
    result = await db.execute(
        text(sql), {"year": year, "limit": limit, "offset": offset, "min_games": min_games}
    )
    rows = result.mappings().all()

    count_sql = """
    SELECT COUNT(*) FROM nba.player_season_stats ps
    JOIN nba.seasons s ON s.id = ps.season_id
    WHERE s.year = :year AND ps.games_played >= :min_games
    """
    total = (await db.execute(text(count_sql), {"year": year, "min_games": min_games})).scalar()

    return {"data": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset, "sort": sort, "order": order}


@router.get("/nba/stats/teams")
async def nba_team_stats(
    year: int = Query(...),
    sort: str = Query("wins"),
    order: str = Query("desc"),
    db: AsyncSession = Depends(get_db),
):
    direction = "DESC" if order == "desc" else "ASC"
    sql = f"""
    SELECT
        t.id AS team_id,
        t.name AS team_name,
        t.abbreviation AS team_abbr,
        t.conference,
        t.division,
        COUNT(g.id) AS games,
        SUM(CASE WHEN g.status::text = 'FINAL' AND (
            (g.home_team_id = t.id AND g.home_score > g.away_score)
            OR (g.away_team_id = t.id AND g.away_score > g.home_score)
        ) THEN 1 ELSE 0 END) AS wins,
        SUM(CASE WHEN g.status::text = 'FINAL' AND (
            (g.home_team_id = t.id AND g.home_score < g.away_score)
            OR (g.away_team_id = t.id AND g.away_score < g.home_score)
        ) THEN 1 ELSE 0 END) AS losses,
        SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score
                 WHEN g.away_team_id = t.id THEN g.away_score END) AS points_for,
        SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score
                 WHEN g.away_team_id = t.id THEN g.home_score END) AS points_against
    FROM nba.teams t
    LEFT JOIN nba.games g ON (g.home_team_id = t.id OR g.away_team_id = t.id)
        AND g.season_id = (SELECT id FROM nba.seasons WHERE year = :year)
        AND g.status::text = 'FINAL'
    GROUP BY t.id, t.name, t.abbreviation, t.conference, t.division
    ORDER BY wins {direction} NULLS LAST
    """
    result = await db.execute(text(sql), {"year": year})
    return {"data": [dict(r) for r in result.mappings().all()]}


# ── NBA Season Leaders (Yahoo-style category cards) ────────────────────
# group -> [(key, title, value_expr, unit)]
_NBA_LEADER_GROUPS = [
    ("Scoring", [
        ("points_per_game", "Points/G", "MAX(ps.points_per_game)", "one"),
        ("points", "Points", "MAX(ps.points)", "int"),
        ("field_goal_pct", "FG%", "MAX(ps.field_goal_pct)", "pct"),
    ]),
    ("Rebounds", [
        ("rebounds_per_game", "Reb/G", "MAX(ps.rebounds_per_game)", "one"),
        ("rebounds", "Rebounds", "MAX(ps.rebounds)", "int"),
    ]),
    ("Defense", [
        ("steals_per_game", "Stl/G", "MAX(ps.steals)::numeric / GREATEST(MAX(ps.games_played), 1)", "one"),
        ("blocks_per_game", "Blk/G", "MAX(ps.blocks)::numeric / GREATEST(MAX(ps.games_played), 1)", "one"),
    ]),
    ("Miscellaneous", [
        ("assists_per_game", "Ast/G", "MAX(ps.assists_per_game)", "one"),
        ("turnovers_per_game", "TO/G", "MAX(ps.turnovers)::numeric / GREATEST(MAX(ps.games_played), 1)", "one"),
        ("efficiency", "EFF", "MAX(ps.efficiency)", "one"),
    ]),
]


async def _nba_leader_rows(db, year: int, expr: str, having: str, limit: int, team: str | None = None):
    team_clause = " AND lt.team_id = (SELECT id FROM nba.teams WHERE abbreviation = :team)" if team else ""
    sql = f"""
        WITH latest_team AS (
            SELECT DISTINCT ON (pg.player_id) pg.player_id, gg.season_id, pg.team_id
            FROM nba.player_game_stats pg
            JOIN nba.games gg ON gg.id = pg.game_id
            WHERE gg.season_id = (SELECT id FROM nba.seasons WHERE year = :year)
            ORDER BY pg.player_id, gg.date DESC
        )
        SELECT p.id AS player_id,
               p.name AS player_name,
               p.position,
               t.abbreviation AS team_abbr,
               ({expr}) AS value
        FROM nba.player_season_stats ps
        JOIN nba.seasons s ON s.id = ps.season_id
        JOIN nba.players p ON p.id = ps.player_id
        LEFT JOIN latest_team lt ON lt.player_id = p.id AND lt.season_id = ps.season_id
        LEFT JOIN nba.teams t ON t.id = lt.team_id
        WHERE s.year = :year{team_clause}
        GROUP BY p.id, p.name, p.position, t.abbreviation
        HAVING {having}
        ORDER BY value DESC NULLS LAST
        LIMIT :limit
    """
    result = await db.execute(text(sql), {"year": year, "limit": limit, "team": team})
    return [dict(r) for r in result.mappings().all()]


@router.get("/nba/stats/leaders")
async def nba_stat_leaders(
    year: int = Query(...),
    limit: int = Query(5, ge=1, le=25),
    group: str = Query("all"),
    team: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    wanted = None if group.lower() in ("all", "") else group.lower()
    groups = []
    for gtitle, cards in _NBA_LEADER_GROUPS:
        if wanted and gtitle.lower() != wanted:
            continue
        out_cards = []
        for key, title, expr, unit in cards:
            rows = await _nba_leader_rows(db, year, expr, "MAX(ps.games_played) >= 5", limit, team)
            out_cards.append({
                "key": key,
                "title": title,
                "unit": unit,
                "rows": [{"rank": i + 1, **r} for i, r in enumerate(rows)],
            })
        groups.append({"title": gtitle, "cards": out_cards})
    return {"year": year, "limit": limit, "groups": groups}


# ── Team season stats (Yahoo-style team page section) ──────────────────

_NBA_TEAM_METRICS = [
    ("Offense", [
        ("Points/G", "ppg", True, "one"),
        ("FG%", "fg_pct", True, "pct"),
        ("3P%", "fg3_pct", True, "pct"),
        ("FT%", "ft_pct", True, "pct"),
        ("Rebounds/G", "rpg", True, "one"),
        ("Assists/G", "apg", True, "one"),
    ]),
    ("Defense", [
        ("Opp Points/G", "opp_ppg", False, "one"),
        ("Steals/G", "spg", True, "one"),
        ("Blocks/G", "bpg", True, "one"),
        ("Turnovers/G", "tpg", False, "one"),
    ]),
]


@router.get("/nba/stats/team/{abbr}")
async def nba_stat_team(abbr: str, year: int = Query(...), db: AsyncSession = Depends(get_db)):
    abbr = abbr.upper()
    agg = [dict(r) for r in (await db.execute(text("""
        WITH latest_team AS (
            SELECT DISTINCT ON (pg.player_id) pg.player_id, gg.season_id, pg.team_id
            FROM nba.player_game_stats pg
            JOIN nba.games gg ON gg.id = pg.game_id
            WHERE gg.season_id = (SELECT id FROM nba.seasons WHERE year = :year)
            ORDER BY pg.player_id, gg.date DESC
        )
        SELECT t.abbreviation AS abbr,
               COALESCE(SUM(ps.points), 0) AS pts,
               COALESCE(SUM(ps.field_goals_made), 0) AS fgm,
               COALESCE(SUM(ps.field_goals_attempted), 0) AS fga,
               COALESCE(SUM(ps.three_points_made), 0) AS tpm,
               COALESCE(SUM(ps.three_points_attempted), 0) AS tpa,
               COALESCE(SUM(ps.free_throws_made), 0) AS ftm,
               COALESCE(SUM(ps.free_throws_attempted), 0) AS fta,
               COALESCE(SUM(ps.rebounds), 0) AS reb,
               COALESCE(SUM(ps.assists), 0) AS ast,
               COALESCE(SUM(ps.steals), 0) AS stl,
               COALESCE(SUM(ps.blocks), 0) AS blk,
               COALESCE(SUM(ps.turnovers), 0) AS tov
        FROM nba.player_season_stats ps
        JOIN nba.seasons s ON s.id = ps.season_id
        JOIN latest_team lt ON lt.player_id = ps.player_id AND lt.season_id = ps.season_id
        JOIN nba.teams t ON t.id = lt.team_id
        WHERE s.year = :year
        GROUP BY t.abbreviation
    """), {"year": year})).mappings().all()]
    rec = [dict(r) for r in (await db.execute(text("""
        SELECT t.abbreviation AS abbr, COUNT(*) AS g,
               SUM(CASE WHEN g.home_team_id = t.id THEN g.home_score ELSE g.away_score END) AS pf,
               SUM(CASE WHEN g.home_team_id = t.id THEN g.away_score ELSE g.home_score END) AS pa,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score > g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score > g.home_score) THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN (g.home_team_id = t.id AND g.home_score < g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score < g.home_score) THEN 1 ELSE 0 END) AS losses
        FROM nba.games g
        JOIN nba.teams t ON t.id = g.home_team_id OR t.id = g.away_team_id
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE s.year = :year AND g.status::text = 'FINAL' AND g.game_type::text = 'REG'
        GROUP BY t.abbreviation
    """), {"year": year})).mappings().all()]

    by = {r["abbr"]: dict(r) for r in agg}
    for r in rec:
        by.setdefault(r["abbr"], {"abbr": r["abbr"]}).update(r)

    teams = []
    for a, d in by.items():
        g = d.get("g") or 0
        if not g:
            continue
        d["ppg"] = round((d.get("pts") or 0) / g, 1)
        d["opp_ppg"] = round((d.get("pa") or 0) / g, 1)
        d["rpg"] = round((d.get("reb") or 0) / g, 1)
        d["apg"] = round((d.get("ast") or 0) / g, 1)
        d["spg"] = round((d.get("stl") or 0) / g, 1)
        d["bpg"] = round((d.get("blk") or 0) / g, 1)
        d["tpg"] = round((d.get("tov") or 0) / g, 1)
        for pct, mk, ma in (("fg_pct", "fgm", "fga"), ("fg3_pct", "tpm", "tpa"), ("ft_pct", "ftm", "fta")):
            d[pct] = round((d.get(mk) or 0) / (d.get(ma) or 1), 3)
        d.setdefault("ranks", {})
        teams.append(d)

    for _title, metrics in _NBA_TEAM_METRICS:
        for _label, key, higher, _unit in metrics:
            order = sorted(range(len(teams)), key=lambda i: (teams[i].get(key) is None,
                                                             -(teams[i].get(key) or 0) if higher else (teams[i].get(key) or 0)))
            for rank, i in enumerate(order, 1):
                teams[i]["ranks"][key] = rank

    tgt = next((t for t in teams if t["abbr"] == abbr), None)
    if not tgt:
        return {"year": year, "found": False, "team": {"abbr": abbr}}

    sections = []
    for title, metrics in _NBA_TEAM_METRICS:
        rows = []
        for label, key, _h, unit in metrics:
            rows.append({"label": label, "value": tgt.get(key), "rank": tgt["ranks"].get(key), "unit": unit})
        sections.append({"title": title, "rows": rows})

    leader_groups = []
    for gtitle, cards in _NBA_LEADER_GROUPS:
        out_cards = []
        for key, title, expr, unit in cards:
            rows = await _nba_leader_rows(db, year, expr, "MAX(ps.games_played) >= 1", 5, abbr)
            out_cards.append({"key": key, "title": title, "unit": unit,
                              "rows": [{"rank": i + 1, **r} for i, r in enumerate(rows)]})
        leader_groups.append({"title": gtitle, "cards": out_cards})

    return {
        "year": year,
        "found": True,
        "team": {"abbr": abbr},
        "record": {"wins": tgt.get("wins") or 0, "losses": tgt.get("losses") or 0,
                   "ties": 0, "games": tgt.get("g") or 0},
        "sections": sections,
        "leader_groups": leader_groups,
    }


@router.get("/nba/stats/seasons")
async def nba_stat_seasons(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        text(
            """
            SELECT DISTINCT s.year
            FROM nba.player_season_stats ps
            JOIN nba.seasons s ON s.id = ps.season_id
            ORDER BY s.year DESC
            """
        )
    )
    return {"years": [r[0] for r in result.fetchall()]}


@router.get("/nba/players")
async def nba_players(
    position: str = Query(None),
    search: str = Query(None),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    conditions = ["1=1"]
    params = {"limit": limit}
    if position:
        conditions.append("p.position = :position")
        params["position"] = position.upper()
    if search:
        conditions.append("p.name ILIKE :search")
        params["search"] = f"%{search}%"
    where = " AND ".join(conditions)
    sql = f"""
    SELECT p.id, p.name, p.position, p.nba_id, p.jersey_number,
           p.height, p.weight, p.college, p.years_exp, p.status,
           p.headshot_url, t.abbreviation AS team_abbr, t.name AS team_name
    FROM nba.players p
    LEFT JOIN nba.teams t ON t.id = p.team_id
    WHERE {where}
    ORDER BY p.name LIMIT :limit
    """
    result = await db.execute(text(sql), params)
    return [dict(r) for r in result.mappings().all()]


async def _team_record_as_of(db: AsyncSession, team_id: int, game_date, season_id: int):
    """Return {'wins': w, 'losses': l} for a team in games played BEFORE game_date
    (i.e. the record going into the game), within the given season.
    The game being displayed is excluded by filtering on date < game_date."""
    if not team_id or not game_date:
        return {"wins": 0, "losses": 0}
    row = (
        await db.execute(
            text(
                """
                SELECT
                  COALESCE(SUM(CASE WHEN (home_team_id=:tid AND home_score>away_score)
                                       OR (away_team_id=:tid AND away_score>home_score)
                                  THEN 1 ELSE 0 END), 0) AS wins,
                  COALESCE(SUM(CASE WHEN (home_team_id=:tid AND home_score<away_score)
                                       OR (away_team_id=:tid AND away_score<home_score)
                                  THEN 1 ELSE 0 END), 0) AS losses
                FROM nba.games
                WHERE id <= 1000000
                  AND season_id = :sid
                  AND (home_team_id=:tid OR away_team_id=:tid)
                  AND date < :gdate
                  AND home_score IS NOT NULL AND away_score IS NOT NULL
                """
            )
            .params(tid=team_id, sid=season_id, gdate=game_date)
        )
    ).one()
    return {"wins": int(row.wins or 0), "losses": int(row.losses or 0)}


@router.get("/nba/games/{game_id}/boxscore")
async def nba_game_boxscore(
    game_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Return detailed NBA game boxscore with home/away stats.

    Box score is public; betting lines are premium and stripped for non-premium.
    """
    result = await db.execute(
        select(NBAGame)
        .options(joinedload(NBAGame.home_team), joinedload(NBAGame.away_team))
        .where(NBAGame.id == game_id)
    )
    game = result.unique().scalar_one_or_none()
    if not game:
        return {"error": "Game not found"}

    from datetime import datetime

    def _get_val(val):
        if val is not None:
            return float(val)
        return None

    home_stats = {
        "team": game.home_team.abbreviation if game.home_team else None,
        "team_id": game.home_team.id if game.home_team else None,
        "score": _get_val(game.home_score),
        "field_goals_made": _get_val(game.home_field_goals_made),
        "field_goals_attempted": _get_val(game.home_field_goals_attempted),
        "three_points_made": _get_val(game.home_three_points_made),
        "three_points_attempted": _get_val(game.home_three_points_attempted),
        "free_throws_made": _get_val(game.home_free_throws_made),
        "free_throws_attempted": _get_val(game.home_free_throws_attempted),
        "rebounds": _get_val(game.home_rebounds),
        "assists": _get_val(game.home_assists),
    }
    away_stats = {
        "team": game.away_team.abbreviation if game.away_team else None,
        "team_id": game.away_team.id if game.away_team else None,
        "score": _get_val(game.away_score),
        "field_goals_made": _get_val(game.away_field_goals_made),
        "field_goals_attempted": _get_val(game.away_field_goals_attempted),
        "three_points_made": _get_val(game.away_three_points_made),
        "three_points_attempted": _get_val(game.away_three_points_attempted),
        "free_throws_made": _get_val(game.away_free_throws_made),
        "free_throws_attempted": _get_val(game.away_free_throws_attempted),
        "rebounds": _get_val(game.away_rebounds),
        "assists": _get_val(game.away_assists),
    }

    # Compute percentages
    for side in (home_stats, away_stats):
        fga = side.get("field_goals_attempted")
        fgm = side.get("field_goals_made")
        side["field_goal_pct"] = round(fgm / fga, 3) if fga and fga > 0 else None

        tpa = side.get("three_points_attempted")
        tpm = side.get("three_points_made")
        side["three_point_pct"] = round(tpm / tpa, 3) if tpa and tpa > 0 else None

        fta = side.get("free_throws_attempted")
        ftm = side.get("free_throws_made")
        side["free_throw_pct"] = round(ftm / fta, 3) if fta and fta > 0 else None

    # Include player stats if available
    from app.models.nba.player_game_stats import NBAPlayerGameStats
    player_rows = await db.execute(
        select(NBAPlayerGameStats)
        .where(NBAPlayerGameStats.game_id == game_id)
        .order_by(NBAPlayerGameStats.points.desc().nullslast())
    )
    player_stats_list = []
    for ps in player_rows.scalars().all():
        # Fetch player name separately
        p_row = await db.execute(select(NBAPlayer.name).where(NBAPlayer.id == ps.player_id))
        p_name = p_row.scalar_one_or_none()
        player_stats_list.append({
            "player_id": ps.player_id,
            "name": p_name,
            "team_id": ps.team_id,
            "minutes": ps.minutes,
            "field_goals_made": ps.field_goals_made,
            "field_goals_attempted": ps.field_goals_attempted,
            "field_goal_pct": ps.field_goal_pct,
            "three_pointers_made": ps.three_pointers_made,
            "three_pointers_attempted": ps.three_pointers_attempted,
            "three_pointer_pct": ps.three_pointer_pct,
            "free_throws_made": ps.free_throws_made,
            "free_throws_attempted": ps.free_throws_attempted,
            "free_throw_pct": ps.free_throw_pct,
            "rebounds_offensive": ps.rebounds_offensive,
            "rebounds_defensive": ps.rebounds_defensive,
            "rebounds_total": ps.rebounds_total,
            "assists": ps.assists,
            "steals": ps.steals,
            "blocks": ps.blocks,
            "turnovers": ps.turnovers,
            "fouls_personal": ps.fouls_personal,
            "points": ps.points,
            "plus_minus": ps.plus_minus,
        })

    # Fetch betting lines
    betting_lines = None
    try:
        from sqlalchemy import text as sa_text
        bl_result = await db.execute(
            sa_text("""
                SELECT
                    opening_spread, opening_ou,
                    closing_spread, closing_ou,
                    closing_home_ml, closing_away_ml,
                    closing_spread_home_odds, closing_spread_away_odds,
                    closing_over_odds, closing_under_odds,
                    closing_home_implied_probability, closing_away_implied_probability
                FROM nba.betting_lines_consolidated
                WHERE game_id = :game_id
            """),
            {"game_id": game_id},
        )
        row = bl_result.one_or_none()
        if row:
            betting_lines = {
                "opening_spread": _get_val(row.opening_spread),
                "opening_ou": _get_val(row.opening_ou),
                "closing_spread": _get_val(row.closing_spread),
                "closing_ou": _get_val(row.closing_ou),
                "closing_home_ml": _get_val(row.closing_home_ml),
                "closing_away_ml": _get_val(row.closing_away_ml),
                "closing_spread_home_odds": _get_val(row.closing_spread_home_odds),
                "closing_spread_away_odds": _get_val(row.closing_spread_away_odds),
                "closing_over_odds": _get_val(row.closing_over_odds),
                "closing_under_odds": _get_val(row.closing_under_odds),
                "closing_home_implied_probability": _get_val(row.closing_home_implied_probability),
                "closing_away_implied_probability": _get_val(row.closing_away_implied_probability),
            }
    except Exception:
        pass

    # Team records as of this game (wins-losses going into the game).
    try:
        season_id = game.season_id
        home_record = await _team_record_as_of(
            db, game.home_team_id, game.date, season_id
        )
        away_record = await _team_record_as_of(
            db, game.away_team_id, game.date, season_id
        )
        home_stats["record"] = home_record
        away_stats["record"] = away_record
    except Exception:
        home_stats["record"] = {"wins": 0, "losses": 0}
        away_stats["record"] = {"wins": 0, "losses": 0}

    # Premium gate (field-level): box score is public, betting lines are premium —
    # except for games played yesterday or earlier (historical = public).
    if not user_is_premium(user) and not _is_past_game(game):
        betting_lines = None

    return {
        "game_id": game.id,
        "nba_game_id": game.nba_game_id,
        "date": game.date.isoformat() if game.date else None,
        "status": game.status.value if game.status else "scheduled",
        "game_type": game.game_type,
        "venue": game.venue,
        "attendance": game.attendance,
        "home": home_stats,
        "away": away_stats,
        "players": player_stats_list,
        "betting_lines": betting_lines,
    }


@router.get("/nba/games/{game_id}/prop-bets")
async def nba_game_prop_bets(
    game_id: int,
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    """Return all player prop bets stored for an NBA game, or empty list if none.

    Premium gate: player props are a premium feature.
    """
    if not user_is_premium(user):
        raise HTTPException(status_code=403, detail="Premium subscription required")
    result = await db.execute(
        text(
            """
            SELECT player_name, team_id, prop_type, line, odds,
                   direction
            FROM nba.player_daily_props
            WHERE game_id = :game_id AND bookmaker = 'DraftKings'
            ORDER BY player_name, prop_type
            """
        ),
        {"game_id": str(game_id)},
    )
    return [dict(r) for r in result.mappings().all()]


@router.get("/nba/seasons")
async def nba_seasons(db: AsyncSession = Depends(get_db)):
    """Return years that have NBA games in the database (2022-23 season onward)."""
    result = await db.execute(
        text("""
            SELECT DISTINCT s.year FROM nba.seasons s
            INNER JOIN nba.games g ON g.season_id = s.id
            WHERE s.year >= 2022
            ORDER BY s.year DESC
        """)
    )
    return [row[0] for row in result.fetchall()]


@router.get("/nba/games")
async def nba_games(
    year: int = Query(...),
    date: str = Query(None),
    team_abbr: str = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User | None = Depends(get_optional_user),
):
    # 30s TTL response cache (per-worker) keyed by query params.
    # Include premium status in the key so the field-level strip below cannot
    # leak picks into an anonymous cache slot (or wipe picks for premium users).
    cache_key = (year, date, (team_abbr or "").upper(), bool(user_is_premium(user)))
    cached = _nba_games_cached(cache_key)
    if cached is not None:
        return JSONResponse(content=cached, headers={"Cache-Control": "public, max-age=30"})

    filters = ["s.year = :year", "g.game_type != 'PRE'"]
    params = {"year": year}

    if date:
        filters.append("(g.date AT TIME ZONE 'America/Chicago')::date = :date")
        params["date"] = datetime.date.fromisoformat(date)

    if team_abbr:
        filters.append("(ht.abbreviation = :team_abbr OR at.abbreviation = :team_abbr)")
        params["team_abbr"] = team_abbr.upper()

    where_clause = " AND ".join(filters)

    sql = f"""
    SELECT g.id, g.nba_game_id, g.date, (g.date AT TIME ZONE 'America/Chicago')::date AS game_date,
           g.game_type, g.home_score, g.away_score, g.home_team_id, g.away_team_id,
           g.status::text, g.venue, g.attendance,
           ht.abbreviation AS home_team, at.abbreviation AS away_team,
           blc.closing_spread AS spread, blc.closing_ou AS over_under,
           blc.closing_home_ml AS home_moneyline, blc.closing_away_ml AS away_moneyline,
           CASE WHEN gp.spread_pick IS NOT NULL
                THEN (CASE WHEN psp.name = ht.name THEN ht.abbreviation
                           WHEN psp.name = at.name THEN at.abbreviation
                           ELSE COALESCE(psp.abbreviation, gp.spread_pick) END)
                ELSE NULL END AS pick_spread,
           gp.ou_pick AS pick_over_under,
           CASE WHEN gp.ml_pick IS NOT NULL
                THEN (CASE WHEN pml.name = ht.name THEN ht.abbreviation
                           WHEN pml.name = at.name THEN at.abbreviation
                           ELSE COALESCE(pml.abbreviation, gp.ml_pick) END)
                ELSE NULL END AS pick_moneyline,
           gp.ats_ev AS pick_ats_ev,
           gp.ou_ev AS pick_ou_ev,
           gp.ml_ev AS pick_ml_ev,
           gp.ats_result AS result_spread,
           gp.ou_result AS result_over_under,
           gp.ml_result AS result_moneyline
    FROM nba.games g
    JOIN nba.teams ht ON ht.id = g.home_team_id
    JOIN nba.teams at ON at.id = g.away_team_id
    JOIN nba.seasons s ON s.id = g.season_id
    LEFT JOIN nba.betting_lines_consolidated blc ON blc.game_id = g.id
    LEFT JOIN nba.game_predictions gp ON gp.game_id = g.id
    LEFT JOIN nba.teams psp ON psp.id = CASE WHEN gp.spread_pick ~ '^[0-9]+$' THEN gp.spread_pick::bigint END
    LEFT JOIN nba.teams pml ON pml.id = CASE WHEN gp.ml_pick ~ '^[0-9]+$' THEN gp.ml_pick::bigint END
    WHERE {where_clause}
    ORDER BY g.date ASC
    """
    result = await db.execute(text(sql), params)
    _nba_docs = [dict(r) for r in result.mappings().all()]

    # Team records at the time of each game (batched, matches game-detail behavior)
    _sid = await db.execute(
        text("SELECT id FROM nba.seasons WHERE year = :y LIMIT 1"), {"y": year}
    )
    _season_id = _sid.scalar()
    _pairs = []
    for _r in _nba_docs:
        _pairs.append((_r["home_team_id"], _r["game_date"], _season_id))
        _pairs.append((_r["away_team_id"], _r["game_date"], _season_id))
    _records = await _records_as_of_batch(db, "nba", _pairs)
    for _g in _nba_docs:
        _g["home_record"] = _records.get((_g["home_team_id"], str(_g["game_date"]), _season_id))
        _g["away_record"] = _records.get((_g["away_team_id"], str(_g["game_date"]), _season_id))

    games_list = jsonable_encoder(_nba_docs)
    # Premium gate (field-level): schedule/list stays public, but pick + EV fields
    # are premium. Strip them BEFORE caching so a non-premium request can never
    # receive (or poison the cache with) a premium-bearing payload.
    if not user_is_premium(user):
        _today = _chi_today_iso()
        _PREMIUM_KEYS = (
            "pick_spread", "pick_over_under", "pick_moneyline",
            "pick_ats_ev", "pick_ou_ev", "pick_ml_ev",
            "predicted_margin", "predicted_total",
        )
        for _g in games_list:
            _gd = _g.get("game_date") or _g.get("date")
            if _gd and str(_gd)[:10] < _today:
                # Game played yesterday or earlier: picks are public (historical).
                _g["picks_unlocked"] = True
                continue
            _had_picks = any(
                _g.get(_k) is not None
                for _k in ("pick_spread", "pick_over_under", "pick_moneyline")
            )
            for _k in _PREMIUM_KEYS:
                if _k in _g:
                    _g[_k] = None
            # Signal the client picks exist but are gated (schedule-card gate msg).
            _g["picks_locked"] = bool(_had_picks)
    _nba_games_store(cache_key, games_list)
    return JSONResponse(content=games_list, headers={"Cache-Control": "public, max-age=30"})


@router.get("/nba/games/nearest-date")
async def nba_nearest_date(
    year: int = Query(...),
    date: str = Query(...),
    direction: str | None = Query(None),
    team_abbr: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    given_date = datetime.date.fromisoformat(date)

    team_join = ""
    team_filter = ""
    params: dict = {"year": year, "date": given_date}
    if team_abbr:
        team_join = """JOIN nba.teams ht ON ht.id = g.home_team_id
JOIN nba.teams at ON at.id = g.away_team_id
"""
        team_filter = "AND (ht.abbreviation = :team_abbr OR at.abbreviation = :team_abbr)"
        params["team_abbr"] = team_abbr.upper()

    def _nearest_sql(where_op: str, order: str) -> str:
        return f"""\
        SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date
        FROM nba.games g
{team_join}\
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE s.year = :year
          AND (g.date AT TIME ZONE 'America/Chicago')::date {where_op}
          AND g.game_type IN ('REG', 'POST')
{team_filter}\
        ORDER BY game_date {order}
        LIMIT 1"""

    if direction == "backward":
        sql = _nearest_sql("< :date", "DESC")
        result = await db.execute(text(sql), params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": year}

        # Nothing backward this year, try previous year (last date)
        prev_params = {k: v for k, v in params.items() if k != "date"}
        prev_sql = f"""\
        SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date, s.year
        FROM nba.games g
{team_join}\
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE s.year < :year
          AND g.game_type IN ('REG', 'POST')
{team_filter}\
        ORDER BY s.year DESC, game_date DESC
        LIMIT 1"""
        result = await db.execute(text(prev_sql), prev_params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": row[1]}
    elif direction == "forward":
        sql = _nearest_sql("> :date", "ASC")
        result = await db.execute(text(sql), params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": year}

        # Nothing forward this year, try next year (first date)
        next_params = {k: v for k, v in params.items() if k != "date"}
        next_sql = f"""\
        SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date, s.year
        FROM nba.games g
{team_join}\
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE s.year > :year
          AND g.game_type IN ('REG', 'POST')
{team_filter}\
        ORDER BY s.year ASC, game_date ASC
        LIMIT 1"""
        result = await db.execute(text(next_sql), next_params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": row[1]}
    else:
        # No direction: try forward first, then backward
        forward_sql = _nearest_sql("> :date", "ASC")
        result = await db.execute(text(forward_sql), params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": year}

        backward_sql = _nearest_sql("< :date", "DESC")
        result = await db.execute(text(backward_sql), params)
        row = result.fetchone()
        if row:
            return {"date": row[0].isoformat(), "year": year}

    return {"date": None, "year": None}


@router.get("/nba/games/dates")
async def nba_game_dates(
    year: int = Query(...),
    db: AsyncSession = Depends(get_db),
):
    """Return all distinct dates with games for a given year."""
    result = await db.execute(
        text("""
            SELECT DISTINCT (g.date AT TIME ZONE 'America/Chicago')::date AS game_date
            FROM nba.games g
            JOIN nba.seasons s ON s.id = g.season_id
            WHERE s.year = :year
              AND g.game_type IN ('REG', 'POST')
            ORDER BY game_date ASC
        """),
        {"year": year},
    )
    return [row[0].isoformat() for row in result.fetchall()]


@router.get("/nba/players/{player_id}/profile")
async def nba_player_profile(player_id: int, db: AsyncSession = Depends(get_db)):
    """Return full NBA player profile: bio + season stats."""
    from datetime import datetime

    result = await db.execute(
        select(NBAPlayer).options(joinedload(NBAPlayer.team)).where(NBAPlayer.id == player_id)
    )
    player = result.unique().scalar_one_or_none()
    if not player:
        return None

    # Season stats
    r = await db.execute(
        select(NBAPlayerSeasonStats).where(
            NBAPlayerSeasonStats.player_id == player.id
        ).order_by(NBAPlayerSeasonStats.season_id.desc())
    )
    season_stats = r.scalars().all()

    recent_seasons = []
    for ss in season_stats:
        season_r = await db.execute(select(NBASeason).where(NBASeason.id == ss.season_id))
        season = season_r.scalar_one_or_none()
        year = season.year if season else 0
        team_abbr = ""
        if ss.team_id:
            tr = await db.execute(select(NBAPlayerSeasonStats.team_id).where(NBAPlayerSeasonStats.id == ss.id))
            t_result = await db.execute(
                select(NBAPlayerSeasonStats).where(NBAPlayerSeasonStats.id == ss.id)
            )
            from app.models.nba import NBATeam
            if ss.team_id:
                tr2 = await db.execute(select(NBATeam).where(NBATeam.id == ss.team_id))
                team = tr2.scalar_one_or_none()
                if team:
                    team_abbr = team.abbreviation

        recent_seasons.append({
            "year": year,
            "team_abbr": team_abbr,
            "games": ss.games_played,
            "games_started": ss.games_started,
            "minutes_played": ss.minutes_played,
            "points": ss.points,
            "points_per_game": ss.points_per_game,
            "field_goals_made": ss.field_goals_made,
            "field_goals_attempted": ss.field_goals_attempted,
            "field_goal_pct": ss.field_goal_pct,
            "three_points_made": ss.three_points_made,
            "three_points_attempted": ss.three_points_attempted,
            "three_point_pct": ss.three_point_pct,
            "free_throws_made": ss.free_throws_made,
            "free_throws_attempted": ss.free_throws_attempted,
            "free_throw_pct": ss.free_throw_pct,
            "rebounds": ss.rebounds,
            "offensive_rebounds": ss.offensive_rebounds,
            "defensive_rebounds": ss.defensive_rebounds,
            "rebounds_per_game": ss.rebounds_per_game,
            "assists": ss.assists,
            "assists_per_game": ss.assists_per_game,
            "steals": ss.steals,
            "blocks": ss.blocks,
            "turnovers": ss.turnovers,
            "personal_fouls": ss.personal_fouls,
        })

    # Career totals
    career = {
        "games": sum(ss.games_played for ss in season_stats if ss.games_played),
        "points": sum(ss.points for ss in season_stats if ss.points),
        "rebounds": sum(ss.rebounds for ss in season_stats if ss.rebounds),
        "assists": sum(ss.assists for ss in season_stats if ss.assists),
        "steals": sum(ss.steals for ss in season_stats if ss.steals),
        "blocks": sum(ss.blocks for ss in season_stats if ss.blocks),
        "first_year": min((s.year for s in season_stats if s.season_id), default=None) if False else None,
        "last_year": max((s.year for s in season_stats if s.season_id), default=None) if False else None,
    }
    if season_stats:
        years = []
        for ss in season_stats:
            sr = await db.execute(select(NBASeason).where(NBASeason.id == ss.season_id))
            s = sr.scalar_one_or_none()
            if s:
                years.append(s.year)
        if years:
            career["first_year"] = min(years)
            career["last_year"] = max(years)

    return {
        "id": player.id,
        "name": player.name,
        "position": player.position,
        "team_abbr": player.team.abbreviation if player.team else None,
        "team_name": player.team.name if player.team else None,
        "college": player.college,
        "height": player.height,
        "weight": player.weight,
        "birth_date": str(player.birth_date) if player.birth_date else None,
        "years_exp": player.years_exp,
        "status": player.status,
        "jersey_number": player.jersey_number,
        "headshot_url": player.headshot_url,
        "draft": None,
        "depth_chart": None,
        "stats": career,
        "recent_seasons": recent_seasons,
        "injuries": [],
        "transactions": [],
        "writeup": None,
    }
