"""NBA research module — gathers structured data for AI write-up generation.

Follows the same pattern as mlb/research.py and nfl/research.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

def _to_eastern(dt: datetime | None) -> str | None:
    """Format a UTC datetime as Eastern Time string."""
    if dt is None:
        return None
    try:
        import zoneinfo
        eastern = zoneinfo.ZoneInfo("America/New_York")
        et = dt.astimezone(eastern)
        return et.strftime("%Y-%m-%d %I:%M %p ET")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M UTC")


def _fmt_local(dt_str: str | None) -> str | None:
    """Parse ISO UTC string and return ET formatted string."""
    if not dt_str:
        return None
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return _to_eastern(dt)
    except Exception:
        return dt_str


def _parse_utc_dt(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _dt_filter(col: str, before_game_dt: datetime | None) -> str:
    """Return a SQL fragment to filter rows before a given datetime."""
    if before_game_dt:
        return f" AND {col} < :cutoff"
    return ""


# ---------------------------------------------------------------------------
# Database helpers — raw SQL queries on the nba schema
# ---------------------------------------------------------------------------

async def _get_team_name(
    db: AsyncSession, team_id: int | None,
) -> dict:
    """Return dict with name, abbreviation, conference, division, logo_url."""
    if team_id is None:
        return {"name": "TBD", "abbr": "TBD", "conference": "", "division": ""}
    result = await db.execute(
        text("""
            SELECT name, abbreviation, conference, division, logo_url
              FROM nba.teams
             WHERE id = :tid
        """),
        {"tid": team_id},
    )
    row = result.fetchone()
    if row:
        return {
            "name": row[0] or "",
            "abbr": row[1] or "",
            "conference": row[2] or "",
            "division": row[3] or "",
            "logo_url": row[4] or "",
        }
    return {"name": "Unknown", "abbr": "", "conference": "", "division": ""}


async def _get_active_season(db: AsyncSession) -> dict | None:
    """Return the most recent NBA season (by year)."""
    result = await db.execute(
        text("""
            SELECT id, year, start_date, end_date
              FROM nba.seasons
             ORDER BY year DESC
             LIMIT 1
        """),
    )
    row = result.fetchone()
    if row:
        return {
            "id": row[0],
            "year": row[1],
            "start_date": str(row[2]) if row[2] else None,
            "end_date": str(row[3]) if row[3] else None,
        }
    return None


async def _get_betting_lines(
    db: AsyncSession,
    game_id: int,
    consolidate_table: str = "betting_lines_consolidated",
) -> dict:
    """Fetch consolidated betting lines for a game."""
    result = await db.execute(
        text(f"""
            SELECT closing_spread, closing_ou, closing_home_ml,
                   closing_away_ml, closing_home_implied_probability
              FROM nba.{consolidate_table}
             WHERE game_id = :gid
             LIMIT 1
        """),
        {"gid": game_id},
    )
    row = result.fetchone()
    if row:
        spread, ou, home_ml, away_ml, impl_prob = row
        return {
            "spread": float(spread) if spread else None,
            "over_under": float(ou) if ou else None,
            "home_moneyline": int(home_ml) if home_ml else None,
            "away_moneyline": int(away_ml) if away_ml else None,
            "home_implied_prob": float(impl_prob) if impl_prob else None,
        }
    return {"error": "No betting lines found"}


async def _get_game_predictions(
    db: AsyncSession, game_id: int,
) -> dict | None:
    """Fetch model predictions for the game."""
    result = await db.execute(
        text("""
            SELECT predicted_home_score, predicted_away_score,
                   predicted_total, predicted_margin,
                   margin_conf, ou_conf, ou_pick,
                   spread_pick, ats_ev, ou_ev, ml_ev,
                   ml_pick, ml_conf, ats_conf_cal, ml_conf_cal, ou_conf_cal
              FROM nba.game_predictions
             WHERE game_id = :gid
             LIMIT 1
        """),
        {"gid": game_id},
    )
    row = result.fetchone()
    if not row:
        return None
    return {
        "predicted_home_score": int(row[0]) if row[0] else None,
        "predicted_away_score": int(row[1]) if row[1] else None,
        "predicted_total": float(row[2]) if row[2] else None,
        "predicted_margin": float(row[3]) if row[3] else None,
        "margin_conf": float(row[4]) if row[4] else None,
        "ou_conf": float(row[5]) if row[5] else None,
        "ou_pick": row[6],
        "spread_pick": row[7],
        "ats_ev": float(row[8]) if row[8] else None,
        "ou_ev": float(row[9]) if row[9] else None,
        "ml_ev": float(row[10]) if row[10] else None,
        "ml_pick": row[11],
        "ml_conf": float(row[12]) if row[12] else None,
        "ats_conf_cal": float(row[13]) if row[13] else None,
        "ml_conf_cal": float(row[14]) if row[14] else None,
        "ou_conf_cal": float(row[15]) if row[15] else None,
    }


async def _get_team_season_wins(
    db: AsyncSession, team_id: int, season_year: int,
) -> dict:
    """Get W/L record for a team in a season."""
    result = await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'FINAL'
                      AND ((home_team_id = :tid AND home_score > away_score)
                        OR (away_team_id = :tid AND away_score > home_score))) AS wins,
                COUNT(*) FILTER (WHERE status = 'FINAL'
                      AND ((home_team_id = :tid AND home_score < away_score)
                        OR (away_team_id = :tid AND away_score < home_score))) AS losses
            FROM nba.games g
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
              AND s.year = :year
              AND g.status = 'FINAL'
        """),
        {"tid": team_id, "year": season_year},
    )
    row = result.fetchone()
    wins = row[0] or 0
    losses = row[1] or 0
    return {"wins": wins, "losses": losses, "total": wins + losses, "pct": round(wins / max(wins + losses, 1), 3)}


