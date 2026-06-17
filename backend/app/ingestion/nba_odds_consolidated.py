"""
Consolidate all NBA betting line sources into one clean table.

Per-column consolidation: for each game, the best available source provides each
stat (closing OU, opening OU, spread, moneyline).

NBA currently has fewer sources than MLB (no SBR or GitHub datasets), so the
logic is simpler. Creates/refreshes nba.betting_lines_consolidated.

Usage:
    python3 -m app.ingestion.nba_odds_consolidated
    python3 -m app.ingestion.nba_odds_consolidated --games 123,456
"""
import logging

import pandas as pd
from sqlalchemy import create_engine, text

logger = logging.getLogger("earl.nba_odds_consolidated")

# ── Validation thresholds (NBA ranges) ──
VALID_OU_MIN = 160.0
VALID_OU_MAX = 280.0
VALID_SPREAD_MIN = -20.0
VALID_SPREAD_MAX = 20.0
VALID_ML_MAX_ABS = 2000  # NBA moneylines can be extreme for mismatches

import os
SYNC_DB = os.environ.get("SYNC_DATABASE_URL", "postgresql://earl:earl_dev_pass@localhost:5432/earl_knows_football")


# ── Quality priority (lower = better) ──

def closing_ou_quality(row):
    """Lower = better for closing OU."""
    s = row["source"]
    if s == "the_odds_api_closing":
        return 1   # Best: dedicated closing snapshot
    elif s == "the_odds_api_current":
        return 2   # Good: current real-time odds
    elif s == "the_odds_api":
        return 3   # Standard snapshot
    else:
        return 9


def opening_ou_quality(row):
    """Lower = better for opening OU."""
    s = row["source"]
    if s == "the_odds_api_opening":
        return 1   # Best: dedicated opening snapshot
    elif s == "the_odds_api":
        return 2   # Standard
    elif s == "the_odds_api_current":
        return 3   # Current snapshot, not ideal for opening
    else:
        return 9


def spread_quality(row):
    """Lower = better for spread."""
    s = row["source"]
    if s == "the_odds_api_closing":
        return 1
    elif s == "the_odds_api_current":
        return 2
    elif s == "the_odds_api":
        return 3
    elif s == "the_odds_api_opening":
        return 4
    else:
        return 9


def ml_quality(row):
    """Lower = better for moneyline. Same priority as spread."""
    return spread_quality(row)


