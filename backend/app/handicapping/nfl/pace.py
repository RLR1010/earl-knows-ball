"""
Pace analysis module for NFL handicapping.

Turns team snap-count data into over/under adjustments.

Key insight: teams that run more offensive plays create more scoring
opportunities, pushing totals higher. The pace adjustment quantifies
this and feeds it into the over/under prediction.

Pace metrics used:
  - Offensive snaps per game (team offensive plays)
  - Defensive snaps per game (defensive plays faced)
  - Total snaps per game

Each additional offensive snap is worth ~0.25 expected points (league avg).
Pace is most useful when a fast offense faces a slow defense (or vice versa).
"""
import logging
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TeamPaceStats, Team, Season, Game

logger = logging.getLogger("earl.handicapping.pace")

# League-average points per offensive play (approximate for modern NFL)
# Each offensive play generates ~0.4 expected points
PTS_PER_OFFENSIVE_PLAY = 0.40
# League-average points allowed per defensive snap
PTS_PER_DEFENSIVE_SNAP = 0.23


async def get_league_pace_baseline(
    db: AsyncSession,
    season_year: int,
    max_week: int | None = None,
) -> dict:
    """
    Get league-wide average pace metrics for a season.

    Returns averages for offensive and defensive snaps per game.
    """
    season_r = await db.execute(select(Season).where(Season.year == season_year))
    season = season_r.scalar_one_or_none()
    if not season:
        return {"off_snaps_avg": 62.0, "def_snaps_avg": 62.0}

    query = select(
        func.avg(TeamPaceStats.offensive_snaps),
        func.avg(TeamPaceStats.defensive_snaps),
    ).where(
        TeamPaceStats.season_id == season.id,
        TeamPaceStats.season_type == "REG",
    )

    if max_week:
        query = query.where(TeamPaceStats.week < max_week)

    r = await db.execute(query)
    row = r.one()

    off_avg = round(float(row[0] or 62.0), 1)
    def_avg = round(float(row[1] or 62.0), 1)

    return {
        "off_snaps_avg": off_avg,
        "def_snaps_avg": def_avg,
        "total_snaps_avg": round((off_avg + def_avg) / 2, 1),
    }


async def get_team_pace_profile(
    db: AsyncSession,
    team_abbr: str,
    season_year: int,
    num_games: int = 5,
    max_week: int | None = None,
) -> dict | None:
    """
    Get a team's pace profile: average snaps per game over recent games.

    Returns offensive, defensive, and total snap averages.
    """
    season_r = await db.execute(select(Season).where(Season.year == season_year))
    season = season_r.scalar_one_or_none()
    if not season:
        return None

    team_r = await db.execute(
        select(Team).where(Team.abbreviation == team_abbr.upper())
    )
    team = team_r.scalar_one_or_none()
    if not team:
        return None

    conditions = [
        TeamPaceStats.season_id == season.id,
        TeamPaceStats.team_id == team.id,
        TeamPaceStats.season_type == "REG",
    ]
    if max_week:
        conditions.append(TeamPaceStats.week < max_week)

    r = await db.execute(
        select(TeamPaceStats)
        .where(*conditions)
        .order_by(TeamPaceStats.week.desc())
    )
    rows = list(r.scalars().all())

    if not rows:
        return None

    # Use last N games
    if num_games:
        rows = rows[:num_games]

    off_snaps = [r.offensive_snaps for r in rows]
    def_snaps = [r.defensive_snaps for r in rows]
    tot_snaps = [r.total_snaps for r in rows]

    return {
        "team": team_abbr.upper(),
        "games_analyzed": len(rows),
        "off_snaps_avg": round(sum(off_snaps) / len(off_snaps), 1),
        "def_snaps_avg": round(sum(def_snaps) / len(def_snaps), 1),
        "total_snaps_avg": round(sum(tot_snaps) / len(tot_snaps), 1),
        "min_off_snaps": min(off_snaps),
        "max_off_snaps": max(off_snaps),
        "min_def_snaps": min(def_snaps),
        "max_def_snaps": max(def_snaps),
    }


