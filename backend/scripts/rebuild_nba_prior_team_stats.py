"""Rebuild nba.prior_team_stats (per-team-season final aggregates).

Mirrors mlb.prior_team_stats / nfl.prior_team_stats. For every team-season that has
a completed row in nba.cumulative_game_stats / nba.team_rolling_stats, picks the LAST
game's season-to-date values (max game_date) and stores them keyed by (team_id,
season_year). The NBA data loader LEFT-JOINs this on season_year = current_year - 1
so opening-night games (first game of a season -> no in-season prior) can blend with
the previous season's full-sample stats instead of sending NULL/0 to the model.

Idempotent: upserts on (team_id, season_year).
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import text, create_engine

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.core.config import settings  # noqa: E402

CUM_SRC = [
    "cum_ppg", "cum_oppg", "cum_margin_pg", "cum_fg_pct", "cum_fg3_pct",
    "cum_ft_pct", "cum_reb_pg", "cum_ast_pg", "cum_stl_pg", "cum_blk_pg",
    "cum_tov_pg", "cum_pf_pg", "cum_ortg", "cum_drtg", "cum_net_ortg",
    "cum_pace", "cum_efg_pct", "cum_opp_efg_pct", "cum_tov_rate",
    "cum_opp_tov_rate", "cum_ft_rate", "cum_3pa_rate", "cum_ast_ratio",
    "cum_stl_rate", "cum_blk_rate", "cum_win_pct",
    "cum_adj_ortg", "cum_adj_drtg", "cum_sos",
]
ROLL_SRC = [
    "rw3_ppg", "rw5_ppg", "rw3_net_rtg", "rw5_net_rtg", "rw3_efg_pct",
    "rw5_efg_pct", "rw3_drtg", "rw5_drtg", "cv10_ppg", "cv20_ppg",
    "cv10_net_rtg", "recency_ppg", "recency_net_rtg", "net_rtg_r5",
    "net_rtg_r10", "ortg_r5", "ortg_r10", "drtg_r5", "drtg_r10",
    "efg_r5", "efg_r10", "pace_r5", "pace_r10", "ast_ratio_r5",
    "ast_ratio_r10", "ft_rate_r5", "ft_rate_r10", "threep_rate_r5",
    "threep_rate_r10", "ats_margin_5", "ats_margin_10", "ats_wins_5",
    "ats_wins_10", "ou_wins_5", "ou_wins_10", "ou_margin_5",
    "wins_5", "wins_10", "adj_off_10", "adj_def_10", "star_ppg_5",
    "star1_ppg_5", "stars_active", "star1_active",
]


def main():
    sync_url = settings.database_url.replace("+asyncpg", "+psycopg2")
    engine = create_engine(sync_url)
    with engine.connect() as conn:
        seasons = conn.execute(
            text("SELECT id, year FROM nba.seasons ORDER BY year")
        ).fetchall()
        season_id_to_year = {r[0]: r[1] for r in seasons}
        print(f"Seasons available: {sorted(season_id_to_year.values())}")

        # Final cumulative row per (team, season) — REGULAR SEASON only.
        # Playoffs/play-in carry the same season_id (and are folded into the
        # cumulative/rolling tables), so we must join through nba.games.game_type
        # to take the last regular-season game as the prior (matches NFL).
        cum = conn.execute(text("""
            SELECT c.team_id, c.season_id, c.games_played, c.game_date,
                   {cols}
            FROM (
                SELECT c.*, ROW_NUMBER() OVER (
                    PARTITION BY c.team_id, c.season_id
                    ORDER BY c.game_date DESC, c.game_id DESC
                ) AS rn
                FROM nba.cumulative_game_stats c
                JOIN nba.games gt ON gt.id = c.game_id
                WHERE gt.game_type = 'REG'
            ) c WHERE c.rn = 1
        """.format(cols="c." + (", c.".join(CUM_SRC))))).fetchall()
        print(f"cumulative finals (REG only): {len(cum)} rows")

        # Final rolling row per (team, season) — REGULAR SEASON only.
        roll = conn.execute(text("""
            SELECT c.team_id, c.season_id, c.game_date,
                   {cols}
            FROM (
                SELECT c.*, ROW_NUMBER() OVER (
                    PARTITION BY c.team_id, c.season_id
                    ORDER BY c.game_date DESC, c.game_id DESC
                ) AS rn
                FROM nba.team_rolling_stats c
                JOIN nba.games gt ON gt.id = c.game_id
                WHERE gt.game_type = 'REG'
            ) c WHERE c.rn = 1
        """.format(cols="c." + (", c.".join(ROLL_SRC))))).fetchall()
        print(f"rolling finals (REG only): {len(roll)} rows")

    cum_by_key = {(r.team_id, r.season_id): r for r in cum}
    roll_by_key = {(r.team_id, r.season_id): r for r in roll}

    all_keys = set(cum_by_key) | set(roll_by_key)
    print(f"Unique team-seasons: {len(all_keys)}")

    cum_cols = ", ".join(CUM_SRC)
    roll_cols = ", ".join(ROLL_SRC)

    insert_sql = text(f"""
        INSERT INTO nba.prior_team_stats (
            team_id, team_abbr, season_year, games_played,
            {cum_cols}, {roll_cols}
        ) VALUES (
            :team_id,
            (SELECT abbreviation FROM nba.teams WHERE id = :team_id),
            :season_year, :games_played,
            :{', :'.join(CUM_SRC)}, :{', :'.join(ROLL_SRC)}
        )
        ON CONFLICT (team_id, season_year) DO UPDATE SET
            team_abbr = EXCLUDED.team_abbr,
            games_played = EXCLUDED.games_played,
            {", ".join(f"{c} = EXCLUDED.{c}" for c in CUM_SRC)},
            {", ".join(f"{c} = EXCLUDED.{c}" for c in ROLL_SRC)}
    """)

    rows = []
    for team_id, season_id in sorted(all_keys):
        year = season_id_to_year.get(season_id)
        if year is None:
            continue
        cr = cum_by_key.get((team_id, season_id))
        rr = roll_by_key.get((team_id, season_id))
        row = {
            "team_id": team_id,
            "season_year": year,
            "games_played": (cr.games_played if cr else None) or (rr.games_played if hasattr(rr, "games_played") else None),
        }
        if cr:
            for f in CUM_SRC:
                row[f] = getattr(cr, f)
        if rr:
            for f in ROLL_SRC:
                row[f] = getattr(rr, f)
        rows.append(row)

    with engine.begin() as conn:
        # batch upsert
        for i in range(0, len(rows), 200):
            conn.execute(insert_sql, rows[i:i + 200])
    print(f"Upserted {len(rows)} prior_team_stats rows")

    # Verify
    with engine.connect() as conn:
        n = conn.execute(text("SELECT count(*) FROM nba.prior_team_stats")).scalar()
        null_cum = conn.execute(text(
            "SELECT count(*) FROM nba.prior_team_stats WHERE cum_ppg IS NULL"
        )).scalar()
        yr = conn.execute(text(
            "SELECT season_year, count(*) FROM nba.prior_team_stats GROUP BY 1 ORDER BY 1"
        )).fetchall()
        print(f"\nTOTAL rows={n}, NULL cum_ppg={null_cum}")
        print("BY YEAR:", yr)

    # Confirm game 37960's prior season (2024) has its teams populated
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT p.team_abbr, p.cum_ppg, p.cum_oppg, p.net_rtg_r5, p.ats_wins_5, p.games_played
            FROM nba.prior_team_stats p
            JOIN nba.games g ON g.id = 37960
              AND p.team_id IN (g.home_team_id, g.away_team_id)
            WHERE p.season_year = 2024
            ORDER BY p.team_id
        """)).fetchall()
        print("\nGame 37960 prior-season (2024) rows:")
        for r in rows:
            print("  ", r)


if __name__ == "__main__":
    main()
