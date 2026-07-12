"""NFL research data collection — builds structured data for DeepSeek writeup generation.

Mirrors the MLB research module architecture with NFL-specific data sources.
"""
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("writeups.nfl.research")

_TZ_EASTERN = timezone(timedelta(hours=-4))


def _to_eastern(dt_tz):
    """Convert a datetime (aware or naive) to US/Eastern, treating naive as UTC."""
    if dt_tz is None:
        return None
    if dt_tz.tzinfo is None:
        dt_tz = dt_tz.replace(tzinfo=timezone.utc)
    return dt_tz.astimezone(_TZ_EASTERN)


def _fmt_local(dt_tz):
    """Format a timezone-aware datetime to a friendly US/Eastern string."""
    local = _to_eastern(dt_tz)
    if local is None:
        return None
    day_name = local.strftime("%a")
    month_day = local.strftime("%b %d")
    time_str = local.strftime("%I:%M %p").lstrip("0").replace(" 0", " ")
    return f"{month_day} ({day_name}) {time_str} ET"


def _season_year_sq(alias: str = "g") -> str:
    """Subquery to get season year from season_id."""
    return f"(SELECT year FROM nfl.seasons WHERE id = {alias}.season_id)"


# ──────────────────────────────────────────────
# GAME SUMMARY
# ──────────────────────────────────────────────


async def get_game_summary(db: AsyncSession, game_id: int) -> dict:
    """Basic game info: teams, venue, weather, date."""
    row = await db.execute(text("""
        SELECT
            g.id, g.week, g.game_type AS season_type, g.date, g.status,
            g.home_score, g.away_score,
            g.venue, g.roof_type, g.surface,
            g.temperature, g.wind_speed, g.weather_condition,
            ht.id AS home_team_id, ht.abbreviation AS home_abbr,
            ht.name AS home_name,
            at.id AS away_team_id, at.abbreviation AS away_abbr,
            at.name AS away_name,
            s.year AS season_year
        FROM nfl.games g
        JOIN nfl.teams ht ON g.home_team_id = ht.id
        JOIN nfl.teams at ON g.away_team_id = at.id
        JOIN nfl.seasons s ON g.season_id = s.id
        WHERE g.id = :game_id
    """), {"game_id": game_id})
    r = row.mappings().one_or_none()
    if not r:
        return {"error": f"Game {game_id} not found"}

    return {
        "game_id": r["id"],
        "season": r["season_year"],
        "week": r["week"],
        "season_type": r["season_type"],
        "date": _to_eastern(r["date"]).isoformat() if r["date"] else None,
        "formatted_time": _fmt_local(r["date"]),
        "status": r["status"],
        "home_team": {
            "id": r["home_team_id"],
            "abbr": r["home_abbr"],
            "name": r["home_name"],
        },
        "away_team": {
            "id": r["away_team_id"],
            "abbr": r["away_abbr"],
            "name": r["away_name"],
        },
        "score": {
            "home": r["home_score"],
            "away": r["away_score"],
        } if r["status"] == "FINAL" else None,
        "venue": r["venue"],
        "roof_type": r["roof_type"],
        "surface": r["surface"],
        "weather": {
            "temperature": r["temperature"],
            "wind_speed": r["wind_speed"],
            "condition": r["weather_condition"],
        } if r.get("roof_type") in ("outdoor", "open", None) else None,
    }


# ──────────────────────────────────────────────
# BETTING LINES & PREDICTIONS
# ──────────────────────────────────────────────


async def get_betting_lines(db: AsyncSession, game_id: int) -> dict:
    """Get consolidated betting lines and model predictions."""
    row = await db.execute(text("""
        SELECT
            bl.closing_spread AS spread,
            bl.closing_ou AS over_under,
            bl.closing_home_ml AS home_moneyline,
            bl.closing_away_ml AS away_moneyline,
            bl.opening_spread, bl.opening_ou,
            bl.opening_home_ml, bl.opening_away_ml,
            gp.predicted_home_score,
            gp.predicted_away_score,
            gp.margin_conf AS confidence,
            gp.spread_pick AS ats_pick,
            gp.ml_pick
        FROM nfl.betting_lines_consolidated bl
        LEFT JOIN nfl.game_predictions gp ON bl.game_id = gp.game_id
        WHERE bl.game_id = :game_id
    """), {"game_id": game_id})
    r = row.mappings().one_or_none()
    if not r:
        return {"error": "No betting data found"}

    result = {
        "spread": r["spread"],
        "over_under": r["over_under"],
        "away_moneyline": r["away_moneyline"],
        "home_moneyline": r["home_moneyline"],
        "line_movement": {},
    }

    # Line movement
    if r["opening_spread"] is not None and r["spread"] is not None:
        result["line_movement"]["spread"] = {
            "opened": r["opening_spread"],
            "current": r["spread"],
            "movement": round(r["spread"] - r["opening_spread"], 1),
        }
    if r["opening_ou"] is not None and r["over_under"] is not None:
        result["line_movement"]["over_under"] = {
            "opened": r["opening_ou"],
            "current": r["over_under"],
            "movement": round(r["over_under"] - r["opening_ou"], 1),
        }

    # Model predictions
    if r["predicted_home_score"] is not None:
        result["model_predictions"] = {
            "predicted_score": {
                "home": r["predicted_home_score"],
                "away": r["predicted_away_score"],
            },
            "ats_pick": r["ats_pick"],
            "confidence": r["confidence"],
            "ml_pick": r["ml_pick"],
        }

    return result


# ──────────────────────────────────────────────
# TEAM RECORDS
# ──────────────────────────────────────────────