async def _get_team_recent_form(
    db: AsyncSession, team_id: int, season_year: int,
    limit: int = 10,
) -> list[dict]:
    """Get recent games for a team in a season."""
    result = await db.execute(
        text("""
            SELECT
                g.date,
                ht.abbreviation AS home_abbr,
                at.abbreviation AS away_abbr,
                g.home_score,
                g.away_score,
                g.home_team_id,
                g.away_team_id,
                g.id
            FROM nba.games g
            JOIN nba.teams ht ON g.home_team_id = ht.id
            JOIN nba.teams at ON g.away_team_id = at.id
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
              AND s.year = :year
              AND g.status = 'FINAL'
            ORDER BY g.date DESC
            LIMIT :lim
        """),
        {"tid": team_id, "year": season_year, "lim": limit},
    )
    rows = result.fetchall()
    games = []
    for row in rows:
        date_val, home_abbr, away_abbr, home_score, away_score, home_id, away_id, gid = row
        is_home = home_id == team_id
        opp_abbr = away_abbr if is_home else home_abbr
        our_score = home_score if is_home else away_score
        opp_score = away_score if is_home else home_score
        won = (home_score is not None and away_score is not None
               and ((is_home and home_score > away_score)
                    or (not is_home and away_score > home_score)))
        games.append({
            "date": str(date_val) if date_val else "",
            "opponent": opp_abbr,
            "location": "home" if is_home else "away",
            "result": "W" if won else "L" if our_score is not None else "-",
            "score": f"{our_score}-{opp_score}" if our_score is not None else "",
            "game_id": gid,
        })
    return games