def consolidate(game_ids: list[int] | None = None):
    """Build/refresh nba.betting_lines_consolidated.

    When game_ids is provided, does incremental update for those games only.
    When None, does a full rebuild of the entire consolidated table.
    """
    engine = create_engine(SYNC_DB, pool_size=2)

    # ── 1. Load ALL lines with metadata ──
    logger.info("Loading NBA betting line sources...")
    source_filter = "'the_odds_api', 'the_odds_api_opening', 'the_odds_api_closing', 'the_odds_api_current'"

    game_filter = ""
    if game_ids:
        ids_str = ",".join(str(gid) for gid in game_ids)
        game_filter = f"AND bl.game_id IN ({ids_str})"
        logger.info(f"Incremental consolidation for {len(game_ids)} games")
    else:
        logger.info("Full consolidation of all games")

    df = pd.read_sql(f"""
        SELECT
            bl.game_id, bl.source, bl.sportsbook,
            bl.over_under, bl.opening_total,
            bl.opening_spread, bl.spread,
            bl.home_moneyline, bl.away_moneyline,
            bl.opening_home_moneyline, bl.opening_away_moneyline,
            bl.home_implied_probability, bl.away_implied_probability,
            bl.is_opening,
            s.year,
            g.home_score, g.away_score,
            bl.recorded_at
        FROM nba.betting_lines bl
        JOIN nba.games g ON g.id = bl.game_id
        JOIN nba.seasons s ON s.id = g.season_id
        WHERE bl.is_opening IN ('false', 'f', 'true', 't')
          AND bl.source IN ({source_filter})
          {game_filter}
    """, engine)
    logger.info(f"  Loaded {len(df)} lines from {df['source'].nunique()} sources")

    if len(df) == 0:
        logger.warning("No lines loaded — nothing to consolidate")
        return 0

    # ── 2. Validate each line ──
    logger.info("Validating lines...")
    valid_ou = df["over_under"].between(VALID_OU_MIN, VALID_OU_MAX) | df["over_under"].isna()
    valid_open = df["opening_total"].between(VALID_OU_MIN, VALID_OU_MAX) | df["opening_total"].isna()
    valid_spr = df["spread"].between(VALID_SPREAD_MIN, VALID_SPREAD_MAX) | df["spread"].isna()
    valid_open_spr = df["opening_spread"].between(VALID_SPREAD_MIN, VALID_SPREAD_MAX) | df["opening_spread"].isna()
    valid_ml_home = (df["home_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["home_moneyline"].isna()
    valid_ml_away = (df["away_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["away_moneyline"].isna()
    valid_open_ml = ((df["opening_home_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["opening_home_moneyline"].isna())
    valid_open_ml_a = ((df["opening_away_moneyline"].fillna(0).abs() <= VALID_ML_MAX_ABS) | df["opening_away_moneyline"].isna())

    df["valid"] = valid_ou & valid_open & valid_spr & valid_open_spr & valid_ml_home & valid_ml_away & valid_open_ml & valid_open_ml_a
    invalid = df[~df["valid"]]
    df = df[df["valid"]].copy()
    logger.info(f"  Valid: {len(df)}, Invalid: {len(invalid)}")

    if len(invalid) > 0:
        invalid_summary = invalid["source"].value_counts()
        for src, cnt in invalid_summary.items():
            logger.info(f"    {src}: {cnt} invalid")

    # ── 3. Per-column consolidation ──
    logger.info("Consolidating per column...")

    # All games to consider
    all_game_ids = pd.read_sql("""
        SELECT g.id as game_id, g.date, s.year,
               ht.abbreviation as home_team, at.abbreviation as away_team,
               g.home_score, g.away_score, g.venue, g.status
        FROM nba.games g
        JOIN nba.seasons s ON s.id = g.season_id
        JOIN nba.teams ht ON ht.id = g.home_team_id
        JOIN nba.teams at ON at.id = g.away_team_id
        ORDER BY s.year, g.date, g.id
    """, engine)
    all_game_ids["date"] = pd.to_datetime(all_game_ids["date"])
    if all_game_ids["date"].dt.tz is None:
        all_game_ids["date"] = all_game_ids["date"].dt.tz_localize("UTC")

    # ── 3a. Closing O/U ──
    logger.info("  Closing O/U...")
    has_closing = df[df["over_under"].notna()].copy()
    has_closing["quality"] = has_closing.apply(closing_ou_quality, axis=1)
    best_closing_idx = has_closing.groupby("game_id")["quality"].idxmin()
    best_closing = has_closing.loc[best_closing_idx, ["game_id", "over_under", "source", "sportsbook"]]
    best_closing.columns = ["game_id", "over_under", "ou_source", "ou_sportsbook"]

    # ── 3b. Opening O/U ──
    logger.info("  Opening O/U...")
    is_open_flag = df["is_opening"].str.lower().isin(["true", "t"])
    opening_lu = df["opening_total"].copy()
    # Fallback: if no dedicated opening_total but has opening flag, use over_under
    opening_lu.loc[is_open_flag & opening_lu.isna()] = df.loc[is_open_flag & opening_lu.isna(), "over_under"]
    has_opening = df[opening_lu.notna()].copy()
    has_opening["_ot"] = opening_lu[has_opening.index]
    if len(has_opening) > 0:
        has_opening["quality"] = has_opening.apply(opening_ou_quality, axis=1)
        best_opening_idx = has_opening.groupby("game_id")["quality"].idxmin()
        best_opening = has_opening.loc[best_opening_idx, ["game_id", "_ot", "source", "sportsbook"]]
        best_opening.columns = ["game_id", "opening_total", "ou_open_source", "ou_open_sportsbook"]
    else:
        best_opening = pd.DataFrame(columns=["game_id", "opening_total", "ou_open_source", "ou_open_sportsbook"])

    # ── 3c. Closing Spread ──
    logger.info("  Closing spread...")
    has_spread = df[df["spread"].notna()].copy()
    if len(has_spread) > 0:
        has_spread["quality"] = has_spread.apply(spread_quality, axis=1)
        best_spread_idx = has_spread.groupby("game_id")["quality"].idxmin()
        best_spread = has_spread.loc[best_spread_idx, ["game_id", "spread", "source", "sportsbook"]]
        best_spread.columns = ["game_id", "spread", "sp_source", "sp_sportsbook"]
    else:
        best_spread = pd.DataFrame(columns=["game_id", "spread", "sp_source", "sp_sportsbook"])

    # ── 3d. Opening Spread ──
    logger.info("  Opening spread...")
    open_sp = df["opening_spread"].copy()
    open_sp.loc[is_open_flag & open_sp.isna()] = df.loc[is_open_flag & open_sp.isna(), "spread"]
    has_open_spread = df[open_sp.notna()].copy()
    has_open_spread["_os"] = open_sp[has_open_spread.index]
    if len(has_open_spread) > 0:
        has_open_spread["quality"] = has_open_spread.apply(spread_quality, axis=1)
        best_open_spread_idx = has_open_spread.groupby("game_id")["quality"].idxmin()
        best_open_spread = has_open_spread.loc[best_open_spread_idx, ["game_id", "_os", "source", "sportsbook"]]
        best_open_spread.columns = ["game_id", "opening_spread", "sp_open_source", "sp_open_sportsbook"]
    else:
        best_open_spread = pd.DataFrame(columns=["game_id", "opening_spread", "sp_open_source", "sp_open_sportsbook"])

    # ── 3e. Closing Moneyline ──
    logger.info("  Moneyline...")
    has_ml = df[df["home_moneyline"].notna()].copy()
    if len(has_ml) > 0:
        has_ml["quality"] = has_ml.apply(ml_quality, axis=1)
        best_ml_idx = has_ml.groupby("game_id")["quality"].idxmin()
        best_ml = has_ml.loc[best_ml_idx, [
            "game_id", "home_moneyline", "away_moneyline",
            "home_implied_probability", "away_implied_probability", "source", "sportsbook"
        ]]
        best_ml.columns = [
            "game_id", "home_moneyline", "away_moneyline",
            "home_implied_probability", "away_implied_probability",
            "ml_source", "ml_sportsbook"
        ]
    else:
        best_ml = pd.DataFrame(columns=["game_id", "home_moneyline", "away_moneyline",
                                        "home_implied_probability", "away_implied_probability",
                                        "ml_source", "ml_sportsbook"])

    # ── 3f. Opening Moneyline ──
    logger.info("  Opening moneyline...")
    open_hl = df["opening_home_moneyline"].copy()
    open_al = df["opening_away_moneyline"].copy()
    open_hl.loc[is_open_flag & open_hl.isna()] = df.loc[is_open_flag & open_hl.isna(), "home_moneyline"]
    open_al.loc[is_open_flag & open_al.isna()] = df.loc[is_open_flag & open_al.isna(), "away_moneyline"]
    has_open_ml = df[open_hl.notna()].copy()
    has_open_ml["_ohl"] = open_hl[has_open_ml.index]
    has_open_ml["_oal"] = open_al[has_open_ml.index]
    if len(has_open_ml) > 0:
        has_open_ml["quality"] = has_open_ml.apply(ml_quality, axis=1)
        best_open_ml_idx = has_open_ml.groupby("game_id")["quality"].idxmin()
        best_open_ml = has_open_ml.loc[best_open_ml_idx, [
            "game_id", "_ohl", "_oal", "source", "sportsbook"
        ]]
        best_open_ml.columns = [
            "game_id", "opening_home_moneyline", "opening_away_moneyline",
            "ml_open_source", "ml_open_sportsbook"
        ]
    else:
        best_open_ml = pd.DataFrame(columns=["game_id", "opening_home_moneyline", "opening_away_moneyline", "ml_open_source", "ml_open_sportsbook"])

    # ── 4. Merge all columns ──
    logger.info("Merging per-column picks...")
    result = all_game_ids.copy()

    result = result.merge(best_closing, on="game_id", how="left")
    result = result.merge(best_opening, on="game_id", how="left")
    result = result.merge(best_spread, on="game_id", how="left")
    result = result.merge(best_open_spread, on="game_id", how="left")
    result = result.merge(best_ml, on="game_id", how="left")
    result = result.merge(best_open_ml, on="game_id", how="left")

    # ── 5. Derived fields ──
    home_score = result["home_score"].fillna(0).astype(int)
    away_score = result["away_score"].fillna(0).astype(int)
    result["actual_total"] = home_score + away_score
    result["is_over"] = result["actual_total"] > result["over_under"]
    result["is_under"] = result["actual_total"] < result["over_under"]
    result["is_push"] = result["actual_total"] == result["over_under"]
    result["ou_movement"] = result["over_under"] - result["opening_total"]
    result["spread_movement"] = result["spread"] - result["opening_spread"]

    # Game time categorization
    result["hour_et"] = result["date"].dt.tz_convert("America/New_York").dt.hour
    result["game_time"] = pd.cut(
        result["hour_et"],
        bins=[0, 14, 17, 24],
        labels=["early", "afternoon", "night"],
        right=False,
    )

    # ── 6. Validate: only keep games where we have verified OU (opening + closing) ──
    result["has_verified_ou"] = result["over_under"].notna() & result["opening_total"].notna()
    has_verified_lines = result["spread"].notna() | result["home_moneyline"].notna()
    verified = result[result["has_verified_ou"] & has_verified_lines].copy()
    missing_ou = result[~result["has_verified_ou"] & result["over_under"].notna()]
    no_ou_at_all = result[result["over_under"].isna()]

    logger.info(f"  Games with verified opening+closing OU + lines: {len(verified)}")
    logger.info(f"  Games with closing only (dropped): {len(missing_ou)}")
    logger.info(f"  Games with no OU at all: {len(no_ou_at_all)}")

    # ── 7. Write to table ──
    logger.info("Writing to nba.betting_lines_consolidated...")

    cols = [
        "game_id", "year", "date", "home_team", "away_team",
        "home_score", "away_score", "actual_total", "venue", "game_time",
        "over_under", "opening_total", "ou_movement",
        "ou_source", "ou_sportsbook",
        "ou_open_source", "ou_open_sportsbook",
        "spread", "opening_spread", "spread_movement",
        "sp_source", "sp_sportsbook", "sp_open_source", "sp_open_sportsbook",
        "home_moneyline", "away_moneyline",
        "opening_home_moneyline", "opening_away_moneyline",
        "home_implied_probability", "away_implied_probability",
        "ml_source", "ml_sportsbook",
        "ml_open_source", "ml_open_sportsbook",
        "is_over", "is_under", "is_push", "has_verified_ou",
    ]

    if game_ids:
        result = result[result["game_id"].isin(game_ids)].copy()

    insert_df = result[cols].copy()
    insert_df["date"] = result["date"].dt.tz_convert(None)

    with engine.begin() as conn:
        if game_ids:
            ids_str = ",".join(str(gid) for gid in game_ids)
            conn.execute(text(f"DELETE FROM nba.betting_lines_consolidated WHERE game_id IN ({ids_str})"))
            logger.info(f"  Deleted existing rows for {len(game_ids)} games")

        insert_df.to_sql(
            "betting_lines_consolidated",
            engine,
            schema="nba",
            if_exists="append" if game_ids else "replace",
            index=False,
            method="multi",
        )

        if not game_ids:
            conn.execute(text("DROP INDEX IF EXISTS idx_nba_consolidated_year"))
            conn.execute(text("DROP INDEX IF EXISTS idx_nba_consolidated_verified"))
            conn.execute(text("DROP INDEX IF EXISTS idx_nba_consolidated_game_time"))
            conn.execute(text("CREATE INDEX idx_nba_consolidated_year ON nba.betting_lines_consolidated (year)"))
            conn.execute(text("CREATE INDEX idx_nba_consolidated_verified ON nba.betting_lines_consolidated (has_verified_ou)"))
            conn.execute(text("CREATE INDEX idx_nba_consolidated_game_time ON nba.betting_lines_consolidated (game_time)"))

    logger.info(f"✅ nba.betting_lines_consolidated: {len(insert_df)} rows")

    # ── 8. Summary ──
    summary = pd.read_sql("""
        SELECT
            year,
            count(*)::int as total_games,
            sum((has_verified_ou)::int) as verified_ou,
            sum((over_under IS NOT NULL AND opening_total IS NULL)::int) as closing_only,
            sum((over_under IS NULL)::int) as no_ou,
            round(avg(CASE WHEN has_verified_ou THEN over_under END)::numeric, 1) as avg_closing,
            round(avg(CASE WHEN has_verified_ou THEN opening_total END)::numeric, 1) as avg_opening
        FROM nba.betting_lines_consolidated
        GROUP BY year
        ORDER BY year
    """, engine)

    print()
    print("=== VERIFIED (OPENING + CLOSING) COVERAGE BY YEAR ===")
    print(summary.to_string(index=False))
    print()

    engine.dispose()
    logger.info("Done!")
    return len(verified)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=str, help="Comma-separated game IDs to incrementally consolidate")
    args = parser.parse_args()

    if args.games:
        game_ids = [int(g.strip()) for g in args.games.split(",") if g.strip()]
        consolidate(game_ids=game_ids)
    else:
        consolidate()