async def get_team_records(db: AsyncSession, team_id: int, season_id: int,
                           as_of_date: Optional[date] = None) -> dict:
    """Compute W-L records (overall, home, away, division, conference)."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_id": team_id, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    row = await db.execute(text(f"""
        WITH team_games AS (
            SELECT g.id, g.date, g.home_team_id, g.away_team_id,
                   g.home_score, g.away_score,
                   ht.division AS home_div, ht.conference AS home_conf,
                   at.division AS away_div, at.conference AS away_conf
            FROM nfl.games g
            JOIN nfl.teams ht ON g.home_team_id = ht.id
            JOIN nfl.teams at ON g.away_team_id = at.id
            WHERE g.season_id = :season_id
              AND g.status = 'FINAL'
              {date_filter}
        )
        SELECT
            SUM(CASE WHEN g.home_team_id = :team_id OR g.away_team_id = :team_id THEN 1 ELSE 0 END) AS total,
            SUM(CASE
                WHEN (g.home_team_id = :team_id AND g.home_score > g.away_score)
                  OR (g.away_team_id = :team_id AND g.away_score > g.home_score)
                THEN 1 ELSE 0
            END) AS wins,
            SUM(CASE
                WHEN (g.home_team_id = :team_id AND g.home_score < g.away_score)
                  OR (g.away_team_id = :team_id AND g.away_score < g.home_score)
                THEN 1 ELSE 0
            END) AS losses,
            SUM(CASE WHEN g.home_team_id = :team_id THEN 1 ELSE 0 END) AS home_games,
            SUM(CASE WHEN g.home_team_id = :team_id AND g.home_score > g.away_score
                THEN 1 ELSE 0 END) AS home_wins,
            SUM(CASE WHEN g.away_team_id = :team_id THEN 1 ELSE 0 END) AS away_games,
            SUM(CASE WHEN g.away_team_id = :team_id AND g.away_score > g.home_score
                THEN 1 ELSE 0 END) AS away_wins,
            SUM(CASE WHEN (g.home_team_id = :team_id AND g.home_div = t.division)
                       OR (g.away_team_id = :team_id AND g.away_div = t.division)
                  THEN 1 ELSE 0 END) AS div_games,
            SUM(CASE WHEN (g.home_team_id = :team_id AND g.home_score > g.away_score
                           AND g.home_div = t.division)
                       OR (g.away_team_id = :team_id AND g.away_score > g.home_score
                           AND g.away_div = t.division)
                  THEN 1 ELSE 0 END) AS div_wins,
            SUM(CASE WHEN (g.home_team_id = :team_id AND g.home_conf = t.conference)
                       OR (g.away_team_id = :team_id AND g.away_conf = t.conference)
                  THEN 1 ELSE 0 END) AS conf_games,
            SUM(CASE WHEN (g.home_team_id = :team_id AND g.home_score > g.away_score
                           AND g.home_conf = t.conference)
                       OR (g.away_team_id = :team_id AND g.away_score > g.home_score
                           AND g.away_conf = t.conference)
                  THEN 1 ELSE 0 END) AS conf_wins
        FROM team_games g
        CROSS JOIN (SELECT division, conference FROM nfl.teams WHERE id = :team_id) t
    """), params)
    r = row.mappings().one()

    wins = r["wins"] or 0
    losses = r["losses"] or 0
    total = r["total"] or 0

    return {
        "overall": f"{wins}-{losses}",
        "win_pct": round(wins / total, 3) if total else 0,
        "home": f"{r['home_wins'] or 0}-{(r['home_games'] or 0) - (r['home_wins'] or 0)}",
        "away": f"{r['away_wins'] or 0}-{(r['away_games'] or 0) - (r['away_wins'] or 0)}",
        "division": f"{r['div_wins'] or 0}-{(r['div_games'] or 0) - (r['div_wins'] or 0)}",
        "conference": f"{r['conf_wins'] or 0}-{(r['conf_games'] or 0) - (r['conf_wins'] or 0)}",
        "games_played": total,
    }


# ──────────────────────────────────────────────
# TEAM SEASON STATS
# ──────────────────────────────────────────────


async def get_team_season_stats(db: AsyncSession, team_abbr: str, season_id: int,
                                as_of_date: Optional[date] = None) -> dict:
    """Compute per-game averages via game_stats (joined on season/week)."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_abbr": team_abbr, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    season_year = await db.execute(
        text("SELECT year FROM nfl.seasons WHERE id = :sid"),
        {"sid": season_id}
    )
    sy = season_year.scalar_one_or_none()
    params["season_year"] = sy

    row = await db.execute(text(f"""
        WITH joined AS (
            SELECT
                gs.*,
                g.date, g.home_score, g.away_score,
                g.home_team_id, g.away_team_id
            FROM nfl.game_stats gs
            JOIN nfl.games g ON gs.season = :season_year
                AND gs.week = g.week
                AND gs.season_type = g.game_type
            WHERE gs.team_abbr = :team_abbr
              AND g.season_id = :season_id
              {date_filter}
        )
        SELECT
            COUNT(*) AS games,
            ROUND(AVG(CASE WHEN team_abbr = (SELECT abbreviation FROM nfl.teams WHERE id = home_team_id)
                          THEN home_score ELSE away_score END::numeric), 1) AS ppg,
            ROUND(AVG(CASE WHEN team_abbr = (SELECT abbreviation FROM nfl.teams WHERE id = home_team_id)
                          THEN away_score ELSE home_score END::numeric), 1) AS oppg,
            ROUND(AVG(total_yards::numeric), 1) AS ypg,
            ROUND(AVG(pass_yards::numeric), 1) AS pass_ypg,
            ROUND(AVG(rush_yards::numeric), 1) AS rush_ypg,
            ROUND(AVG(pass_attempts::numeric), 1) AS pass_att_pg,
            ROUND(AVG(rush_attempts::numeric), 1) AS rush_att_pg,
            ROUND(AVG(pass_tds::numeric), 1) AS pass_td_pg,
            ROUND(AVG(rush_tds::numeric), 1) AS rush_td_pg,
            ROUND(AVG(pass_interceptions::numeric), 1) AS int_pg,
            ROUND(AVG(fumbles_lost::numeric), 1) AS fumbles_pg,
            ROUND(AVG(turnovers::numeric), 1) AS to_pg,
            ROUND(AVG(takeaways::numeric), 1) AS takeaways_pg,
            ROUND(AVG(sacks_suffered::numeric), 1) AS sacks_pg,
            ROUND(AVG(penalties::numeric), 1) AS penalties_pg
        FROM joined
    """), params)
    r = row.mappings().one()

    # Defensive stats from opponent perspective
    def_row = await db.execute(text(f"""
        SELECT
            ROUND(AVG(def_yards_allowed::numeric), 1) AS def_ypg,
            ROUND(AVG(def_pass_yards::numeric), 1) AS def_pass_ypg,
            ROUND(AVG(def_rush_yards::numeric), 1) AS def_rush_ypg,
            ROUND(AVG(def_interceptions::numeric), 1) AS def_int_pg,
            ROUND(AVG(def_sacks::numeric), 1) AS def_sacks_pg
        FROM nfl.game_stats gs
        JOIN nfl.games g ON gs.season = :season_year
            AND gs.week = g.week
            AND gs.season_type = g.game_type
        WHERE gs.team_abbr = :team_abbr
          AND g.season_id = :season_id
          {date_filter}
    """), params)
    d = def_row.mappings().one()

    return {
        "games_played": r["games"] or 0,
        "offense": {
            "ppg": r["ppg"] or 0,
            "ypg": r["ypg"] or 0,
            "pass_ypg": r["pass_ypg"] or 0,
            "rush_ypg": r["rush_ypg"] or 0,
            "pass_att_per_game": r["pass_att_pg"] or 0,
            "rush_att_per_game": r["rush_att_pg"] or 0,
            "pass_td_per_game": r["pass_td_pg"] or 0,
            "rush_td_per_game": r["rush_td_pg"] or 0,
            "int_per_game": r["int_pg"] or 0,
            "fumbles_per_game": r["fumbles_pg"] or 0,
            "turnovers_per_game": r["to_pg"] or 0,
            "sacks_per_game": r["sacks_pg"] or 0,
            "penalties_per_game": r["penalties_pg"] or 0,
        },
        "defense": {
            "oppg": r["oppg"] or 0,
            "def_ypg": d["def_ypg"] or 0,
            "def_pass_ypg": d["def_pass_ypg"] or 0,
            "def_rush_ypg": d["def_rush_ypg"] or 0,
            "def_int_per_game": d["def_int_pg"] or 0,
            "def_sacks_per_game": d["def_sacks_pg"] or 0,
        },
        "takeaways_per_game": r["takeaways_pg"] or 0,
        "turnover_diff_per_game": round((r["takeaways_pg"] or 0) - (r["to_pg"] or 0), 1),
    }