async def compute_pace_adjustment(
    db: AsyncSession,
    season_year: int,
    home_abbr: str,
    away_abbr: str,
    num_games: int = 5,
    max_week: int | None = None,
) -> dict:
    """
    Compute pace-based over/under adjustment for a matchup.

    Returns:
        adjustment_pts: Points to add/subtract from predicted total
        home_pace: Home team pace profile
        away_pace: Away team pace profile
        league_baseline: League average snap counts
        pace_tempo: 'fast', 'slow', or 'neutral' description

    Logic:
        - Home offensive snaps vs league avg → extra home scoring
        - Away offensive snaps vs league avg → extra away scoring
        - Home defensive snaps vs league avg → extra opponent scoring
        - Away defensive snaps vs league avg → extra opponent scoring
    """
    baseline = await get_league_pace_baseline(db, season_year, max_week)
    home = await get_team_pace_profile(
        db, home_abbr, season_year, num_games, max_week
    )
    away = await get_team_pace_profile(
        db, away_abbr, season_year, num_games, max_week
    )

    if not home or not away:
        return {
            "adjustment_pts": 0.0,
            "has_pace_data": False,
            "home_pace": home,
            "away_pace": away,
            "league_baseline": baseline,
        }

    off_avg = baseline["off_snaps_avg"]
    def_avg = baseline["def_snaps_avg"]

    # Extra plays each team runs vs league average
    home_off_diff = home["off_snaps_avg"] - off_avg
    away_off_diff = away["off_snaps_avg"] - off_avg

    # Extra defensive snaps faced
    home_def_diff = home["def_snaps_avg"] - def_avg
    away_def_diff = away["def_snaps_avg"] - def_avg

    # Points impact from pace:
    # Home scores more if they run more plays on offense
    home_scoring_boost = home_off_diff * PTS_PER_OFFENSIVE_PLAY
    # Away scores more if they run more plays
    away_scoring_boost = away_off_diff * PTS_PER_OFFENSIVE_PLAY
    # Home allows more if defense is on field more
    home_def_penalty = home_def_diff * PTS_PER_DEFENSIVE_SNAP
    # Away allows more if defense is on field more
    away_def_penalty = away_def_diff * PTS_PER_DEFENSIVE_SNAP

    # Total adjustment to game total
    # Home scores more (home_off_diff+) + home allows more (home_def_diff+ = opponent scores more)
    # Total points added: home scoring + away scoring + home defense + away defense
    total_adjustment = (
        home_scoring_boost + away_scoring_boost
        + home_def_penalty + away_def_penalty
    )

    # Classify tempo
    combined_pace = (
        home["off_snaps_avg"] + away["off_snaps_avg"]
    ) / 2
    if combined_pace > off_avg + 3:
        tempo = "fast"
    elif combined_pace < off_avg - 3:
        tempo = "slow"
    else:
        tempo = "neutral"

    return {
        "adjustment_pts": round(total_adjustment, 1),
        "home_off_boost": round(home_scoring_boost, 1),
        "away_off_boost": round(away_scoring_boost, 1),
        "home_def_penalty": round(home_def_penalty, 1),
        "away_def_penalty": round(away_def_penalty, 1),
        "tempo": tempo,
        "has_pace_data": True,
        "home_pace": home,
        "away_pace": away,
        "league_baseline": baseline,
    }


def enrich_ou_prediction(predicted_total: float, pace_adjustment: dict) -> dict:
    """
    Apply pace adjustment to a predicted total.

    Returns the adjusted total and the explanation.
    """
    adj_pts = pace_adjustment.get("adjustment_pts", 0.0)

    if not pace_adjustment.get("has_pace_data", False):
        return {
            "predicted_total": predicted_total,
            "adjusted_total": predicted_total,
            "pace_adjustment": 0.0,
            "pace_note": "No pace data available for this season",
        }

    adjusted_total = round(predicted_total + adj_pts, 1)

    pace_details = []
    if pace_adjustment["home_off_boost"] > 0.3:
        pace_details.append(
            f"{pace_adjustment['home_pace']['team']} runs "
            f"{pace_adjustment['home_pace']['off_snaps_avg']} plays/game "
            f"(+{pace_adjustment['home_off_boost']:+.1f} pts)"
        )
    elif pace_adjustment["home_off_boost"] < -0.3:
        pace_details.append(
            f"{pace_adjustment['home_pace']['team']} runs "
            f"{pace_adjustment['home_pace']['off_snaps_avg']} plays/game "
            f"({pace_adjustment['home_off_boost']:+.1f} pts)"
        )

    if pace_adjustment["away_off_boost"] > 0.3:
        pace_details.append(
            f"{pace_adjustment['away_pace']['team']} runs "
            f"{pace_adjustment['away_pace']['off_snaps_avg']} plays/game "
            f"(+{pace_adjustment['away_off_boost']:+.1f} pts)"
        )
    elif pace_adjustment["away_off_boost"] < -0.3:
        pace_details.append(
            f"{pace_adjustment['away_pace']['team']} runs "
            f"{pace_adjustment['away_pace']['off_snaps_avg']} plays/game "
            f"({pace_adjustment['away_off_boost']:+.1f} pts)"
        )

    if pace_adjustment["home_def_penalty"] > 0.3:
        pace_details.append(
            f"{pace_adjustment['home_pace']['team']} defense faces "
            f"{pace_adjustment['home_pace']['def_snaps_avg']} snaps/game "
            f"(+{pace_adjustment['home_def_penalty']:+.1f} pts allowed)"
        )
    elif pace_adjustment["home_def_penalty"] < -0.3:
        pace_details.append(
            f"{pace_adjustment['home_pace']['team']} defense faces "
            f"{pace_adjustment['home_pace']['def_snaps_avg']} snaps/game "
            f"({pace_adjustment['home_def_penalty']:+.1f} pts allowed)"
        )

    note = " | ".join(pace_details) if pace_details else f"Pace: {pace_adjustment['tempo']} game"

    return {
        "predicted_total": predicted_total,
        "adjusted_total": adjusted_total,
        "pace_adjustment": adj_pts,
        "tempo": pace_adjustment["tempo"],
        "pace_note": note,
        "home_pace": pace_adjustment["home_pace"],
        "away_pace": pace_adjustment["away_pace"],
        "league_baseline": pace_adjustment["league_baseline"],
    }
