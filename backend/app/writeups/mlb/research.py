"""
MLB research functions for the write-up generation pipeline.

Each function queries the database and returns a clean dict
suitable for serialization into the write-up's research_brief JSONB column.

All functions accept an optional *as_of_date* parameter for historical
write-ups — queries filter to data available before that date.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mlb import (
    MLBGames,
    MLBTeamSplit,
    MLBBullpenStat,
    MLBVenue,
)

logger = logging.getLogger("writeups")


# ──────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────

def _dt_filter(as_of_date: datetime | None, prefix: str = "g.") -> str:
    """Return SQL fragment filtering rows <= *as_of_date*."""
    if as_of_date is None:
        return ""
    return f"AND {prefix}date <= '{as_of_date.isoformat()}'"


_STATUS_FINAL = "FINAL"  # nfl.gamestatus enum value shared by all sports


# ──────────────────────────────────────────────
#  1. Game Summary
# ──────────────────────────────────────────────

async def get_game_summary(
    db: AsyncSession,
    game_id: int,
) -> dict[str, Any]:
    """Basic game info — teams, date, venue, status, score, pitchers by name."""
    row = await db.execute(
        text("""
            SELECT
                g.id,
                g.mlb_game_id,
                g.date,
                g.status::text,
                g.home_score, g.away_score,
                g.venue, g.venue_id, g.roof_type, g.surface,
                g.temperature, g.wind_speed, g.weather_condition,
                g.scheduled_innings, g.day_night, g.attendance,
                g.game_type, g.season_id,
                g.home_pitcher_name, g.away_pitcher_name,
                ht.id   AS home_team_id,
                ht.name AS home_team_name,
                ht.abbreviation AS home_team_abbr,
                at.id   AS away_team_id,
                at.name AS away_team_name,
                at.abbreviation AS away_team_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE g.id = :game_id
        """),
        {"game_id": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"error": f"Game {game_id} not found"}

    rec = None
    if r["home_score"] is not None:
        # Derive records from game scores where available
        pass  # games don't store W/L per-game; computed in season_stats

    return {
        "game_id": r["id"],
        "mlb_game_id": r["mlb_game_id"],
        "season_id": r["season_id"],
        "date": r["date"].isoformat() if r["date"] else None,
        "status": r["status"],
        "game_type": r["game_type"],
        "day_night": r["day_night"],
        "home_team": {
            "id": r["home_team_id"],
            "name": r["home_team_name"],
            "abbreviation": r["home_team_abbr"],
        },
        "away_team": {
            "id": r["away_team_id"],
            "name": r["away_team_name"],
            "abbreviation": r["away_team_abbr"],
        },
        "venue": {
            "name": r["venue"],
            "id": r["venue_id"],
            "roof_type": r["roof_type"],
            "surface": r["surface"],
        },
        "weather": {
            "temperature": r["temperature"],
            "wind_speed": r["wind_speed"],
            "condition": r["weather_condition"],
        },
        "score": {
            "home": r["home_score"],
            "away": r["away_score"],
        } if r["home_score"] is not None else None,
        "pitchers": {
            "home": r["home_pitcher_name"],
            "away": r["away_pitcher_name"],
        },
    }


# ──────────────────────────────────────────────
#  2. Team Season Stats
# ──────────────────────────────────────────────

async def get_team_season_stats(
    db: AsyncSession,
    team_id: int,
    season_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Team-level record, RS/G, RA/G, home/away splits from completed games."""
    dt_final = _dt_filter(as_of_date)
    # Replace placeholders for the two joined variants
    dt_final_h = _dt_filter(as_of_date, "gh.")
    dt_final_a = _dt_filter(as_of_date, "ga.")

    row = await db.execute(
        text(f"""
            WITH team_games AS (
                SELECT
                    t.id AS team_id,
                    t.name, t.abbreviation,
                    s.year::text AS season_name,
                    COALESCE(h.games, 0) AS home_games,
                    COALESCE(h.wins, 0) AS home_wins,
                    COALESCE(h.losses, 0) AS home_losses,
                    COALESCE(h.runs_scored, 0) AS home_rs,
                    COALESCE(h.runs_allowed, 0) AS home_ra,
                    COALESCE(a.games, 0) AS away_games,
                    COALESCE(a.wins, 0) AS away_wins,
                    COALESCE(a.losses, 0) AS away_losses,
                    COALESCE(a.runs_scored, 0) AS away_rs,
                    COALESCE(a.runs_allowed, 0) AS away_ra
                FROM mlb.teams t
                JOIN mlb.seasons s ON s.id = :sid
                LEFT JOIN (
                    SELECT home_team_id AS tid,
                           COUNT(*) AS games,
                           SUM(CASE WHEN home_score > away_score THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN home_score < away_score THEN 1 ELSE 0 END) AS losses,
                           SUM(home_score) AS runs_scored,
                           SUM(away_score) AS runs_allowed
                    FROM mlb.games gh
                    WHERE gh.season_id = :sid
                      AND gh.status::text = :sfinal
                      AND gh.home_team_id = :tid
                      {dt_final_h}
                    GROUP BY gh.home_team_id
                ) h ON h.tid = t.id
                LEFT JOIN (
                    SELECT away_team_id AS tid,
                           COUNT(*) AS games,
                           SUM(CASE WHEN away_score > home_score THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN away_score < home_score THEN 1 ELSE 0 END) AS losses,
                           SUM(away_score) AS runs_scored,
                           SUM(home_score) AS runs_allowed
                    FROM mlb.games ga
                    WHERE ga.season_id = :sid
                      AND ga.status::text = :sfinal
                      AND ga.away_team_id = :tid
                      {dt_final_a}
                    GROUP BY ga.away_team_id
                ) a ON a.tid = t.id
                WHERE t.id = :tid
            )
            SELECT * FROM team_games
        """),
        {"tid": team_id, "sid": season_id, "sfinal": _STATUS_FINAL},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"error": f"Team {team_id} in season {season_id} not found"}

    total_gp = r["home_games"] + r["away_games"]
    total_wins = r["home_wins"] + r["away_wins"]
    total_losses = r["home_losses"] + r["away_losses"]
    total_rs = r["home_rs"] + r["away_rs"]
    total_ra = r["home_ra"] + r["away_ra"]

    return {
        "team_id": team_id,
        "name": r["name"],
        "abbreviation": r["abbreviation"],
        "season": r["season_name"],
        "games_played": total_gp,
        "record": {"wins": total_wins, "losses": total_losses},
        "home_record": {
            "wins": r["home_wins"],
            "losses": r["home_losses"],
        },
        "away_record": {
            "wins": r["away_wins"],
            "losses": r["away_losses"],
        },
        "runs_scored_per_game": round(total_rs / total_gp, 2) if total_gp > 0 else None,
        "runs_allowed_per_game": round(total_ra / total_gp, 2) if total_gp > 0 else None,
        "run_differential": total_rs - total_ra,
    }