# ──────────────────────────────────────────────
# TEAM RANKINGS
# ──────────────────────────────────────────────


async def get_team_rankings(db: AsyncSession, team_abbr: str, season_id: int,
                            as_of_date: Optional[date] = None) -> dict:
    """Rank this team among all teams in key categories."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    team_stats = await db.execute(text(f"""
        WITH team_aggs AS (
            SELECT
                gs.team_abbr,
                ROUND(AVG(CASE WHEN gs.team_abbr = (SELECT abbreviation FROM nfl.teams WHERE id = g.home_team_id)
                              THEN g.home_score ELSE g.away_score END::numeric), 1) AS ppg,
                ROUND(AVG(gs.total_yards::numeric), 1) AS ypg,
                ROUND(AVG(gs.pass_yards::numeric), 1) AS pass_ypg,
                ROUND(AVG(gs.rush_yards::numeric), 1) AS rush_ypg,
                ROUND(AVG(CASE WHEN gs.team_abbr = (SELECT abbreviation FROM nfl.teams WHERE id = g.home_team_id)
                              THEN g.away_score ELSE g.home_score END::numeric), 1) AS oppg,
                ROUND(AVG(gs.def_yards_allowed::numeric), 1) AS def_ypg,
                ROUND(AVG(gs.def_pass_yards::numeric), 1) AS def_pass_ypg,
                ROUND(AVG(gs.def_rush_yards::numeric), 1) AS def_rush_ypg,
                ROUND(AVG(gs.turnover_diff::numeric), 1) AS to_diff_pg
            FROM nfl.game_stats gs
            JOIN nfl.games g ON gs.season = (SELECT year FROM nfl.seasons WHERE id = :season_id)
                AND gs.week = g.week
                AND gs.season_type = g.game_type
            WHERE g.season_id = :season_id
              {date_filter}
            GROUP BY gs.team_abbr
        )
        SELECT * FROM team_aggs ORDER BY ppg DESC
    """), params)
    all_stats = team_stats.mappings().fetchall()

    target = None
    for s in all_stats:
        if s["team_abbr"] == team_abbr:
            target = dict(s)
            break

    if not target:
        return {"error": f"No stats for {team_abbr}"}

    rankings = {}
    categories = [
        ("ppg", False), ("ypg", False), ("pass_ypg", False), ("rush_ypg", False),
        ("oppg", True), ("def_ypg", True), ("def_pass_ypg", True), ("def_rush_ypg", True),
        ("to_diff_pg", False),
    ]
    for cat, lowest_is_best in categories:
        sorted_list = sorted(
            all_stats,
            key=lambda x: (x[cat] or 0) if not lowest_is_best else -(x[cat] or 0),
            reverse=not lowest_is_best,
        )
        rank = next(i + 1 for i, s in enumerate(sorted_list) if s["team_abbr"] == team_abbr)
        rankings[cat] = {
            "rank": rank,
            "total": len(all_stats),
            "value": target[cat],
        }

    return rankings


# ──────────────────────────────────────────────
# QB PROFILE
# ──────────────────────────────────────────────


async def get_qb_profile(db: AsyncSession, team_abbr: str, team_id: int,
                         season_id: int, as_of_date: Optional[date] = None) -> Optional[dict]:
    """Get the starting QB's season stats and recent game log."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_id": team_id, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    qb_row = await db.execute(text(f"""
        SELECT pws.player_id, p.name
        FROM nfl.player_weekly_stats pws
        JOIN nfl.players p ON pws.player_id = p.id
        JOIN nfl.games g ON pws.game_id = g.id
        WHERE pws.team_id = :team_id
          AND g.season_id = :season_id
          AND g.game_type = 'REG'
          AND p.position = 'QB'
          {date_filter}
        GROUP BY pws.player_id, p.name
        ORDER BY SUM(COALESCE(pws.pass_attempts, 0)) DESC
        LIMIT 1
    """), params)
    qb = qb_row.mappings().one_or_none()
    if not qb:
        return None

    player_id = qb["player_id"]
    qb_name = qb["name"]

    # Season totals
    season_row = await db.execute(text(f"""
        SELECT
            SUM(COALESCE(pass_yards, 0)) AS pass_yds,
            SUM(COALESCE(pass_tds, 0)) AS pass_td,
            SUM(COALESCE(pass_int, 0)) AS pass_int,
            SUM(COALESCE(pass_attempts, 0)) AS pass_att,
            SUM(COALESCE(pass_completions, 0)) AS pass_cmp,
            SUM(COALESCE(rush_yards, 0)) AS rush_yds,
            SUM(COALESCE(rush_tds, 0)) AS rush_td,
            COUNT(*) AS games
        FROM nfl.player_weekly_stats pws
        JOIN nfl.games g ON pws.game_id = g.id
        WHERE pws.player_id = :player_id
          AND g.season_id = :season_id
          AND g.game_type = 'REG'
          {date_filter}
    """), {"player_id": player_id, "season_id": season_id, **(params if as_of_date else {})})
    s = season_row.mappings().one()

    att = s["pass_att"] or 0
    comp_pct = round((s["pass_cmp"] or 0) / att * 100, 1) if att else 0
    ypa = round((s["pass_yds"] or 0) / att, 1) if att else 0
    rate = _compute_qb_rating(s["pass_cmp"] or 0, att, s["pass_yds"] or 0,
                              s["pass_td"] or 0, s["pass_int"] or 0)

    # Recent 5 games
    recent_rows = await db.execute(text(f"""
        SELECT g.week, g.date,
               ht.abbreviation AS home, at.abbreviation AS away,
               pws.pass_yards, pws.pass_tds, pws.pass_int,
               pws.pass_attempts, pws.pass_completions,
               pws.rush_yards, pws.rush_tds,
               g.home_score, g.away_score
        FROM nfl.player_weekly_stats pws
        JOIN nfl.games g ON pws.game_id = g.id
        JOIN nfl.teams ht ON g.home_team_id = ht.id
        JOIN nfl.teams at ON g.away_team_id = at.id
        WHERE pws.player_id = :player_id
          AND g.season_id = :season_id
          AND g.game_type = 'REG'
        ORDER BY g.date DESC
        LIMIT 5
    """), {"player_id": player_id, "season_id": season_id})

    recent = []
    for rr in recent_rows.mappings():
        is_home = rr["home"] == team_abbr
        won = (is_home and rr["home_score"] > rr["away_score"]) or \
              (not is_home and rr["away_score"] > rr["home_score"])
        recent.append({
            "week": rr["week"],
            "date": _to_eastern(rr["date"]).isoformat() if rr["date"] else None,
            "opponent": rr["away"] if is_home else rr["home"],
            "result": "W" if won else "L",
            "score": f"{rr['away_score']}-{rr['home_score']}",
            "pass_yds": rr["pass_yards"],
            "pass_td": rr["pass_tds"],
            "pass_int": rr["pass_int"],
            "comp_pct": round(rr["pass_completions"] / rr["pass_attempts"] * 100, 1) if (rr["pass_attempts"] or 0) > 0 else 0,
            "rush_yds": rr["rush_yards"],
            "rush_td": rr["rush_tds"],
        })

    return {
        "name": qb_name,
        "player_id": player_id,
        "season_stats": {
            "games": s["games"] or 0,
            "pass_yds": s["pass_yds"] or 0,
            "pass_td": s["pass_td"] or 0,
            "pass_int": s["pass_int"] or 0,
            "pass_att": att,
            "pass_cmp": s["pass_cmp"] or 0,
            "comp_pct": comp_pct,
            "ypa": ypa,
            "qb_rating": rate,
            "rush_yds": s["rush_yds"] or 0,
            "rush_td": s["rush_td"] or 0,
            "yds_per_game": round((s["pass_yds"] or 0) / max(s["games"] or 1, 1), 1),
        },
        "recent_games": recent,
    }


def _compute_qb_rating(cmp, att, yds, td, inter):
    """NFL passer rating formula."""
    if not att or att == 0:
        return 0
    a = max(min(((cmp / att) - 0.3) * 5, 2.375), 0)
    b = max(min(((yds / att) - 3) * 0.25, 2.375), 0)
    c = max(min(((td / att) * 100) * 0.2, 2.375), 0)
    d = max(min(2.375 - ((inter / att) * 100) * 0.25, 2.375), 0)
    return round(((a + b + c + d) / 6) * 100, 1)


# ──────────────────────────────────────────────
# KEY SKILL PLAYERS
# ──────────────────────────────────────────────


async def get_key_skill_players(db: AsyncSession, team_id: int, team_abbr: str,
                                season_id: int, as_of_date: Optional[date] = None) -> list:
    """Get top WRs, RBs, and TEs with season totals and recent games."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_id": team_id, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    players = await db.execute(text(f"""
        SELECT pws.player_id, p.name, p.position,
               SUM(COALESCE(pws.rush_yards, 0)) AS rush_yds,
               SUM(COALESCE(pws.rush_tds, 0)) AS rush_td,
               SUM(COALESCE(pws.rush_attempts, 0)) AS rush_att,
               SUM(COALESCE(pws.receiving_yards, 0)) AS recv_yds,
               SUM(COALESCE(pws.receiving_tds, 0)) AS recv_td,
               SUM(COALESCE(pws.receptions, 0)) AS receptions,
               SUM(COALESCE(pws.targets, 0)) AS targets,
               COUNT(DISTINCT g.week) AS games
        FROM nfl.player_weekly_stats pws
        JOIN nfl.players p ON pws.player_id = p.id
        JOIN nfl.games g ON pws.game_id = g.id
        WHERE pws.team_id = :team_id
          AND g.season_id = :season_id
          AND g.game_type = 'REG'
          {date_filter}
        GROUP BY pws.player_id, p.name, p.position
        HAVING SUM(COALESCE(pws.receptions, 0) + COALESCE(pws.rush_attempts, 0)) > 5
        ORDER BY
            CASE p.position
                WHEN 'WR' THEN SUM(COALESCE(pws.receiving_yards, 0))
                WHEN 'RB' THEN SUM(COALESCE(pws.rush_yards, 0))
                WHEN 'TE' THEN SUM(COALESCE(pws.receiving_yards, 0))
                ELSE 0
            END DESC
        LIMIT 10
    """), params)

    result = []
    for p in players.mappings():
        recent = await db.execute(text(f"""
            SELECT g.week,
                   pws.rush_yards, pws.rush_tds, pws.rush_attempts,
                   pws.receiving_yards, pws.receiving_tds, pws.receptions, pws.targets
            FROM nfl.player_weekly_stats pws
            JOIN nfl.games g ON pws.game_id = g.id
            WHERE pws.player_id = :player_id
              AND g.season_id = :season_id
              AND g.game_type = 'REG'
              {date_filter}
            ORDER BY g.date DESC
            LIMIT 3
        """), {"player_id": p["player_id"], "season_id": season_id, **(params if as_of_date else {})})

        recent_games = []
        for r in recent.mappings():
            recent_games.append({
                "week": r["week"],
                "rush_yds": r["rush_yards"],
                "rush_td": r["rush_tds"],
                "rush_att": r["rush_attempts"],
                "recv_yds": r["receiving_yards"],
                "recv_td": r["receiving_tds"],
                "receptions": r["receptions"],
                "targets": r["targets"],
            })

        result.append({
            "name": p["name"],
            "position": p["position"],
            "season_totals": {
                "games": p["games"],
                "rush_yds": p["rush_yds"] or 0,
                "rush_td": p["rush_td"] or 0,
                "rush_att": p["rush_att"] or 0,
                "recv_yds": p["recv_yds"] or 0,
                "recv_td": p["recv_td"] or 0,
                "receptions": p["receptions"] or 0,
                "targets": p["targets"] or 0,
            },
            "avg_per_game": {
                "rush_yds": round((p["rush_yds"] or 0) / max(p["games"], 1), 1),
                "recv_yds": round((p["recv_yds"] or 0) / max(p["games"], 1), 1),
                "receptions": round((p["receptions"] or 0) / max(p["games"], 1), 1),
                "yards_per_carry": round((p["rush_yds"] or 0) / max(p["rush_att"] or 1, 1), 1),
                "yards_per_reception": round((p["recv_yds"] or 0) / max(p["receptions"] or 1, 1), 1),
            },
            "recent_games": recent_games,
        })

    return result


