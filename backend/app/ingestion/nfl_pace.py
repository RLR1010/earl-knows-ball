"""
Ingest NFL pace data from nflverse snap_counts.

Downloads player-level snap counts per game, aggregates to team level,
and stores in nfl.team_pace_stats.

Pace metrics:
  - offensive_snaps: Total offensive plays run by the team
  - defensive_snaps: Total defensive snaps the team was on the field
  - special_teams_snaps: Total ST snaps
  - total_snaps: Combined total

These are critical for over/under prediction — teams that run more plays
create more scoring opportunities.
"""
import asyncio
import csv
import gzip
import io
import logging
from collections import defaultdict
from datetime import datetime, timezone

import httpx

from sqlalchemy import select, and_, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TeamPaceStats, Team, Season, Game

logger = logging.getLogger("earl.nfl_pace")

NFLVERSE_BASE = "https://github.com/nflverse/nflverse-data/releases/download/snap_counts"

# Earliest year with snap count data
MIN_YEAR = 2012


async def download_snap_counts_csv(year: int) -> list[dict]:
    """Download snap_counts CSV for a year and return as list of dicts."""
    url = f"{NFLVERSE_BASE}/snap_counts_{year}.csv.gz"
    logger.info(f"Downloading snap counts for {year}: {url}")

    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        text = gzip.decompress(resp.content).decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    logger.info(f"  Downloaded {len(rows)} player-snap records for {year}")
    return rows


async def _build_game_lookup(db: AsyncSession, years: list[int]) -> dict:
    """
    Build a lookup: (season, week, away_abbr, home_abbr) -> game_id.

    Uses our DB games to construct nflverse-style game_ids.
    Also returns a team abbreviation lookup.
    """
    # Load teams
    team_r = await db.execute(select(Team))
    teams = {t.id: t.abbreviation for t in team_r.scalars().all()}
    abbr_to_id = {a: i for i, a in teams.items()}

    # Build lookup from (season, week, home, away) -> game_id
    # We'll also create nflverse game_id keys
    lookup = {}

    for year in years:
        season_r = await db.execute(select(Season).where(Season.year == year))
        season = season_r.scalar_one_or_none()
        if not season:
            logger.warning(f"  Season {year} not found in DB, skipping")
            continue

        game_r = await db.execute(
            select(Game).where(
                Game.season_id == season.id,
                Game.game_type == "REG",
            )
        )
        for g in game_r.scalars().all():
            home_abbr = teams.get(g.home_team_id)
            away_abbr = teams.get(g.away_team_id)
            if not home_abbr or not away_abbr:
                continue

            # nflverse game_id format: {season}_{week_padded}_{away}_{home}
            # Week is zero-padded to 2 digits (e.g., "01", "09", "14")
            week_padded = f"{g.week:02d}"
            nflverse_gid = f"{year}_{week_padded}_{away_abbr}_{home_abbr}"

            # Also store alternate key (if away and home could be swapped)
            rev_key = f"{year}_{week_padded}_{home_abbr}_{away_abbr}"

            lookup[nflverse_gid] = {
                "game_id": g.id,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
            }
            lookup[rev_key] = {
                "game_id": g.id,
                "home_abbr": home_abbr,
                "away_abbr": away_abbr,
            }

    return lookup, abbr_to_id