# ──────────────────────────────────────────────
#  3. Pitching Matchup
# ──────────────────────────────────────────────

async def get_pitching_matchup(
    db: AsyncSession,
    game_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Resolve starters by name, return season + recent stats.

    Games store *home_pitcher_name* / *away_pitcher_name* but not player
    IDs.  We attempt to match by name + team; if that fails we return
    just the name with a null stats block.
    """
    game = await db.get(MLBGames, game_id)
    if game is None:
        return {"error": f"Game {game_id} not found"}

    return {
        "home": await _pitcher_profile(
            db, game.home_pitcher_name, game.home_team_id, game.season_id,
            game.away_team_id, as_of_date,
        ),
        "away": await _pitcher_profile(
            db, game.away_pitcher_name, game.away_team_id, game.season_id,
            game.home_team_id, as_of_date,
        ),
    }


async def _pitcher_profile(
    db: AsyncSession,
    name: str | None,
    team_id: int,
    season_id: int,
    _opponent_team_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    if not name:
        return {"name": "TBD", "season_stats": None, "recent_starts": []}

    # Try to find the player record by name + team
    player = await db.execute(
        text("""
            SELECT id, name, throws, position
            FROM mlb.players
            WHERE name ILIKE :name AND team_id = :tid
            LIMIT 1
        """),
        {"name": name.strip(), "tid": team_id},
    )
    pl = player.mappings().one_or_none()

    profile = {"name": name}

    if pl is None:
        profile["throws"] = None
        profile["position"] = None
        profile["season_stats"] = None
        profile["recent_starts"] = None
        return profile

    profile["throws"] = pl["throws"]
    profile["position"] = pl["position"]

    # Season stats
    ps_row = await db.execute(
        text("""
            SELECT wins, losses, era, whip, games_started,
                   innings_pitched, strikeouts, base_on_balls,
                   hits, home_runs, strikeout_walk_ratio,
                   hits_per_9, strikeouts_per_9, walks_per_9
            FROM mlb.pitching_stats
            WHERE player_id = :pid AND season_id = :sid
            LIMIT 1
        """),
        {"pid": pl["id"], "sid": season_id},
    )
    ps = ps_row.mappings().one_or_none()
    profile["season_stats"] = dict(ps) if ps else None

    # Last 5 starts — games where this pitcher started (by name match)
    dt_f = _dt_filter(as_of_date)
    recent = await db.execute(
        text(f"""
            SELECT g.date, g.home_score, g.away_score,
                   g.home_pitcher_name, g.away_pitcher_name,
                   ht.abbreviation AS home_abbr,
                   at.abbreviation AS away_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE (
                g.home_pitcher_name ILIKE :name
                OR g.away_pitcher_name ILIKE :name
            )
            AND g.status::text = :sfinal
            AND g.season_id = :sid
            AND g.id != :gid
            {dt_f}
            ORDER BY g.date DESC
            LIMIT 5
        """),
        {"name": name.strip(), "sfinal": _STATUS_FINAL,
         "sid": season_id, "gid": None},
    )
    # We don't have the current game_id easily here; exclude by date instead
    recent = await db.execute(
        text(f"""
            SELECT g.date, g.home_score, g.away_score,
                   g.home_pitcher_name, g.away_pitcher_name,
                   ht.abbreviation AS home_abbr,
                   at.abbreviation AS away_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE (
                g.home_pitcher_name ILIKE :name
                OR g.away_pitcher_name ILIKE :name
            )
            AND g.status::text = :sfinal
            AND g.season_id = :sid
            {dt_f}
            ORDER BY g.date DESC
            LIMIT 5
        """),
        {"name": name.strip(), "sfinal": _STATUS_FINAL, "sid": season_id},
    )

    starts = []
    for s in recent.mappings():
        is_home = (s["home_pitcher_name"] or "").lower() == (name or "").lower()
        starts.append({
            "date": s["date"].isoformat() if s["date"] else None,
            "opponent": s["away_abbr"] if is_home else s["home_abbr"],
            "team_score": s["home_score"] if is_home else s["away_score"],
            "opponent_score": s["away_score"] if is_home else s["home_score"],
        })

    profile["recent_starts"] = starts
    return profile


# ──────────────────────────────────────────────
#  4. Betting Lines
# ──────────────────────────────────────────────

async def get_betting_lines(
    db: AsyncSession,
    game_id: int,
) -> dict[str, Any]:
    """Opening and current lines from consolidated or raw tables."""
    row = await db.execute(
        text("""
            SELECT
                opening_spread, opening_ou, opening_home_ml, opening_away_ml,
                closing_spread, closing_ou, closing_home_ml, closing_away_ml,
                closing_over_odds, closing_under_odds
            FROM mlb.betting_lines_consolidated
            WHERE game_id = :game_id
        """),
        {"game_id": game_id},
    )
    r = row.mappings().one_or_none()
    if r:
        return {
            "opening": {
                "spread": r["opening_spread"],
                "over_under": r["opening_ou"],
                "home_moneyline": r["opening_home_ml"],
                "away_moneyline": r["opening_away_ml"],
            },
            "current": {
                "spread": r["closing_spread"],
                "over_under": r["closing_ou"],
                "home_moneyline": r["closing_home_ml"],
                "away_moneyline": r["closing_away_ml"],
            },
        }

    # Fallback — raw betting_lines
    rows = await db.execute(
        text("""
            SELECT spread, over_under, home_moneyline, away_moneyline,
                   recorded_at
            FROM mlb.betting_lines
            WHERE game_id = :game_id
            ORDER BY recorded_at ASC
        """),
        {"game_id": game_id},
    )
    raw = list(rows.mappings())
    if not raw:
        return {"opening": None, "current": None, "consensus": None}

    opening = raw[0]
    closing = raw[-1]
    return {
        "opening": {
            "spread": opening["spread"],
            "over_under": opening["over_under"],
            "home_moneyline": opening["home_moneyline"],
            "away_moneyline": opening["away_moneyline"],
        },
        "current": {
            "spread": closing["spread"],
            "over_under": closing["over_under"],
            "home_moneyline": closing["home_moneyline"],
            "away_moneyline": closing["away_moneyline"],
        },
        "consensus": None,
    }


# ──────────────────────────────────────────────
#  5. Predictions
# ──────────────────────────────────────────────

async def get_predictions(
    db: AsyncSession,
    game_id: int,
) -> dict[str, Any]:
    """Latest model predictions for this game.

    Returns EV (expected value) scores and predicted runs.
    Confidence scores are intentionally excluded — only EV is surfaced.
    """
    row = await db.execute(
        text("""
            SELECT
                run_line_pick,
                ou_pick,
                ml_pick,
                predicted_home_runs, predicted_away_runs,
                predicted_total, predicted_margin,
                ats_ev, ou_ev, ml_ev
            FROM mlb.game_predictions
            WHERE game_id = :game_id
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"game_id": game_id},
    )
    r = row.mappings().one_or_none()
    if r is None:
        return {"error": "No predictions for this game"}

    return {
        "run_line": {
            "pick": r["run_line_pick"],
        },
        "over_under": {
            "pick": r["ou_pick"],
        },
        "moneyline": {
            "pick": r["ml_pick"],
        },
        "predicted_runs": {
            "home": r["predicted_home_runs"],
            "away": r["predicted_away_runs"],
            "total": r["predicted_total"],
            "margin": r["predicted_margin"],
        },
        "expected_value": {
            "ats": r["ats_ev"],
            "ou": r["ou_ev"],
            "ml": r["ml_ev"],
        },
    }