# ──────────────────────────────────────────────
# RECENT FORM
# ──────────────────────────────────────────────


async def get_recent_form(db: AsyncSession, team_id: int, team_abbr: str,
                          season_id: int, limit: int = 5,
                          as_of_date: Optional[date] = None) -> list:
    """Last N games for a team with scores."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_id": team_id, "season_id": season_id, "limit": limit}
    if as_of_date:
        params["as_of_date"] = as_of_date

    rows = await db.execute(text(f"""
        SELECT g.id, g.week, g.date, g.game_type AS season_type,
               g.home_score, g.away_score,
               ht.abbreviation AS home, at.abbreviation AS away
        FROM nfl.games g
        JOIN nfl.teams ht ON g.home_team_id = ht.id
        JOIN nfl.teams at ON g.away_team_id = at.id
        WHERE (g.home_team_id = :team_id OR g.away_team_id = :team_id)
          AND g.season_id = :season_id
          AND g.status = 'FINAL'
          {date_filter}
        ORDER BY g.date DESC
        LIMIT :limit
    """), params)

    result = []
    for r in rows.mappings():
        is_home = r["home"] == team_abbr
        team_score = r["home_score"] if is_home else r["away_score"]
        opp_score = r["away_score"] if is_home else r["home_score"]
        opponent = r["away"] if is_home else r["home"]
        won = (team_score or 0) > (opp_score or 0)

        result.append({
            "week": r["week"],
            "date": _to_eastern(r["date"]).isoformat() if r["date"] else None,
            "location": "home" if is_home else "away",
            "opponent": opponent,
            "result": "W" if won else "L",
            "score": f"{team_score}-{opp_score}",
        })

    return result


# ──────────────────────────────────────────────
# HEAD-TO-HEAD
# ──────────────────────────────────────────────


async def get_head_to_head(db: AsyncSession, team1_abbr: str, team2_abbr: str,
                           limit: int = 10) -> dict:
    """Recent matchup history between two teams."""
    rows = await db.execute(text("""
        SELECT g.id, g.week, g.date, g.game_type AS season_type,
               g.home_score, g.away_score,
               ht.abbreviation AS home, at.abbreviation AS away,
               g.status, s.year AS season
        FROM nfl.games g
        JOIN nfl.teams ht ON g.home_team_id = ht.id
        JOIN nfl.teams at ON g.away_team_id = at.id
        JOIN nfl.seasons s ON g.season_id = s.id
        WHERE ((ht.abbreviation = :t1 AND at.abbreviation = :t2)
               OR (ht.abbreviation = :t2 AND at.abbreviation = :t1))
          AND g.status = 'FINAL'
        ORDER BY g.date DESC
        LIMIT :limit
    """), {"t1": team1_abbr, "t2": team2_abbr, "limit": limit})
    games_list = []
    t1_wins = 0
    t2_wins = 0
    for r in rows.mappings():
        t1_is_home = r["home"] == team1_abbr
        t1_score = r["home_score"] if t1_is_home else r["away_score"]
        t2_score = r["away_score"] if t1_is_home else r["home_score"]
        if (t1_score or 0) > (t2_score or 0):
            t1_wins += 1
        else:
            t2_wins += 1

        games_list.append({
            "season": r["season"],
            "week": r["week"],
            "date": _to_eastern(r["date"]).isoformat() if r["date"] else None,
            "venue": r["home"],
            "winner": team1_abbr if (t1_score or 0) > (t2_score or 0) else team2_abbr,
            "score": f"{t1_score}-{t2_score}",
        })

    return {
        "team1": team1_abbr,
        "team2": team2_abbr,
        "team1_wins": t1_wins,
        "team2_wins": t2_wins,
        "total_games": len(games_list),
        "games": games_list,
    }


# ──────────────────────────────────────────────
# INJURY REPORT
# ──────────────────────────────────────────────


async def get_injury_report(db: AsyncSession, game_id: int, home_team_id: int,
                            away_team_id: int, as_of_date: Optional[date] = None) -> dict:
    """Key injuries for both teams by matching players -> season/week."""
    # Get game week and season_id
    game_info = await db.execute(text("""
        SELECT week, season_id, date FROM nfl.games WHERE id = :gid
    """), {"gid": game_id})
    gi = game_info.mappings().one_or_none()
    if not gi:
        return {"home": [], "away": []}

    week = gi["week"]
    season_id = gi["season_id"]
    game_date = gi["date"]

    # Get injuries for both teams — link via players table
    for team_id, label in [(home_team_id, "home"), (away_team_id, "away")]:
        if as_of_date:
            rows = await db.execute(text("""
                SELECT p.name, p.position,
                       i.injury_type, i.practice_status, i.game_status
                FROM nfl.injuries i
                JOIN nfl.players p ON i.player_id = p.id
                WHERE p.team_id = :team_id
                  AND i.season_id = :season_id
                  AND i.week = :week
            """), {"team_id": team_id, "season_id": season_id, "week": week})
        else:
            rows = await db.execute(text("""
                SELECT p.name, p.position,
                       i.injury_type, i.practice_status, i.game_status
                FROM nfl.injuries i
                JOIN nfl.players p ON i.player_id = p.id
                WHERE p.team_id = :team_id
                  AND i.season_id = :season_id
                  AND i.week = :week
            """), {"team_id": team_id, "season_id": season_id, "week": week})

    # Rebuild with proper structure
    result = {"home": [], "away": []}
    for team_id, label in [(home_team_id, "home"), (away_team_id, "away")]:
        inj_rows = await db.execute(text("""
            SELECT p.name, p.position,
                   i.injury_type, i.practice_status, i.game_status
            FROM nfl.injuries i
            JOIN nfl.players p ON i.player_id = p.id
            WHERE p.team_id = :team_id
              AND i.season_id = :season_id
              AND i.week = :week
            ORDER BY
                CASE i.game_status
                    WHEN 'Out' THEN 1 WHEN 'Doubtful' THEN 2
                    WHEN 'Questionable' THEN 3 ELSE 4
                END,
                CASE p.position
                    WHEN 'QB' THEN 1 WHEN 'RB' THEN 2 WHEN 'WR' THEN 3
                    WHEN 'TE' THEN 4 WHEN 'OL' THEN 5 WHEN 'DL' THEN 6
                    WHEN 'LB' THEN 7 WHEN 'DB' THEN 8 ELSE 9
                END
        """), {"team_id": team_id, "season_id": season_id, "week": week})

        for r in inj_rows.mappings():
            result[label].append({
                "player": r["name"],
                "position": r["position"],
                "injury": r["injury_type"],
                "practice_status": r["practice_status"],
                "game_status": r["game_status"],
            })

    return result


# ──────────────────────────────────────────────
# SITUATIONAL CONTEXT
# ──────────────────────────────────────────────


async def get_situational_context(db: AsyncSession, home_team_id: int, away_team_id: int,
                                  game_date: datetime, team_abbr_home: str,
                                  team_abbr_away: str, season_id: int) -> dict:
    """Rest days, travel, division game, dome/outdoor context."""
    last_games = await db.execute(text("""
        SELECT
            MAX(CASE WHEN home_team_id = :home_id OR away_team_id = :home_id THEN date END) AS home_last,
            MAX(CASE WHEN home_team_id = :away_id OR away_team_id = :away_id THEN date END) AS away_last
        FROM nfl.games
        WHERE (home_team_id IN (:home_id, :away_id) OR away_team_id IN (:home_id, :away_id))
          AND date < :game_date
          AND status = 'FINAL'
    """), {
        "home_id": home_team_id, "away_id": away_team_id,
        "game_date": game_date,
    })
    lg = last_games.mappings().one()
    home_last = lg["home_last"]
    away_last = lg["away_last"]

    home_rest = (game_date - home_last).days if home_last else None
    away_rest = (game_date - away_last).days if away_last else None

    teams = await db.execute(text("""
        SELECT id, abbreviation, conference, division FROM nfl.teams
        WHERE id IN (:home_id, :away_id)
    """), {"home_id": home_team_id, "away_id": away_team_id})
    team_info = {r["id"]: dict(r) for r in teams.mappings()}
    home_info = team_info.get(home_team_id, {})
    away_info = team_info.get(away_team_id, {})

    is_division = home_info.get("division") == away_info.get("division") and home_info.get("division") is not None
    is_conference = home_info.get("conference") == away_info.get("conference") and home_info.get("conference") is not None

    # Roof type from games table
    game_info = await db.execute(text("""
        SELECT roof_type, venue FROM nfl.games WHERE home_team_id = :home_id
          AND away_team_id = :away_id AND season_id = :sid
        ORDER BY date DESC LIMIT 1
    """), {"home_id": home_team_id, "away_id": away_team_id, "sid": season_id})
    gi = game_info.mappings().one_or_none()

    return {
        "home_team": {
            "rest_days": home_rest,
            "short_week": home_rest is not None and home_rest <= 4,
            "division": home_info.get("division"),
            "conference": home_info.get("conference"),
        },
        "away_team": {
            "rest_days": away_rest,
            "short_week": away_rest is not None and away_rest <= 4,
            "division": away_info.get("division"),
            "conference": away_info.get("conference"),
        },
        "is_division_game": is_division,
        "is_conference_game": is_conference,
        "roof_type": gi["roof_type"] if gi else None,
        "venue": gi["venue"] if gi else None,
    }


# ──────────────────────────────────────────────
# TEAM PACE
# ──────────────────────────────────────────────


async def get_team_pace(db: AsyncSession, team_id: int, team_abbr: str,
                          season_id: int, as_of_date: Optional[date] = None) -> Optional[dict]:
    """Compute offensive pace and play-calling tendency from game_stats."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"team_abbr": team_abbr, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    row = await db.execute(text(f"""
        SELECT
            ROUND(AVG(COALESCE(pass_attempts, 0) + COALESCE(rush_attempts, 0))::numeric, 1) AS plays_per_game,
            ROUND(AVG(COALESCE(pass_attempts, 0))::numeric, 1) AS pass_attempts_pg,
            ROUND(AVG(COALESCE(rush_attempts, 0))::numeric, 1) AS rush_attempts_pg,
            ROUND((AVG(COALESCE(pass_attempts, 0))::numeric / NULLIF(AVG(COALESCE(pass_attempts, 0) + COALESCE(rush_attempts, 0)), 0)) * 100, 1) AS pass_pct
        FROM nfl.game_stats gs
        JOIN nfl.games g ON gs.season = (SELECT year FROM nfl.seasons WHERE id = :season_id)
            AND gs.week = g.week
            AND gs.season_type = g.game_type
        WHERE gs.team_abbr = :team_abbr
          AND g.season_id = :season_id
          {date_filter}
    """), params)
    r = row.mappings().one_or_none()
    if not r or r["plays_per_game"] is None:
        return None
    return {
        "plays_per_game": r["plays_per_game"],
        "pass_attempts_per_game": r["pass_attempts_pg"],
        "rush_attempts_per_game": r["rush_attempts_pg"],
        "pass_play_pct": float(r["pass_pct"]) if r["pass_pct"] else None,
        "rush_play_pct": round(100 - float(r["pass_pct"]), 1) if r["pass_pct"] else None,
    }


# ──────────────────────────────────────────────
# DEFENSIVE MATCHUP
# ──────────────────────────────────────────────


async def get_defensive_matchup(db: AsyncSession, offense_abbr: str, defense_abbr: str,
                                season_id: int, as_of_date: Optional[date] = None) -> Optional[dict]:
    """How the defense matches up against the offense's strengths."""
    date_filter = " AND g.date < :as_of_date" if as_of_date else ""
    params = {"off_abbr": offense_abbr, "def_abbr": defense_abbr, "season_id": season_id}
    if as_of_date:
        params["as_of_date"] = as_of_date

    off = await db.execute(text(f"""
        SELECT
            ROUND(AVG(gs.pass_yards::numeric), 1) AS pass_ypg,
            ROUND(AVG(gs.rush_yards::numeric), 1) AS rush_ypg,
            ROUND(AVG(gs.pass_attempts::numeric), 1) AS pass_att_pg,
            ROUND(AVG(gs.rush_attempts::numeric), 1) AS rush_att_pg,
            ROUND(AVG(gs.total_yards::numeric), 1) AS ypg
        FROM nfl.game_stats gs
        JOIN nfl.games g ON gs.season = (SELECT year FROM nfl.seasons WHERE id = :season_id)
            AND gs.week = g.week
            AND gs.season_type = g.game_type
        WHERE gs.team_abbr = :off_abbr
          AND g.season_id = :season_id
          {date_filter}
    """), params)
    off_stats = off.mappings().one()

    defe = await db.execute(text(f"""
        SELECT
            ROUND(AVG(gs.def_pass_yards::numeric), 1) AS def_pass_ypg,
            ROUND(AVG(gs.def_rush_yards::numeric), 1) AS def_rush_ypg,
            ROUND(AVG(gs.def_yards_allowed::numeric), 1) AS def_ypg,
            ROUND(AVG(gs.def_sacks::numeric), 1) AS sacks_pg,
            ROUND(AVG(gs.def_interceptions::numeric), 1) AS int_pg
        FROM nfl.game_stats gs
        JOIN nfl.games g ON gs.season = (SELECT year FROM nfl.seasons WHERE id = :season_id)
            AND gs.week = g.week
            AND gs.season_type = g.game_type
        WHERE gs.team_abbr = :def_abbr
          AND g.season_id = :season_id
          {date_filter}
    """), params)
    def_stats = defe.mappings().one()

    if not off_stats["ypg"] or not def_stats["def_ypg"]:
        return None

    return {
        "offense_vs_defense": {
            "off_pass_ypg": off_stats["pass_ypg"],
            "def_pass_allowed": def_stats["def_pass_ypg"],
            "pass_advantage": round((off_stats["pass_ypg"] or 0) - (def_stats["def_pass_ypg"] or 0), 1),
            "off_rush_ypg": off_stats["rush_ypg"],
            "def_rush_allowed": def_stats["def_rush_ypg"],
            "run_advantage": round((off_stats["rush_ypg"] or 0) - (def_stats["def_rush_ypg"] or 0), 1),
        },
        "offense_tendency": {
            "pass_ypg_pct": round((off_stats["pass_ypg"] or 0) / (off_stats["ypg"] or 1) * 100, 1),
            "rush_ypg_pct": round((off_stats["rush_ypg"] or 0) / (off_stats["ypg"] or 1) * 100, 1),
            "pass_att_pg": off_stats["pass_att_pg"],
            "rush_att_pg": off_stats["rush_att_pg"],
        },
        "defense_strength": {
            "sacks_pg": def_stats["sacks_pg"],
            "int_pg": def_stats["int_pg"],
        },
    }


