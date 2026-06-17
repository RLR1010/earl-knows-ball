"""nflverse weekly player stats ingestion."""

import httpx
import io
import pandas as pd
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Player, Game, Team, Season, PlayerWeeklyStats


COLUMN_MAP = {
    "attempts": "pass_attempts",
    "completions": "pass_completions",
    "passing_yards": "pass_yards",
    "passing_tds": "pass_tds",
    "passing_interceptions": "pass_int",
    "carries": "rush_attempts",
    "rushing_yards": "rush_yards",
    "rushing_tds": "rush_tds",
    "targets": "targets",
    "receptions": "receptions",
    "receiving_yards": "receiving_yards",
    "receiving_tds": "receiving_tds",
    "rushing_fumbles": "fumbles",
    "rushing_fumbles_lost": "fumbles_lost",
    "def_sacks": "sacks",
    "def_interceptions": "interceptions",
    "fumble_recovery_opp": "fumbles_recovered",
    "def_tds": "defensive_tds",
    "special_teams_tds": "special_teams_tds",
    "fg_made": "field_goals_made",
    "fg_att": "field_goals_attempted",
    "pat_made": "extra_points_made",
    "pat_att": "extra_points_attempted",
    "fantasy_points_ppr": "fantasy_points_ppr",
    "fantasy_points": "fantasy_points_std",
}


async def fetch_nflverse_stats(season: int) -> pd.DataFrame:
    url = f"https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{season}.parquet"
    async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        buf = io.BytesIO(resp.content)
        df = pd.read_parquet(buf)
    return df


async def ingest_nflverse_stats(
    session: AsyncSession,
    season_year: int = 2025,
) -> dict:
    """Load weekly player stats from nflverse for a single season."""
    # Get or create season
    result = await session.execute(select(Season).where(Season.year == season_year))
    season = result.scalar_one_or_none()
    if not season:
        season = Season(year=season_year)
        session.add(season)
        await session.flush()

    # Caches
    player_cache = {}
    team_cache = {}
    game_cache = {}

    # Pre-load teams
    result = await session.execute(select(Team))
    for t in result.scalars().all():
        team_cache[t.abbreviation] = t

    # Pre-load players with GSIS IDs (handle dups: prefer ones with a team)
    result = await session.execute(select(Player).where(Player.nflverse_id.isnot(None)))
    for p in result.scalars().all():
        if p.nflverse_id not in player_cache or (p.team_id and not player_cache[p.nflverse_id].team_id):
            player_cache[p.nflverse_id] = p

    # Pre-load games for this season (if any)
    result = await session.execute(select(Game).where(Game.season_id == season.id))
    for g in result.scalars().all():
        game_cache[(g.home_team_id, g.away_team_id, g.week)] = g

    print(f"  Cached {len(player_cache)} players, {len(game_cache)} games for {season_year}")

    # Download
    df = await fetch_nflverse_stats(season_year)
    df = df[df["season_type"].isin(["REG", "POST"])]
    print(f"  Downloaded {len(df)} stat rows")

    stats_added = 0
    no_player = 0
    no_team = 0

    for _, row in df.iterrows():
        gsis = str(row.get("player_id", "")).strip()
        team_abbr = str(row.get("team", "")).strip()
        week = int(row.get("week", 0))
        opponent = str(row.get("opponent_team", "")).strip()

        if not gsis or not week:
            continue

        player = player_cache.get(gsis)
        if not player:
            no_player += 1
            continue

        # Map historical team abbreviations
        TEAM_ABBR_MAP = {
            "LA": "LAR", "SL": "LAR", "STL": "LAR",
            "SD": "LAC",
            "OAK": "LV",
            "WSH": "WAS",
        }
        team_abbr = TEAM_ABBR_MAP.get(team_abbr, team_abbr)
        opponent = TEAM_ABBR_MAP.get(opponent, opponent)

        player_team = team_cache.get(team_abbr)
        opp_team = team_cache.get(opponent)
        if not player_team or not opp_team:
            no_team += 1
            continue

        # Try to match a game (optional — NULL if no match)
        game = game_cache.get((opp_team.id, player_team.id, week))
        if not game:
            game = game_cache.get((player_team.id, opp_team.id, week))

        game_id = game.id if game else None

        # Check dup by (player_id, season_year, week) when no game
        if game_id:
            dup_check = await session.execute(
                select(PlayerWeeklyStats).where(
                    and_(
                        PlayerWeeklyStats.player_id == player.id,
                        PlayerWeeklyStats.game_id == game_id,
                    )
                )
            )
        else:
            dup_check = await session.execute(
                select(PlayerWeeklyStats).where(
                    and_(
                        PlayerWeeklyStats.player_id == player.id,
                        PlayerWeeklyStats.season_id == season.id,
                        PlayerWeeklyStats.week == week,
                    )
                )
            )

        if dup_check.scalar_one_or_none():
            continue

        stats_row = {
            "player_id": player.id,
            "game_id": game_id,
            "season_id": season.id,
            "week": week,
            "team_id": player_team.id,
            "opponent_id": opp_team.id,
        }

        for nf_col, model_col in COLUMN_MAP.items():
            val = row.get(nf_col)
            if val is not None and not pd.isna(val) and val != 0:
                stats_row[model_col] = val

        session.add(PlayerWeeklyStats(**stats_row))
        stats_added += 1

        if stats_added % 5000 == 0:
            await session.flush()

    await session.commit()
    return {
        "season": season_year,
        "stats_loaded": stats_added,
        "no_player_match": no_player,
        "no_team_match": no_team,
    }
