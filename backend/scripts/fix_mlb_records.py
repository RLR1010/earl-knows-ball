"""
Migration: Fix home_wins / home_losses / away_wins / away_losses in mlb.games.

The existing data is wrong because:
1. The ingestion had a swap bug (home_wins got away_record, away_wins got home_record)
2. Records were only set on initial game insert and never refreshed

This script recalculates ALL records by counting actual wins/losses from the games table.
Processes all games (FINAL, IN_PROGRESS, SCHEDULED) in date order so cumulative
records are correct for every game.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from sqlalchemy import create_engine, text

DATABASE_URL = "postgresql://earl:earl2025@localhost:5432/earl_knows_football"
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

def run():
    with engine.connect() as conn:
        seasons = conn.execute(text("SELECT id, year FROM mlb.seasons ORDER BY year")).fetchall()
        print(f"Found {len(seasons)} seasons")

        total_fixed = 0
        for season_id, year in seasons:
            print(f"\nProcessing {year} season (id={season_id})...")

            result = conn.execute(
                text("""
                    SELECT g.id, g.date, g.home_team_id, g.away_team_id,
                           g.home_wins, g.home_losses, g.away_wins, g.away_losses,
                           g.status, g.home_score, g.away_score
                    FROM mlb.games g
                    WHERE g.season_id = :sid
                    ORDER BY g.date, g.id
                """),
                {"sid": season_id}
            )
            games = result.fetchall()
            final_count = sum(1 for g in games if g[8] == 'FINAL')
            print(f"  {len(games)} total games ({final_count} final)")

            team_wins = {}
            team_losses = {}
            season_fixed = 0

            for g in games:
                gid = g[0]
                home_id = g[2]
                away_id = g[3]
                stored_hw, stored_hl = g[4], g[5]
                stored_aw, stored_al = g[6], g[7]
                status = g[8]
                home_score = g[9]
                away_score = g[10]

                # Before this game, what are the cumulative records?
                hw = team_wins.get(home_id, 0)
                hl = team_losses.get(home_id, 0)
                aw = team_wins.get(away_id, 0)
                al = team_losses.get(away_id, 0)

                if (stored_hw != hw or stored_hl != hl or
                    stored_aw != aw or stored_al != al):
                    conn.execute(
                        text("""
                            UPDATE mlb.games
                            SET home_wins = :hw, home_losses = :hl,
                                away_wins = :aw, away_losses = :al
                            WHERE id = :gid
                        """),
                        {"hw": hw, "hl": hl, "aw": aw, "al": al, "gid": gid}
                    )
                    season_fixed += 1

                # Update cumulative totals based on this game's result
                if status == 'FINAL' and home_score is not None and away_score is not None:
                    if home_score > away_score:
                        team_wins[home_id] = team_wins.get(home_id, 0) + 1
                        team_losses[away_id] = team_losses.get(away_id, 0) + 1
                    elif away_score > home_score:
                        team_wins[away_id] = team_wins.get(away_id, 0) + 1
                        team_losses[home_id] = team_losses.get(home_id, 0) + 1

            if season_fixed > 0:
                conn.commit()
            total_fixed += season_fixed
            print(f"  Fixed {season_fixed} games in {year}")

    print(f"\n{'='*50}")
    print(f"Total games fixed: {total_fixed}")
    print(f"{'='*50}")

if __name__ == "__main__":
    run()