# ──────────────────────────────────────────────
# RESEARCH BRIEF (full — for premium writeups)
# ──────────────────────────────────────────────


async def get_research_brief(db: AsyncSession, game_id: int,
                             as_of_date: Optional[date] = None) -> dict:
    """Compile the full NFL research brief for DeepSeek premium writeups."""
    game = await get_game_summary(db, game_id)
    if "error" in game:
        return game

    sid = await db.execute(
        text("SELECT id FROM nfl.seasons WHERE year = :year"),
        {"year": game["season"]}
    )
    srow = sid.scalar_one_or_none()
    if not srow:
        return {"error": f"Season {game['season']} not found"}
    season_id = srow

    home_abbr = game["home_team"]["abbr"]
    away_abbr = game["away_team"]["abbr"]
    home_id = game["home_team"]["id"]
    away_id = game["away_team"]["id"]
    game_date = game["date"]

    import asyncio

    tasks = {
        "betting": get_betting_lines(db, game_id),
        "home_rec": get_team_records(db, home_id, season_id, as_of_date),
        "away_rec": get_team_records(db, away_id, season_id, as_of_date),
        "home_stats": get_team_season_stats(db, home_abbr, season_id, as_of_date),
        "away_stats": get_team_season_stats(db, away_abbr, season_id, as_of_date),
        "home_rank": get_team_rankings(db, home_abbr, season_id, as_of_date),
        "away_rank": get_team_rankings(db, away_abbr, season_id, as_of_date),
        "home_qb": get_qb_profile(db, home_abbr, home_id, season_id, as_of_date),
        "away_qb": get_qb_profile(db, away_abbr, away_id, season_id, as_of_date),
        "home_players": get_key_skill_players(db, home_id, home_abbr, season_id, as_of_date),
        "away_players": get_key_skill_players(db, away_id, away_abbr, season_id, as_of_date),
        "home_form": get_recent_form(db, home_id, home_abbr, season_id, 5, as_of_date),
        "away_form": get_recent_form(db, away_id, away_abbr, season_id, 5, as_of_date),
        "h2h": get_head_to_head(db, home_abbr, away_abbr, 10),
        "injuries": get_injury_report(db, game_id, home_id, away_id, as_of_date),
        "situ": get_situational_context(db, home_id, away_id,
                                         datetime.fromisoformat(game_date) if game_date else datetime.now(timezone.utc),
                                         home_abbr, away_abbr, season_id),
        "home_pace": get_team_pace(db, home_id, home_abbr, season_id, as_of_date),
        "away_pace": get_team_pace(db, away_id, away_abbr, season_id, as_of_date),
        "home_def_matchup": get_defensive_matchup(db, away_abbr, home_abbr, season_id, as_of_date),
        "away_def_matchup": get_defensive_matchup(db, home_abbr, away_abbr, season_id, as_of_date),
    }

    results = {}
    for name, coro in tasks.items():
        try:
            results[name] = await coro
        except Exception as e:
            logger.warning("NFL research brief task %s failed: %s", name, e)
            results[name] = {}

    return {
        "game_info": game,
        "betting_lines": results.get("betting"),
        "teams": {
            "home": {
                "record": results.get("home_rec"),
                "season_stats": results.get("home_stats"),
                "rankings": results.get("home_rank"),
                "qb": results.get("home_qb"),
                "key_players": results.get("home_players"),
                "recent_form": results.get("home_form"),
                "pace": results.get("home_pace"),
            },
            "away": {
                "record": results.get("away_rec"),
                "season_stats": results.get("away_stats"),
                "rankings": results.get("away_rank"),
                "qb": results.get("away_qb"),
                "key_players": results.get("away_players"),
                "recent_form": results.get("away_form"),
                "pace": results.get("away_pace"),
            },
        },
        "head_to_head": results.get("h2h"),
        "injuries": results.get("injuries"),
        "situational": results.get("situ"),
        "defensive_matchups": {
            "home_defense_vs_away_offense": results.get("home_def_matchup"),
            "away_defense_vs_home_offense": results.get("away_def_matchup"),
        },
    }


# ──────────────────────────────────────────────
# PUBLIC RESEARCH BRIEF
# ──────────────────────────────────────────────


async def get_public_research_brief(db: AsyncSession, game_id: int,
                                    as_of_date: Optional[date] = None) -> dict:
    """Stripped research brief for public writeups — no model picks."""
    brief = await get_research_brief(db, game_id, as_of_date)
    if "error" in brief:
        return brief

    if "betting_lines" in brief and isinstance(brief["betting_lines"], dict):
        brief["betting_lines"].pop("model_predictions", None)

    if "teams" in brief:
        for side in ("home", "away"):
            if side in brief["teams"]:
                brief["teams"][side].pop("pace", None)

    return brief