async def _get_team_avg_stats(
    db: AsyncSession, team_id: int, season_year: int,
) -> dict:
    """Get per-game averages from finished NBA games."""
    result = await db.execute(
        text("""
            SELECT
                AVG(CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END) AS ppg,
                AVG(CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END) AS oppg,
                AVG(CASE WHEN g.home_team_id = :tid THEN g.home_score - g.away_score ELSE g.away_score - g.home_score END) AS pt_diff
            FROM nba.games g
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
              AND s.year = :year
              AND g.status = 'FINAL'
        """),
        {"tid": team_id, "year": season_year},
    )
    row = result.fetchone()
    if not row or row[0] is None:
        return {"ppg": None, "oppg": None, "pt_diff": None}
    return {
        "ppg": round(float(row[0]), 1),
        "oppg": round(float(row[1]), 1),
        "pt_diff": round(float(row[2]), 1),
    }


async def _get_head_to_head(
    db: AsyncSession, home_team_id: int, away_team_id: int, season_year: int,
) -> dict:
    """Get head-to-head record between two teams in the same season."""
    result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE g.home_team_id = :home
                      AND g.home_score > g.away_score) AS home_wins,
                COUNT(*) FILTER (WHERE g.away_team_id = :away
                      AND g.away_score > g.home_score) AS away_wins
            FROM nba.games g
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE ((g.home_team_id = :home AND g.away_team_id = :away)
                OR (g.home_team_id = :away AND g.away_team_id = :home))
              AND s.year = :year
              AND g.status = 'FINAL'
        """),
        {"home": home_team_id, "away": away_team_id, "year": season_year},
    )
    row = result.fetchone()
    if row and row[0]:
        home_wins = row[1] or 0
        away_wins_row_2 = row[2] or 0
        return {
            "games_played": row[0],
            "home_wins": home_wins,
            "away_series_wins": away_wins_row_2,
        }
    return {"games_played": 0, "home_wins": 0, "away_series_wins": 0}


async def _get_team_features(
    db: AsyncSession, game_id: int,
) -> dict | None:
    """Fetch feature rows for a game (training features with rolling stats).
    NBA features table is a definitions table; skip if no game-level values.
    """
    # NBA features table is a feature definitions table, not game-level values
    # Skip features lookup for NBA research brief
    return None


async def _get_conference_standings(
    db: AsyncSession, conference: str, season_year: int,
) -> list[dict]:
    """Get standings for a conference sorted by win pct."""
    result = await db.execute(
        text("""
            WITH team_wins AS (
                SELECT
                    t.id AS team_id,
                    t.name AS team_name,
                    t.abbreviation AS abbr,
                    COUNT(*) FILTER (WHERE g.status = 'FINAL'
                        AND ((g.home_team_id = t.id AND g.home_score > g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score > g.home_score))) AS wins,
                    COUNT(*) FILTER (WHERE g.status = 'FINAL'
                        AND ((g.home_team_id = t.id AND g.home_score < g.away_score)
                          OR (g.away_team_id = t.id AND g.away_score < g.home_score))) AS losses
                FROM nba.teams t
                JOIN nba.games g ON g.home_team_id = t.id OR g.away_team_id = t.id
                JOIN nba.seasons s ON g.season_id = s.id
                WHERE t.conference = :conf AND s.year = :year AND g.status = 'FINAL'
                GROUP BY t.id, t.name, t.abbreviation
            )
            SELECT team_name, abbr, wins, losses,
                   ROUND(wins::numeric / NULLIF(wins + losses, 0), 3) AS pct
            FROM team_wins
            ORDER BY pct DESC
        """),
        {"conf": conference, "year": season_year},
    )
    rows = result.fetchall()
    return [
        {"name": r[0], "abbr": r[1], "wins": r[2], "losses": r[3], "pct": float(r[4]) if r[4] else 0.0}
        for r in rows
    ]


async def _get_team_star_player(
    db: AsyncSession, team_id: int, season_year: int,
) -> dict | None:
    """Get the best player on a team for the season (by PPG)."""
    result = await db.execute(
        text("""
            SELECT p.name, p.position,
                   pss.points_per_game, pss.assists_per_game, pss.rebounds_per_game,
                   pss.minutes_played, pss.games_played,
                   pss.field_goal_pct, pss.three_point_pct
            FROM nba.player_season_stats pss
            JOIN nba.players p ON pss.player_id = p.id
            JOIN nba.seasons s ON pss.season_id = s.id
            WHERE pss.team_id = :tid AND s.year = :year
            ORDER BY pss.points_per_game DESC
            LIMIT 1
        """),
        {"tid": team_id, "year": season_year},
    )
    row = result.fetchone()
    if not row:
        return None
    return {
        "name": row[0],
        "position": row[1] or "",
        "ppg": float(row[2]) if row[2] else 0.0,
        "apg": float(row[3]) if row[3] else 0.0,
        "rpg": float(row[4]) if row[4] else 0.0,
        "minutes": float(row[5]) if row[5] else 0.0,
        "games_played": row[6] or 0,
        "fg_pct": float(row[7]) if row[7] else 0.0,
        "three_pct": float(row[8]) if row[8] else 0.0,
    }


async def _get_team_recent_ats(
    db: AsyncSession, team_id: int, season_year: int, limit: int = 10,
) -> dict:
    """Get ATS performance for a team over recent games.
    Uses the betting_lines_consolidated table to get spread info.
    """
    result = await db.execute(
        text("""
            SELECT
                blc.closing_spread,
                g.home_team_id,
                g.away_team_id,
                g.home_score,
                g.away_score,
                g.date
            FROM nba.games g
            JOIN nba.betting_lines_consolidated blc ON g.id = blc.game_id
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
              AND s.year = :year
              AND g.status = 'FINAL'
              AND blc.closing_spread IS NOT NULL
            ORDER BY g.date DESC
            LIMIT :lim
        """),
        {"tid": team_id, "year": season_year, "lim": limit},
    )
    rows = result.fetchall()
    covered = 0
    total = 0
    for row in rows:
        spread, home_id, away_id, home_score, away_score, date_val = row
        if home_score is None or away_score is None:
            continue
        total += 1
        is_home = home_id == team_id
        margin = home_score - away_score
        if is_home:
            # home team covers if margin > spread (spread is negative for fav)
            if margin + spread > 0:
                covered += 1
        else:
            # away team covers if (-margin) > -spread → away_margin > -spread
            if -margin + spread > 0:
                covered += 1
    return {
        "covered": covered,
        "total": total,
        "pct": round(covered / max(total, 1), 3),
    }


# ---------------------------------------------------------------------------
# Main research brief builder
# ---------------------------------------------------------------------------

async def get_research_brief(
    db: AsyncSession,
    game_id: int,
    season_year: int | None = None,
    include_predictions: bool = True,
) -> dict:
    """Build a comprehensive research brief for NBA write-up generation.

    Returns a dict with sections:
    - game_info
    - team_home / team_away (season stats, form, star player, ATS)
    - betting_lines
    - model_predictions (if available)
    - head_to_head
    - conference_standings
    - season_context
    """
    # --- Get game info ---
    result = await db.execute(
        text("""
            SELECT g.date, g.game_type, g.venue, g.status,
                   g.home_team_id, g.away_team_id,
                   g.home_score, g.away_score, g.season_id,
                   s.year
            FROM nba.games g
            JOIN nba.seasons s ON g.season_id = s.id
            WHERE g.id = :gid
        """),
        {"gid": game_id},
    )
    game_row = result.fetchone()
    if not game_row:
        return {"error": f"Game {game_id} not found"}

    (game_date, game_type, venue, status,
     home_team_id, away_team_id,
     home_score, away_score, season_id, season_year_from_db) = game_row

    season_year = season_year or season_year_from_db or 2025

    game_dt = _parse_utc_dt(str(game_date)) if game_date else None

    # --- Get team info ---
    home_team = await _get_team_name(db, home_team_id)
    away_team = await _get_team_name(db, away_team_id)

    # --- Team season stats ---
    home_stats = await _get_team_avg_stats(db, home_team_id, season_year)
    away_stats = await _get_team_avg_stats(db, away_team_id, season_year)

    # --- Team records ---
    home_record = await _get_team_season_wins(db, home_team_id, season_year)
    away_record = await _get_team_season_wins(db, away_team_id, season_year)

    # --- Recent form ---
    home_form = await _get_team_recent_form(db, home_team_id, season_year)
    away_form = await _get_team_recent_form(db, away_team_id, season_year)

    # --- Star players ---
    home_star = await _get_team_star_player(db, home_team_id, season_year)
    away_star = await _get_team_star_player(db, away_team_id, season_year)

    # --- Betting lines ---
    betting = await _get_betting_lines(db, game_id)

    # --- Model predictions ---
    predictions = None
    if include_predictions:
        predictions = await _get_game_predictions(db, game_id)

    # --- Head-to-head ---
    h2h = await _get_head_to_head(db, home_team_id, away_team_id, season_year)

    # --- Conference standings ---
    home_standings = None
    away_standings = None
    if home_team.get("conference"):
        home_standings = await _get_conference_standings(db, home_team["conference"], season_year)
    if away_team.get("conference") and away_team["conference"] != home_team.get("conference"):
        away_standings = await _get_conference_standings(db, away_team["conference"], season_year)
    elif away_team.get("conference"):
        away_standings = home_standings  # same conference

    # --- ATS recent ---
    home_ats = await _get_team_recent_ats(db, home_team_id, season_year)
    away_ats = await _get_team_recent_ats(db, away_team_id, season_year)

    # --- Team features (rolling stats if available) ---
    features = await _get_team_features(db, game_id)

    brief = {
        "game_info": {
            "game_id": game_id,
            "home_team": home_team,
            "away_team": away_team,
            "date": str(game_date) if game_date else "",
            "formatted_time": _to_eastern(game_dt) if game_dt else "",
            "game_type": game_type or "Regular Season",
            "venue": venue or "TBD",
            "status": status or "SCHEDULED",
            "score": f"{home_score}-{away_score}" if home_score is not None else None,
        },
        "team_home": {
            "record": home_record,
            "stats": home_stats,
            "recent_form": home_form,
            "star_player": home_star,
            "ats_recent": home_ats,
        },
        "team_away": {
            "record": away_record,
            "stats": away_stats,
            "recent_form": away_form,
            "star_player": away_star,
            "ats_recent": away_ats,
        },
        "betting_lines": betting,
    }

    if predictions:
        brief["model_predictions"] = predictions

    if h2h:
        brief["head_to_head"] = h2h

    if home_standings:
        brief["standings"] = {
            home_team.get("conference", ""): home_standings,
        }
    if away_standings and away_standings != home_standings:
        brief["standings"] = brief.get("standings", {})
        brief["standings"][away_team.get("conference", "")] = away_standings

    if features:
        brief["features"] = features

    # --- Article enrichment (same as NFL/MLB) ---
    _enrich_game_dt = _parse_utc_dt(str(game_date)) if game_date else None
    _home_name = home_team.get("name", "")
    _away_name = away_team.get("name", "")
    if _home_name and _away_name and _enrich_game_dt:
        from app.writeups.enrichment import enrich_writeup_context

        if _enrich_game_dt:
            enrichment = await enrich_writeup_context(
                db=db,
                sport="nba",
                home_team=_home_name,
                away_team=_away_name,
                game_date=_enrich_game_dt,  # pass full datetime not just date
            )
            if enrichment:
                brief["article_enrichment"] = enrichment

    return brief


async def get_public_research_brief(
    db: AsyncSession,
    game_id: int,
    season_year: int | None = None,
) -> dict:
    """Build a research brief with public-only data (no model predictions)."""
    return await get_research_brief(
        db, game_id, season_year=season_year, include_predictions=False,
    )
