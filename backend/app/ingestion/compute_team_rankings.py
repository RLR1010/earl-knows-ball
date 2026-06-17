"""
Compute and store weekly team rankings (scoring offense/defense, total offense/defense)
for every season and week, using ONLY games played before each week (no look-ahead).

Stored in weekly_team_rankings table (nfl schema) for fast feature engineering.
"""
import asyncio
import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import text, select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("compute_rankings")

DB = "postgresql+asyncpg://earl:earl@localhost:5432/earl_knows_football"


async def compute_rankings():
    engine = create_async_engine(DB)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        # Get all seasons with games
        seasons = await db.execute(
            text("SELECT id, year FROM seasons ORDER BY year")
        )
        seasons = seasons.all()

        prior_final_week = None  # (week, year) of last season's final rankings

        for season_row in seasons:
            season_id, year = season_row
            logger.info(f"\n=== Season {year} ===")

            # Carry over prior season's final rankings to Week 1
            if prior_final_week:
                prev_year, prev_week = prior_final_week
                logger.info(f"  Carrying over {prev_year} W{prev_week} rankings to W1")
                r = await db.execute(
                    text("""
                        INSERT INTO nfl.weekly_team_rankings
                            (season, week, team_id, games_played,
                             scoring_offense_rank, scoring_defense_rank,
                             total_offense_rank, total_defense_rank,
                             points_per_game, points_allowed_per_game,
                             yards_per_game, yards_allowed_per_game)
                        SELECT :new_year, 1, team_id, 0,
                               scoring_offense_rank, scoring_defense_rank,
                               total_offense_rank, total_defense_rank,
                               points_per_game, points_allowed_per_game,
                               yards_per_game, yards_allowed_per_game
                        FROM nfl.weekly_team_rankings
                        WHERE season = :prev_year AND week = :prev_week
                        ON CONFLICT (season, week, team_id)
                        DO UPDATE SET
                            scoring_offense_rank = EXCLUDED.scoring_offense_rank,
                            scoring_defense_rank = EXCLUDED.scoring_defense_rank,
                            total_offense_rank = EXCLUDED.total_offense_rank,
                            total_defense_rank = EXCLUDED.total_defense_rank,
                            points_per_game = EXCLUDED.points_per_game,
                            points_allowed_per_game = EXCLUDED.points_allowed_per_game,
                            yards_per_game = EXCLUDED.yards_per_game,
                            yards_allowed_per_game = EXCLUDED.yards_allowed_per_game
                    """),
                    {"new_year": year, "prev_year": prev_year, "prev_week": prev_week}
                )
                await db.commit()
                logger.info(f"  Carried over {r.rowcount} team rankings")

            # Get max week for this season
            max_week = await db.execute(
                text("""
                    SELECT COALESCE(MAX(week), 0) FROM games
                    WHERE season_id = :sid AND game_type = 'REG'
                    AND home_score IS NOT NULL AND away_score IS NOT NULL
                """),
                {"sid": season_id}
            )
            max_week = max_week.scalar()

            for target_week in range(1, max_week + 1):
                # Get all games played before this week (weeks 1 to target_week-1)
                prior_games = await db.execute(
                    text("""
                        SELECT g.id, g.home_team_id, g.away_team_id,
                               g.home_score, g.away_score
                        FROM games g
                        WHERE g.season_id = :sid
                          AND g.game_type = 'REG'
                          AND g.week < :tw
                          AND g.home_score IS NOT NULL
                          AND g.away_score IS NOT NULL
                    """),
                    {"sid": season_id, "tw": target_week}
                )
                prior_games = prior_games.all()

                if len(prior_games) < 2:
                    logger.info(f"  W{target_week}: fewer than 2 prior games, skipping")
                    continue

                # Build per-team stats from prior games
                team_stats = {}
                for g in prior_games:
                    # Home team
                    if g.home_team_id not in team_stats:
                        team_stats[g.home_team_id] = {
                            'pf': 0, 'pa': 0, 'games': 0,
                            'total_yards': 0, 'total_yards_allowed': 0,
                            'pass_yards': 0, 'rush_yards': 0,
                            'pass_yards_allowed': 0, 'rush_yards_allowed': 0,
                        }
                    if g.away_team_id not in team_stats:
                        team_stats[g.away_team_id] = {
                            'pf': 0, 'pa': 0, 'games': 0,
                            'total_yards': 0, 'total_yards_allowed': 0,
                            'pass_yards': 0, 'rush_yards': 0,
                            'pass_yards_allowed': 0, 'rush_yards_allowed': 0,
                        }

                    ts_h = team_stats[g.home_team_id]
                    ts_a = team_stats[g.away_team_id]
                    ts_h['pf'] += g.home_score
                    ts_h['pa'] += g.away_score
                    ts_h['games'] += 1
                    ts_a['pf'] += g.away_score
                    ts_a['pa'] += g.home_score
                    ts_a['games'] += 1

                # Get yards stats from player_weekly_stats
                yards_data = await db.execute(
                    text("""
                        SELECT team_id, opponent_id,
                               COALESCE(SUM(pass_yards), 0) + COALESCE(SUM(rush_yards), 0) as total_yds,
                               COALESCE(SUM(yards_allowed), 0) as yds_allowed,
                               COALESCE(SUM(pass_yards), 0) as pass_yds,
                               COALESCE(SUM(rush_yards), 0) as rush_yds
                        FROM player_weekly_stats pws
                        JOIN games g ON g.id = pws.game_id
                        WHERE g.season_id = :sid AND g.week < :tw
                        GROUP BY pws.team_id, pws.opponent_id
                    """),
                    {"sid": season_id, "tw": target_week}
                )
                for row in yards_data:
                    if row.team_id in team_stats:
                        team_stats[row.team_id]['total_yards'] += row.total_yds
                        team_stats[row.team_id]['total_yards_allowed'] += row.yds_allowed

                # Compute per-game averages
                averages = []
                for tid, s in team_stats.items():
                    g = max(s['games'], 1)
                    averages.append({
                        'team_id': tid,
                        'games': s['games'],
                        'ppg': s['pf'] / g,
                        'papg': s['pa'] / g,
                        'ypg': s['total_yards'] / g if s['total_yards'] else 0,
                        'ypapg': s['total_yards_allowed'] / g if s['total_yards_allowed'] else 0,
                    })

                if len(averages) < 4:
                    continue

                # Rank each category (1 = best, 32 = worst)
                for cat_key, cat_name, ascending in [
                    ('ppg', 'scoring_offense', False),       # more points = better
                    ('papg', 'scoring_defense', True),       # fewer points allowed = better
                    ('ypg', 'total_offense', False),          # more yards = better
                    ('ypapg', 'total_defense', True),         # fewer yards allowed = better
                ]:
                    sorted_teams = sorted(averages, key=lambda x: x[cat_key], reverse=not ascending)
                    if ascending:
                        sorted_teams = sorted(averages, key=lambda x: x[cat_key])
                    for rank, entry in enumerate(sorted_teams, 1):
                        entry[f'{cat_name}_rank'] = rank

                # Upsert rankings
                for entry in averages:
                    await db.execute(
                        text("""
                            INSERT INTO nfl.weekly_team_rankings
                                (season, week, team_id, games_played,
                                 scoring_offense_rank, scoring_defense_rank,
                                 total_offense_rank, total_defense_rank,
                                 points_per_game, points_allowed_per_game,
                                 yards_per_game, yards_allowed_per_game)
                            VALUES (:season, :week, :team_id, :games,
                                    :so_rank, :sd_rank, :to_rank, :td_rank,
                                    :ppg, :papg, :ypg, :ypapg)
                            ON CONFLICT (season, week, team_id)
                            DO UPDATE SET
                                scoring_offense_rank = EXCLUDED.scoring_offense_rank,
                                scoring_defense_rank = EXCLUDED.scoring_defense_rank,
                                total_offense_rank = EXCLUDED.total_offense_rank,
                                total_defense_rank = EXCLUDED.total_defense_rank,
                                points_per_game = EXCLUDED.points_per_game,
                                points_allowed_per_game = EXCLUDED.points_allowed_per_game,
                                yards_per_game = EXCLUDED.yards_per_game,
                                yards_allowed_per_game = EXCLUDED.yards_allowed_per_game,
                                games_played = EXCLUDED.games_played
                        """),
                        {
                            "season": year, "week": target_week,
                            "team_id": entry['team_id'],
                            "games": entry['games'],
                            "so_rank": entry.get('scoring_offense_rank', 16),
                            "sd_rank": entry.get('scoring_defense_rank', 16),
                            "to_rank": entry.get('total_offense_rank', 16),
                            "td_rank": entry.get('total_defense_rank', 16),
                            "ppg": round(entry['ppg'], 1),
                            "papg": round(entry['papg'], 1),
                            "ypg": round(entry['ypg'], 1),
                            "ypapg": round(entry['ypapg'], 1),
                        }
                    )

                await db.commit()
                if target_week % 4 == 0 or target_week == 1:
                    logger.info(f"  W{target_week}: {len(averages)} teams ranked")

        # Store final week for carryover to next season
        prior_final_week = (year, max_week)

        await db.commit()
        logger.info(f"Season {year} done: stored final W{max_week} for carryover")

    # Verify
    async with Session() as db:
        r = await db.execute(text("SELECT COUNT(*) FROM nfl.weekly_team_rankings"))
        total = r.scalar()
        r = await db.execute(text("""
            SELECT season, week, COUNT(*) FROM nfl.weekly_team_rankings
            GROUP BY season, week ORDER BY season, week LIMIT 5
        """))
        logger.info(f"Total ranking entries: {total}")
        for row in r:
            logger.info(f"  {row.season} W{row.week}: {row.count} teams")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(compute_rankings())
