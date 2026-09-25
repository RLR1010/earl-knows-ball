"""nfl_situational — shared situational-splits query layer (chat tools + writeups).

Single source of truth for the canonical situational tables built by the
NFL ingestion layer:
    nfl.team_splits        (team × {season, career} × split)
    nfl.player_splits_v2   (player × {season, career} × split)
    nfl.def_vs_position    (defence × position group × {season, career})

Every function takes a SQLAlchemy AsyncSession so the same code is used by the
chat tools (app/chat_tools/nfl.py) and the article research (app/writeups/...).

Splits (team/player): all, home, away, division, non_division, vs_afc, vs_nfc,
primetime, non_primetime, dome, outdoor, grass, turf, cold, mild, warm, windy,
calm, rest_short, rest_normal, rest_long, favorite, underdog
(player also: vs_winning, vs_losing).
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

TEAM_SPLITS = [
    "all", "home", "away", "division", "non_division", "vs_afc", "vs_nfc",
    "primetime", "non_primetime", "dome", "outdoor", "grass", "turf",
    "cold", "mild", "warm", "windy", "calm",
    "rest_short", "rest_normal", "rest_long", "favorite", "underdog",
]
PLAYER_SPLITS = TEAM_SPLITS + ["vs_winning", "vs_losing"]

# ---- column whitelists (guards against injection in ORDER BY / metric args) ----
TEAM_METRICS = {
    "win_pct", "games", "points_for", "points_against", "point_diff",
    "epa_per_play", "success_rate", "yards_per_play", "explosive_per_game",
    "third_down_pct", "red_zone_td_pct", "ats_pct", "ou_over_pct",
}
PLAYER_METRICS = {
    "games", "passing_yards", "passing_tds", "passing_epa", "passing_cpoe",
    "rushing_yards", "rushing_tds", "rushing_epa",
    "receiving_yards", "receiving_tds", "receiving_epa",
    "receiving_air_yards", "receiving_yards_after_catch",
    "targets", "receptions", "fantasy_points", "fantasy_points_ppr",
}

_TEAM_COLS = """split_type, split_label, season, games, wins, losses, ties, win_pct,
    points_for, points_against, point_diff, yards_per_play, epa_per_play, success_rate,
    explosive_per_game, third_down_pct, red_zone_td_pct,
    ats_wins, ats_losses, ats_pushes, ats_pct, ou_overs, ou_unders, ou_pushes, ou_over_pct"""

_PLAYER_COLS = """split_type, split_label, season, games,
    completions, attempts, passing_yards, passing_tds, passing_interceptions,
    passing_epa, passing_cpoe, carries, rushing_yards, rushing_tds, rushing_epa,
    targets, receptions, receiving_yards, receiving_tds, receiving_epa,
    receiving_air_yards, receiving_yards_after_catch,
    fantasy_points, fantasy_points_ppr"""

_DVP_COLS = """position_group, season, games, fp_ppg, fp_pg,
    passing_yards, passing_tds, passing_interceptions, rushing_yards, rushing_tds,
    receptions, receiving_yards, receiving_tds, targets,
    receiving_air_yards, receiving_yards_after_catch,
    fantasy_points, fantasy_points_ppr, fp_rank_ppr"""


def _season_clause(season: int | None) -> str:
    return "season = :season" if season is not None else "season IS NULL"


async def team_situational_splits(db: AsyncSession, team_abbr: str,
                                  season: int | None = None,
                                  splits: list[str] | None = None) -> list[dict]:
    """Situational splits for one NFL team (season or career)."""
    sql = f"""
        SELECT {_TEAM_COLS}
        FROM nfl.team_splits
        WHERE team = :team AND {_season_clause(season)}
    """
    params: dict = {"team": team_abbr, "season": season}
    if splits:
        sql += " AND split_type = ANY(:splits)"
        params["splits"] = [s for s in splits if s in TEAM_SPLITS]
    sql += " ORDER BY split_type"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def player_situational_splits(db: AsyncSession, player_name: str,
                                    position: str | None = None,
                                    season: int | None = None,
                                    splits: list[str] | None = None,
                                    metric: str = "games",
                                    limit: int = 30) -> dict:
    """Situational splits for one NFL player (resolves by name)."""
    # resolve the best-matching player (most career games)
    rp = await db.execute(text("""
        SELECT player_id, player_name, position
        FROM nfl.player_splits_v2
        WHERE player_name ILIKE :q AND season IS NULL AND split_type='all'
          AND (CAST(:position AS VARCHAR) IS NULL OR upper(position) = upper(CAST(:position AS VARCHAR)))
        ORDER BY games DESC NULLS LAST LIMIT 1
    """), {"q": f"%{player_name}%", "position": position})
    who = rp.mappings().first()
    if not who:
        return {"error": f"Player not found: {player_name}"}

    sql = f"""
        SELECT {_PLAYER_COLS}
        FROM nfl.player_splits_v2
        WHERE player_id = :pid AND {_season_clause(season)}
    """
    params: dict = {"pid": who["player_id"], "season": season}
    if splits:
        sql += " AND split_type = ANY(:splits)"
        params["splits"] = [s for s in splits if s in PLAYER_SPLITS]
    order_col = metric if metric in PLAYER_METRICS else "games"
    sql += f" ORDER BY {order_col} DESC NULLS LAST LIMIT :lim"
    params["lim"] = max(1, min(int(limit), 60))
    rows = (await db.execute(text(sql), params)).mappings().all()
    return {
        "player_id": who["player_id"], "player_name": who["player_name"],
        "position": who["position"], "season": season, "splits": [dict(r) for r in rows],
    }


async def defense_vs_position(db: AsyncSession, team_abbr: str,
                              position_group: str | None = None,
                              season: int | None = None) -> list[dict]:
    """Production a defence allowed, by opposing position group (season or career)."""
    sql = f"""
        SELECT {_DVP_COLS}
        FROM nfl.def_vs_position
        WHERE defense_team = :team AND {_season_clause(season)}
    """
    params: dict = {"team": team_abbr, "season": season}
    if position_group:
        sql += " AND upper(position_group) = upper(:pg)"
        params["pg"] = position_group
    sql += " ORDER BY position_group"
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def situational_leaders(db: AsyncSession, split_type: str,
                              metric: str = "epa_per_play",
                              season: int | None = None,
                              order: str = "desc", limit: int = 10,
                              min_games: int = 4) -> list[dict]:
    """League leaders for a given TEAM split (e.g. best teams in cold weather)."""
    if split_type not in TEAM_SPLITS:
        return [{"error": f"Unknown split: {split_type}"}]
    metric = metric if metric in TEAM_METRICS else "epa_per_play"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    sql = f"""
        SELECT team, split_label, season, games, wins, losses, win_pct,
               points_for, points_against, epa_per_play, success_rate,
               ats_pct, ou_over_pct
        FROM nfl.team_splits
        WHERE split_type = :st AND {_season_clause(season)} AND games >= :mg
        ORDER BY {metric} {direction} NULLS LAST
        LIMIT :lim
    """
    rows = (await db.execute(text(sql), {
        "st": split_type, "season": season, "mg": max(1, int(min_games)),
        "lim": max(1, min(int(limit), 40)),
    })).mappings().all()
    return [dict(r) for r in rows]


async def player_situational_leaders(db: AsyncSession, split_type: str,
                                     metric: str = "receiving_yards",
                                     position: str | None = None,
                                     season: int | None = None,
                                     order: str = "desc", limit: int = 10,
                                     min_games: int = 4) -> list[dict]:
    """League leaders for a PLAYER split (e.g. most receiving yards in primetime)."""
    if split_type not in PLAYER_SPLITS:
        return [{"error": f"Unknown split: {split_type}"}]
    metric = metric if metric in PLAYER_METRICS else "receiving_yards"
    direction = "ASC" if str(order).lower() == "asc" else "DESC"
    sql = f"""
        SELECT player_name, position, team, split_label, season, games,
               receiving_yards, receiving_tds, receiving_epa, rushing_yards,
               rushing_tds, passing_yards, passing_tds, fantasy_points_ppr
        FROM nfl.player_splits_v2
        WHERE split_type = :st AND {_season_clause(season)} AND games >= :mg
          AND (CAST(:position AS VARCHAR) IS NULL OR upper(position) = upper(CAST(:position AS VARCHAR)))
        ORDER BY {metric} {direction} NULLS LAST
        LIMIT :lim
    """
    rows = (await db.execute(text(sql), {
        "st": split_type, "season": season, "mg": max(1, int(min_games)),
        "position": position, "lim": max(1, min(int(limit), 40)),
    })).mappings().all()
    return [dict(r) for r in rows]