async def ingest_pace_data(
    db: AsyncSession,
    years: list[int] | None = None,
    clear_existing: bool = False,
) -> dict:
    """
    Download nflverse snap_counts data and store as TeamPaceStats.

    Args:
        years: List of years to process. Defaults to 2012-current year.
        clear_existing: If True, clear existing pace stats before inserting.

    Returns:
        dict with ingestion stats
    """
    if years is None:
        current_year = datetime.now(timezone.utc).year
        years = list(range(MIN_YEAR, current_year + 1))

    logger.info(f"=== Ingesting NFL pace data for years: {years} ===")

    # Build game lookup
    lookup, abbr_to_id = await _build_game_lookup(db, years)
    logger.info(f"  Built lookup with {len(lookup)} game keys")

    if clear_existing:
        for year in years:
            season_r = await db.execute(select(Season).where(Season.year == year))
            season = season_r.scalar_one_or_none()
            if season:
                season_ids = [season.id]
                await db.execute(
                    delete(TeamPaceStats).where(
                        TeamPaceStats.season_id.in_(season_ids)
                    )
                )
        await db.commit()
        logger.info("  Cleared existing pace stats")

    stats = {"total_rows": 0, "inserted": 0, "skipped": 0, "errors": 0, "years": {}}

    for year in years:
        year_stats = {"downloaded": 0, "games_found": 0, "inserted": 0, "skipped": 0}

        try:
            rows = await download_snap_counts_csv(year)
            year_stats["downloaded"] = len(rows)
        except Exception as e:
            logger.error(f"  Failed to download snap counts for {year}: {e}")
            stats["errors"] += 1
            stats["years"][str(year)] = {"error": str(e)}
            continue

        # Get the season ID for this year
        season_r = await db.execute(select(Season).where(Season.year == year))
        season_obj = season_r.scalar_one_or_none()
        if not season_obj:
            logger.warning(f"  Season {year} not found in DB, skipping")
            stats["skipped"] += 1
            continue

        # Aggregate player-level snaps to team-game level
        # key: (team_abbr, nflverse_game_id)
        team_game_data = defaultdict(lambda: {
            "off_total": 0, "def_total": 0, "st_total": 0,
            "off_max": 0, "def_max": 0,
            "off_players": set(), "def_players": set(),
            "season_type": None, "week": None,
        })

        for r in rows:
            team_abbr = r.get("team", "")
            game_id = r.get("game_id", "")
            key = (team_abbr, game_id)

            d = team_game_data[key]
            off = int(r.get("offense_snaps", 0) or 0)
            df = int(r.get("defense_snaps", 0) or 0)
            st = int(r.get("st_snaps", 0) or 0)
            d["off_total"] += off
            d["def_total"] += df
            d["st_total"] += st
            d["off_max"] = max(d["off_max"], off)
            d["def_max"] = max(d["def_max"], df)
            if off > 0:
                d["off_players"].add(r.get("pfr_player_id", ""))
            if df > 0:
                d["def_players"].add(r.get("pfr_player_id", ""))
            d["season_type"] = r.get("game_type", "")
            d["week"] = int(r.get("week", 0))

        year_stats["games_found"] = len(team_game_data)
        logger.info(
            f"  Aggregated {len(team_game_data)} team-game records for {year}"
        )

        # Insert into DB
        insert_count = 0
        skip_count = 0
        batch = []

        for (team_abbr, nflverse_gid), data in team_game_data.items():
            # Look up game_id from our DB
            match = lookup.get(nflverse_gid)
            if not match:
                skip_count += 1
                continue

            game_id = match["game_id"]
            team_id = abbr_to_id.get(team_abbr)
            if not team_id:
                skip_count += 1
                continue

            # Check if already exists
            existing_r = await db.execute(
                select(TeamPaceStats).where(
                    TeamPaceStats.game_id == game_id,
                    TeamPaceStats.team_id == team_id,
                )
            )
            if existing_r.scalar_one_or_none():
                skip_count += 1
                continue

            # 'off_max' = max single-player offensive snaps = actual team offensive plays
            # 'off_total' = sum across all players (line estimate, less useful)
            off_snaps = data["off_max"]  # Use max as actual play count
            def_snaps = data["def_max"]  # Use max as actual defensive plays faced
            st_snaps = data["st_total"]  # ST uses total (different formation each play)
            total = off_snaps + def_snaps + st_snaps

            pace = TeamPaceStats(
                game_id=game_id,
                team_id=team_id,
                season_id=season_obj.id,
                week=data["week"],
                season_type=data["season_type"],
                offensive_snaps=off_snaps,
                defensive_snaps=def_snaps,
                special_teams_snaps=st_snaps,
                total_snaps=total,
                offensive_players=len(data["off_players"]),
                defensive_players=len(data["def_players"]),
                source="nflverse_snap_counts",
            )
            batch.append(pace)
            insert_count += 1

            # Flush in batches
            if len(batch) >= 100:
                for p in batch:
                    db.add(p)
                await db.flush()
                batch = []

        # Flush remaining
        for p in batch:
            db.add(p)
        await db.flush()

        year_stats["inserted"] = insert_count
        year_stats["skipped"] = skip_count
        stats["total_rows"] += insert_count
        stats["inserted"] += insert_count
        stats["skipped"] += skip_count

        stats["years"][str(year)] = year_stats
        logger.info(
            f"  Year {year}: {insert_count} inserted, {skip_count} skipped "
            f"({year_stats['games_found']} team-game records in source)"
        )

    await db.commit()

    logger.info(
        f"=== Pace ingestion complete: {stats['inserted']} rows inserted, "
        f"{stats['skipped']} skipped ==="
    )
    return stats


async def get_team_pace_summary(
    db: AsyncSession,
    season_year: int,
    team_abbr: str | None = None,
    exclude_postseason: bool = True,
) -> list[dict]:
    """
    Get pace summary for one or all teams in a season.

    Returns per-game pace stats with averages.
    """
    conditions = []
    season_r = await db.execute(select(Season).where(Season.year == season_year))
    season = season_r.scalar_one_or_none()
    if not season:
        return []

    conditions.append(TeamPaceStats.season_id == season.id)

    team_r = await db.execute(select(Team))
    teams = {t.id: t.abbreviation for t in team_r.scalars().all()}
    abbr_to_id = {a: i for i, a in teams.items()}

    if team_abbr:
        team_id = abbr_to_id.get(team_abbr.upper())
        if not team_id:
            return []
        conditions.append(TeamPaceStats.team_id == team_id)

    if exclude_postseason:
        conditions.append(TeamPaceStats.season_type == "REG")

    r = await db.execute(
        select(TeamPaceStats)
        .where(*conditions)
        .order_by(TeamPaceStats.week)
    )
    rows = r.scalars().all()

    results = []
    for row in rows:
        results.append({
            "game_id": row.game_id,
            "team": teams.get(row.team_id, "???"),
            "week": row.week,
            "offensive_snaps": row.offensive_snaps,
            "defensive_snaps": row.defensive_snaps,
            "total_snaps": row.total_snaps,
            "offensive_players": row.offensive_players,
            "defensive_players": row.defensive_players,
        })

    return results