# ──────────────────────────────────────────────
#  6. Head-to-Head
# ──────────────────────────────────────────────

async def get_head_to_head(
    db: AsyncSession,
    team1_id: int,
    team2_id: int,
    season_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Completed games between the two teams this season."""
    dt_f = _dt_filter(as_of_date)

    rows = await db.execute(
        text(f"""
            SELECT g.date, g.home_score, g.away_score,
                   g.home_team_id, g.away_team_id,
                   ht.abbreviation AS home_abbr,
                   at.abbreviation AS away_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE (
                (g.home_team_id = :t1 AND g.away_team_id = :t2)
                OR (g.home_team_id = :t2 AND g.away_team_id = :t1)
            )
            AND g.season_id = :sid
            AND g.status::text = :sfinal
            {dt_f}
            ORDER BY g.date ASC
        """),
        {"t1": team1_id, "t2": team2_id, "sid": season_id, "sfinal": _STATUS_FINAL},
    )

    games = []
    t1_wins = t2_wins = 0
    for r in rows.mappings():
        t1_is_home = r["home_team_id"] == team1_id
        t1_score = r["home_score"] if t1_is_home else r["away_score"]
        t2_score = r["away_score"] if t1_is_home else r["home_score"]
        if t1_score > t2_score:
            t1_wins += 1
        else:
            t2_wins += 1
        games.append({
            "date": r["date"].isoformat() if r["date"] else None,
            "venue": r["home_abbr"],
            "winner": r["home_abbr"] if r["home_score"] > r["away_score"] else r["away_abbr"],
            "score": f"{r['home_score']}-{r['away_score']}",
        })

    return {
        "team1_id": team1_id, "team2_id": team2_id,
        "team1_wins": t1_wins, "team2_wins": t2_wins,
        "total_games": len(games), "games": games,
    }


# ──────────────────────────────────────────────
#  7. Injuries
# ──────────────────────────────────────────────

async def get_injuries(
    db: AsyncSession,
    game_id: int,
) -> dict[str, list]:
    """Active injuries for both teams."""
    game = await db.get(MLBGames, game_id)
    if game is None:
        return {"home": [], "away": []}

    rows = await db.execute(
        text("""
            SELECT p.name, p.position, i.team_id,
                   i.status AS injury_status,
                   i.description AS injury_detail,
                   t.abbreviation AS team_abbr
            FROM mlb.injuries i
            JOIN mlb.players p ON p.id = i.player_id
            JOIN mlb.teams t ON t.id = i.team_id
            WHERE i.team_id IN (:home_id, :away_id)
            AND i.is_active = TRUE
            ORDER BY i.team_id, p.position
        """),
        {"home_id": game.home_team_id, "away_id": game.away_team_id},
    )

    injuries: dict[str, list] = {"home": [], "away": []}
    for r in rows.mappings():
        side = "home" if r["team_id"] == game.home_team_id else "away"
        injuries[side].append({
            "name": r["name"],
            "position": r["position"],
            "status": r["injury_status"],
            "detail": r["injury_detail"],
        })

    return injuries


# ──────────────────────────────────────────────
#  8. Recent Form
# ──────────────────────────────────────────────

async def get_recent_form(
    db: AsyncSession,
    team_id: int,
    season_id: int,
    n: int = 10,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Last *n* completed games for a team with results."""
    dt_f = _dt_filter(as_of_date)

    rows = await db.execute(
        text(f"""
            SELECT g.date,
                   CASE WHEN g.home_team_id = :tid THEN 'home' ELSE 'away' END AS location,
                   CASE WHEN g.home_team_id = :tid THEN g.home_score ELSE g.away_score END AS team_score,
                   CASE WHEN g.home_team_id = :tid THEN g.away_score ELSE g.home_score END AS opp_score,
                   ht.abbreviation AS home_abbr,
                   at.abbreviation AS away_abbr
            FROM mlb.games g
            JOIN mlb.teams ht ON ht.id = g.home_team_id
            JOIN mlb.teams at ON at.id = g.away_team_id
            WHERE (g.home_team_id = :tid OR g.away_team_id = :tid)
            AND g.season_id = :sid
            AND g.status::text = :sfinal
            {dt_f}
            ORDER BY g.date DESC
            LIMIT :n
        """),
        {"tid": team_id, "sid": season_id, "sfinal": _STATUS_FINAL, "n": n},
    )

    games = []
    wins = 0
    for r in rows.mappings():
        won = r["team_score"] > r["opp_score"]
        if won:
            wins += 1
        games.append({
            "date": r["date"].isoformat() if r["date"] else None,
            "location": r["location"],
            "result": "W" if won else "L",
            "team_score": r["team_score"],
            "opponent_score": r["opp_score"],
            "opponent": r["away_abbr"] if r["location"] == "home" else r["home_abbr"],
        })

    return {"last_n": len(games), "wins": wins, "losses": len(games) - wins, "games": games}


# ──────────────────────────────────────────────
#  9. Bullpen Stats
# ──────────────────────────────────────────────

async def get_bullpen_stats(
    db: AsyncSession,
    team_id: int,
    season_id: int,
) -> dict[str, Any] | None:
    row = await db.execute(
        select(MLBBullpenStat).where(
            MLBBullpenStat.team_id == team_id,
            MLBBullpenStat.season_id == season_id,
        )
    )
    stat = row.scalars().one_or_none()
    if stat is None:
        return None
    return {
        "era": stat.era,
        "whip": stat.whip,
        "fip": stat.fip,
        "saves": stat.saves,
        "blown_saves": stat.blown_saves,
        "innings_pitched": stat.innings_pitched,
        "strikeouts": stat.strikeouts,
        "walks": stat.walks,
        "k_per_9": round(stat.strikeouts / (stat.innings_pitched / 9), 2)
                   if stat.innings_pitched > 0 else None,
        "left_avg": stat.left_avg,
        "right_avg": stat.right_avg,
    }


# ──────────────────────────────────────────────
#  10. Team Splits
# ──────────────────────────────────────────────

async def get_team_splits(
    db: AsyncSession,
    team_id: int,
    season_id: int,
) -> dict[str, Any]:
    rows = await db.execute(
        select(MLBTeamSplit).where(
            MLBTeamSplit.team_id == team_id,
            MLBTeamSplit.season_id == season_id,
        )
    )
    result: dict[str, Any] = {}
    for s in rows.scalars().all():
        result[s.split_type] = {
            "games": s.games,
            "record": f"{s.wins}-{s.losses}",
            "runs_scored": s.runs_scored,
            "runs_allowed": s.runs_allowed,
            "avg": s.avg,
            "obp": s.obp,
            "slg": s.slg,
            "ops": s.ops,
            "era": s.era,
            "whip": s.whip,
        }
    return result


# ──────────────────────────────────────────────
#  11. Venue Info
# ──────────────────────────────────────────────

async def get_venue_info(
    db: AsyncSession,
    mlb_venue_id: int | None,
) -> dict[str, Any] | None:
    if mlb_venue_id is None:
        return None
    row = await db.execute(
        select(MLBVenue).where(MLBVenue.mlb_venue_id == mlb_venue_id)
    )
    venue = row.scalars().one_or_none()
    if venue is None:
        return None
    return {
        "name": venue.name,
        "city": venue.city,
        "capacity": venue.capacity,
        "surface": venue.surface,
        "roof_type": venue.roof_type,
        "dimensions": {
            "left_field": venue.left_field,
            "left_center": venue.left_center,
            "center_field": venue.center_field,
            "right_center": venue.right_center,
            "right_field": venue.right_field,
        },
        "altitude": venue.altitude,
        "park_factor": {"overall": venue.park_factor_overall, "home_runs": venue.park_factor_home_runs},
        "year_opened": venue.year_opened,
        "description": venue.description,
    }


# ──────────────────────────────────────────────
#  12. Game Weather
# ──────────────────────────────────────────────

async def get_game_weather(
    db: AsyncSession,
    game_id: int,
) -> dict[str, Any] | None:
    game = await db.get(MLBGames, game_id)
    if game is None:
        return None
    if not any([game.temperature, game.weather_condition, game.wind_speed]):
        return None
    return {
        "temperature": game.temperature,
        "condition": game.weather_condition,
        "wind_speed": game.wind_speed,
        "day_night": game.day_night,
        "roof_type": game.roof_type,
    }


# ──────────────────────────────────────────────
#  13. Standings
# ──────────────────────────────────────────────

async def get_standings(
    db: AsyncSession,
    season_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    dt_f = _dt_filter(as_of_date)

    rows = await db.execute(
        text(f"""
            WITH home AS (
                SELECT home_team_id AS tid,
                       COUNT(*) AS gp,
                       SUM(CASE WHEN home_score > away_score THEN 1 ELSE 0 END) AS w,
                       SUM(CASE WHEN home_score < away_score THEN 1 ELSE 0 END) AS l,
                       SUM(home_score) AS rs,
                       SUM(away_score) AS ra
                FROM mlb.games
                WHERE season_id = :sid AND status::text = :sfinal
                {dt_f.replace('g.', '')}
                GROUP BY home_team_id
            ),
            away AS (
                SELECT away_team_id AS tid,
                       COUNT(*) AS gp,
                       SUM(CASE WHEN away_score > home_score THEN 1 ELSE 0 END) AS w,
                       SUM(CASE WHEN away_score < home_score THEN 1 ELSE 0 END) AS l,
                       SUM(away_score) AS rs,
                       SUM(home_score) AS ra
                FROM mlb.games
                WHERE season_id = :sid AND status::text = :sfinal
                {dt_f.replace('g.', '')}
                GROUP BY away_team_id
            ),
            merged AS (
                SELECT COALESCE(h.tid, a.tid) AS tid,
                       COALESCE(h.gp, 0) + COALESCE(a.gp, 0) AS gp,
                       COALESCE(h.w, 0) + COALESCE(a.w, 0) AS w,
                       COALESCE(h.l, 0) + COALESCE(a.l, 0) AS l,
                       COALESCE(h.rs, 0) + COALESCE(a.rs, 0) AS rs,
                       COALESCE(h.ra, 0) + COALESCE(a.ra, 0) AS ra
                FROM home h FULL JOIN away a ON h.tid = a.tid
            )
            SELECT m.*, t.name, t.abbreviation, t.league, t.division
            FROM merged m
            JOIN mlb.teams t ON t.id = m.tid
            ORDER BY m.w DESC
        """),
        {"sid": season_id, "sfinal": _STATUS_FINAL},
    )

    standings = []
    for r in rows.mappings():
        standings.append({
            "team_id": r["tid"],
            "name": r["name"],
            "abbreviation": r["abbreviation"],
            "division": r["division"],
            "league": r["league"],
            "wins": r["w"] or 0,
            "losses": r["l"] or 0,
            "games_played": r["gp"] or 0,
            "runs_scored": r["rs"] or 0,
            "runs_allowed": r["ra"] or 0,
        })

    return {"standings": standings}


# ──────────────────────────────────────────────
#  14. Combined Research Brief
# ──────────────────────────────────────────────

async def get_research_brief(
    db: AsyncSession,
    game_id: int,
    as_of_date: Optional[datetime] = None,
) -> dict[str, Any]:
    """Gather ALL research data into one dict — the main entry point for
    the generation pipeline."""
    logger.info("Building research brief for game_id=%s (as_of=%s)", game_id, as_of_date)

    summary = await get_game_summary(db, game_id)
    if "error" in summary:
        return summary

    season_id = summary["season_id"]
    home_id = summary["home_team"]["id"]
    away_id = summary["away_team"]["id"]

    tasks = {
        "home_stats": get_team_season_stats(db, home_id, season_id, as_of_date),
        "away_stats": get_team_season_stats(db, away_id, season_id, as_of_date),
        "pitching_matchup": get_pitching_matchup(db, game_id, as_of_date),
        "betting_lines": get_betting_lines(db, game_id),
        "predictions": get_predictions(db, game_id),
        "head_to_head": get_head_to_head(db, home_id, away_id, season_id, as_of_date),
        "injuries": get_injuries(db, game_id),
        "home_form": get_recent_form(db, home_id, season_id, 10, as_of_date),
        "away_form": get_recent_form(db, away_id, season_id, 10, as_of_date),
        "home_splits": get_team_splits(db, home_id, season_id),
        "away_splits": get_team_splits(db, away_id, season_id),
        "home_bullpen": get_bullpen_stats(db, home_id, season_id),
        "away_bullpen": get_bullpen_stats(db, away_id, season_id),
        "venue": get_venue_info(db, summary.get("venue", {}).get("id")),
        "weather": get_game_weather(db, game_id),
        "standings": get_standings(db, season_id, as_of_date),
    }

    results: dict[str, Any] = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
        except Exception as e:
            logger.exception("Research '%s' failed for game %s", name, game_id)
            results[name] = {"error": str(e)}

    # ── Enrichment: vector search + DeepSeek summarization ──
    try:
        from app.writeups.enrichment import enrich_writeup_context

        home_team_name = summary.get("home_team", {}).get("name", "")
        away_team_name = summary.get("away_team", {}).get("name", "")

        # Extract starting pitcher names from the pitching_matchup result
        starting_pitchers = None
        pm = results.get("pitching_matchup", {})
        if isinstance(pm, dict) and "error" not in pm:
            sp_home = pm.get("home", {}).get("name", "")
            sp_away = pm.get("away", {}).get("name", "")
            if sp_home or sp_away:
                starting_pitchers = [sp_home, sp_away]

        game_dt = summary.get("game_datetime") or datetime.now(timezone.utc)
        if isinstance(game_dt, str):
            game_dt = datetime.fromisoformat(game_dt)

        if home_team_name and away_team_name:
            enrichment = await enrich_writeup_context(
                db=db,
                sport="mlb",
                home_team=home_team_name,
                away_team=away_team_name,
                game_date=game_dt,
                starting_pitchers=starting_pitchers,
                pitching_matchup=pm,
            )
            results["article_enrichment"] = enrichment
        else:
            results["article_enrichment"] = {"enriched_summary": "", "article_count": 0}

    except Exception as e:
        logger.warning("Article enrichment failed for game %s: %s", game_id, e)
        results["article_enrichment"] = {"enriched_summary": "", "article_count": 0}

    return {
        "game_summary": summary,
        **results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": as_of_date.isoformat() if as_of_date else None,
    }
