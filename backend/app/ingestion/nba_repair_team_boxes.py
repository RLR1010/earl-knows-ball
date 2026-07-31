"""
Repair missing team-level box scores in nba.games by summing player_game_stats.

Some games in seasons 22-27 (2012-13 → 2017-18) were ingested from ESPN's
scoreboard API, which doesn't include the detailed team shooting box
(FGM/FGA/3PM/3PA/FTM/FTA/REB/AST/STL/BLK/TOV/PF). Once per-player game stats
exist for those games (see nba_player_game_stats_run.py), the team box can be
reconstructed exactly: verified 1:1 against games where both sources exist.

Usage:
    python -m backend.app.ingestion.nba_repair_team_boxes [--dry-run]
"""

import logging
import sys

from sqlalchemy import create_engine, text as sa_text

from backend.app.core.config import settings

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

DB_URL = settings.database_url_sync

MAPPING = {
    "field_goals_made": "field_goals_made",
    "field_goals_attempted": "field_goals_attempted",
    "three_points_made": "three_pointers_made",
    "three_points_attempted": "three_pointers_attempted",
    "free_throws_made": "free_throws_made",
    "free_throws_attempted": "free_throws_attempted",
    "rebounds": "rebounds_total",
    "assists": "assists",
    "steals": "steals",
    "blocks": "blocks",
    "turnovers": "turnovers",
    "fouls": "fouls_personal",
}

PAIRS = [
    ("home_team_id", "home_field_goals_made", "home_"),
    ("away_team_id", "away_field_goals_made", "away_"),
]


def repair(engine, dry_run: bool = False):
    with engine.connect() as conn:
        # Games missing team box (either side) that have player stats available
        q = sa_text(
            """
            SELECT g.id, g.home_team_id, g.away_team_id
            FROM nba.games g
            WHERE (g.home_field_goals_made IS NULL OR g.away_field_goals_made IS NULL)
              AND g.game_type IN ('REG', 'POST')
              AND EXISTS (SELECT 1 FROM nba.player_game_stats pgs
                          WHERE pgs.game_id = g.id LIMIT 1)
            """
        )
        games = conn.execute(q).fetchall()
        logger.info(f"Found {len(games)} games with player stats but missing team box")

        if dry_run:
            return

        updated = 0
        with engine.begin() as tx:
            for gid, home_id, away_id in games:
                for team_col, box_probe, prefix in PAIRS:
                    team_id = home_id if prefix == "home_" else away_id
                    if team_id is None:
                        continue
                    probe_col = box_probe
                    has_box = conn.execute(
                        sa_text(f"SELECT {probe_col} FROM nba.games WHERE id = :gid"),
                        {"gid": gid},
                    ).scalar()
                    if has_box is not None:
                        continue
                    sums = conn.execute(
                        sa_text(
                            """
                            SELECT
                                sum(field_goals_made), sum(field_goals_attempted),
                                sum(three_pointers_made), sum(three_pointers_attempted),
                                sum(free_throws_made), sum(free_throws_attempted),
                                sum(rebounds_total), sum(assists), sum(steals),
                                sum(blocks), sum(turnovers), sum(fouls_personal)
                            FROM nba.player_game_stats
                            WHERE game_id = :gid AND team_id = :team_id
                            """
                        ),
                        {"gid": gid, "team_id": team_id},
                    ).fetchone()
                    if sums[0] is None:
                        continue
                    set_clause = ", ".join(
                        f"{prefix}{col} = :v{i}"
                        for i, col in enumerate(MAPPING.keys())
                    )
                    params = {f"v{i}": sums[i] for i in range(len(MAPPING))}
                    params["gid"] = gid
                    tx.execute(
                        sa_text(f"UPDATE nba.games SET {set_clause} WHERE id = :gid"),
                        params,
                    )
                    updated += 1
        logger.info(f"Repaired team boxes for {updated} team-game sides")


def main():
    dry_run = "--dry-run" in sys.argv
    engine = create_engine(DB_URL)
    try:
        repair(engine, dry_run=dry_run)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