async def get_team_pace_averages(
    db: AsyncSession,
    season_year: int,
    team_abbr: str,
    num_games: int | None = None,
) -> dict | None:
    """
    Get average pace metrics for a team over its most recent N games.

    Returns dict with off_snaps_avg, def_snaps_avg, total_snaps_avg, etc.
    """
    season_r = await db.execute(select(Season).where(Season.year == season_year))
    season = season_r.scalar_one_or_none()
    if not season:
        return None

    team_r = await db.execute(select(Team).where(Team.abbreviation == team_abbr.upper()))
    team = team_r.scalar_one_or_none()
    if not team:
        return None

    conditions = [
        TeamPaceStats.season_id == season.id,
        TeamPaceStats.team_id == team.id,
        TeamPaceStats.season_type == "REG",
    ]

    r = await db.execute(
        select(TeamPaceStats)
        .where(*conditions)
        .order_by(TeamPaceStats.week.desc())
    )
    rows = list(r.scalars().all())

    if not rows:
        return None

    if num_games:
        rows = rows[:num_games]

    off_snaps = [r.offensive_snaps for r in rows]
    def_snaps = [r.defensive_snaps for r in rows]

    return {
        "team": team_abbr.upper(),
        "season": season_year,
        "num_games": len(rows),
        "offensive_snaps_avg": round(sum(off_snaps) / len(off_snaps), 1),
        "defensive_snaps_avg": round(sum(def_snaps) / len(def_snaps), 1),
        "total_snaps_avg": round(
            sum(r.total_snaps for r in rows) / len(rows), 1
        ),
        "offensive_players_avg": round(
            sum(r.offensive_players or 0 for r in rows) / len(rows), 1
        ),
        "league_offensive_snaps_avg": round(
            sum(off_snaps) / len(off_snaps), 1
        ),
    }


async def get_pace_adjustment(
    db: AsyncSession,
    season_year: int,
    home_abbr: str,
    away_abbr: str,
    num_games: int = 5,
) -> float:
    """
    Compute a pace-based adjustment for over/under.

    Returns a points adjustment to the predicted total:
      - Positive = faster pace pushes total higher (Over)
      - Negative = slower pace pushes total lower (Under)

    Uses average offensive snaps per game compared to league average.
    """
    season_r = await db.execute(select(Season).where(Season.year == season_year))
    season = season_r.scalar_one_or_none()
    if not season:
        return 0.0

    # Get league average offensive snaps per game for the season
    r = await db.execute(
        select(TeamPaceStats).where(
            TeamPaceStats.season_id == season.id,
            TeamPaceStats.season_type == "REG",
        )
    )
    all_pace = list(r.scalars().all())

    if not all_pace:
        return 0.0

    league_avg_off = sum(p.offensive_snaps for p in all_pace) / max(len(all_pace), 1)

    # Get home and away pace averages
    home_pace = await get_team_pace_averages(db, season_year, home_abbr, num_games)
    away_pace = await get_team_pace_averages(db, season_year, away_abbr, num_games)

    if not home_pace or not away_pace:
        return 0.0

    # How many extra/fewer plays each team runs vs league average
    home_diff = home_pace["offensive_snaps_avg"] - league_avg_off
    away_diff = away_pace["offensive_snaps_avg"] - league_avg_off

    # Each additional play is worth ~0.4 points in expected scoring
    # (league average is ~0.4 pts per play for offense + defense combined)
    PTS_PER_PLAY = 0.4

    # The game total is affected by both teams' pace
    # But the offense-only pace gives ~0.25 pts per play (since defense doesn't score)
    # Actually: on offense, you're creating scoring opportunities
    # On defense, you're giving the opponent opportunities
    # For total points, both matter:
    #   home offense pace + away defense faced = home scoring
    #   away offense pace + home defense faced = away scoring
    # But we only have offensive/defensive snaps:
    #   home offensive snaps = home scoring opportunities
    #   home defensive snaps = opp scoring opportunities

    # Get defensive pace too
    home_def_avg = sum(
        p.defensive_snaps
        for p in all_pace
    ) / max(len(all_pace), 1)

    away_def_avg = home_def_avg  # same league average for defensive snaps faced

    home_def_diff = home_pace.get("defensive_snaps_avg", home_def_avg) - home_def_avg
    away_def_diff = away_pace.get("defensive_snaps_avg", away_def_avg) - away_def_avg

    # Total adjustment: more offensive plays = more scoring for that team
    # More defensive plays = opponent scores more
    # Total game points = home_off_adj + away_def_adj + away_off_adj + home_def_adj
    total_adjustment = (
        (home_diff + away_def_diff) * PTS_PER_PLAY * 0.5 +
        (away_diff + home_def_diff) * PTS_PER_PLAY * 0.5
    )

    return round(total_adjustment, 1)
