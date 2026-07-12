"""
Ingest NFL play-by-play data from nflverse parquet files.

Downloads PBP parquet files for specified seasons, extracts key columns,
computes third/fourth down conversion flags, and inserts into nfl.play_by_play.

Usage:
    from app.database import get_db
    await ingest_nfl_pbp(db, years=[2025, 2024, 2023], replace=True)
"""

import logging
from typing import Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PlayByPlay

logger = logging.getLogger("earl.nflverse_pbp")

NFLVERSE_PBP_URL = (
    "https://github.com/nflverse/nflverse-data/releases/download/pbp/"
    "play_by_play_{year}.parquet"
)

# Columns we need from the nflverse PBP parquet
PBP_COLUMNS = [
    "game_id", "old_game_id", "season", "week", "season_type",
    "posteam", "defteam", "posteam_type",
    "down", "ydstogo", "yardline_100",
    "play_type", "play_id", "drive", "qtr",
    "first_down", "yards_gained",
    "game_seconds_remaining", "quarter_seconds_remaining",
    "pass", "rush", "complete_pass", "interception", "fumble_lost",
    "touchdown", "sp", "timeout", "timeout_team", "desc",
]


def _compute_conversion_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Compute third_down_converted/attempted and fourth_down_converted/attempted.

    These aren't native columns in nflverse PBP, so we derive them:

    - third_down_attempted: play on 3rd down (not a timeout, kneel, spike, or end of half)
    - third_down_converted: 3rd down play that results in first_down=1 or touchdown=1
    - Same logic for 4th down.
    """
    # Filter out non-plays: timeouts, kneels, spikes, missing data
    is_play = (
        (df["timeout"] != 1) &
        (~df["play_type"].isin(["no_play", "qb_kneel", "qb_spike"])) &
        (~df["play_type"].isna()) &
        (df["down"].notna())
    )

    df = df.copy()
    df["third_down_attempted"] = 0
    df["fourth_down_attempted"] = 0
    df["third_down_converted"] = 0
    df["fourth_down_converted"] = 0

    play_mask = is_play & (df["down"] > 0) & (df["down"] <= 4)

    # 3rd down attempts
    d3 = play_mask & (df["down"] == 3)
    df.loc[d3, "third_down_attempted"] = 1
    # 3rd down conversions: first_down=1 or touchdown=1
    d3_conv = d3 & ((df["first_down"] == 1) | (df["touchdown"] == 1))
    df.loc[d3_conv, "third_down_converted"] = 1

    # 4th down attempts
    d4 = play_mask & (df["down"] == 4)
    df.loc[d4, "fourth_down_attempted"] = 1
    # 4th down conversions: first_down=1 or touchdown=1
    d4_conv = d4 & ((df["first_down"] == 1) | (df["touchdown"] == 1))
    df.loc[d4_conv, "fourth_down_converted"] = 1

    return df


CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS nfl.play_by_play (
    id BIGSERIAL PRIMARY KEY,
    game_id VARCHAR(50) NOT NULL,
    old_game_id VARCHAR(50),
    season INTEGER,
    week INTEGER,
    season_type VARCHAR(10),
    posteam VARCHAR(5),
    defteam VARCHAR(5),
    posteam_type VARCHAR(5),
    down INTEGER,
    ydstogo DOUBLE PRECISION,
    yardline_100 DOUBLE PRECISION,
    play_type VARCHAR(50),
    play_id BIGINT,
    drive DOUBLE PRECISION,
    qtr INTEGER,
    first_down INTEGER,
    third_down_converted INTEGER,
    third_down_attempted INTEGER,
    fourth_down_converted INTEGER,
    fourth_down_attempted INTEGER,
    yards_gained DOUBLE PRECISION,
    game_seconds_remaining DOUBLE PRECISION,
    quarter_seconds_remaining DOUBLE PRECISION,
    pass_attempt INTEGER,
    rush_attempt INTEGER,
    complete_pass INTEGER,
    interception INTEGER,
    fumble_lost INTEGER,
    touchdown INTEGER,
    scoring_play INTEGER,
    timeout INTEGER,
    timeout_team VARCHAR(5),
    desc_text TEXT,
    CONSTRAINT uq_pbp_game_play UNIQUE (old_game_id, play_id)
);

CREATE INDEX IF NOT EXISTS idx_pbp_season ON nfl.play_by_play (season);
CREATE INDEX IF NOT EXISTS idx_pbp_old_game_id ON nfl.play_by_play (old_game_id);
CREATE INDEX IF NOT EXISTS idx_pbp_posteam ON nfl.play_by_play (posteam);
"""


def _safe_int(v, default=0):
    """Safely extract an int from a pandas record value, handling NaN."""
    if v is None or (isinstance(v, float) and (v != v)):
        return default
    return int(v)


def _safe_float(v, default=0.0):
    """Safely extract a float from a pandas record value, handling NaN."""
    if v is None or (isinstance(v, float) and (v != v)):
        return default
    return float(v)


def _safe_str(v, default=""):
    """Safely extract a str from a pandas record value, handling NaN."""
    if v is None or (isinstance(v, float) and (v != v)):
        return default
    return str(v)


async def _ensure_table(db: AsyncSession):
    """Create the play_by_play table if it doesn't exist."""
    # Split into individual statements since asyncpg doesn't support multi-statement
    statements = [s.strip() for s in CREATE_TABLE_SQL.split(";") if s.strip()]
    for stmt in statements:
        await db.execute(text(stmt))
    await db.commit()


async def ingest_nfl_pbp(
    db: AsyncSession,
    years: list[int],
    replace: bool = False,
    batch_size: int = 5000,
) -> dict:
    """Download and ingest nflverse PBP data for the given seasons.

    Args:
        db: Async SQLAlchemy session.
        years: List of season years to ingest (e.g. [2025, 2024]).
        replace: If True, delete existing rows for these seasons first.
        batch_size: Number of rows to insert per batch.

    Returns:
        dict with counts of loaded, skipped, and errors.
    """
    await _ensure_table(db)
    loaded = 0
    skipped = 0
    errors = 0

    for year in years:
        logger.info(f"Downloading nflverse PBP data for {year}...")
        url = NFLVERSE_PBP_URL.format(year=year)

        try:
            df = pd.read_parquet(
                url,
                columns=PBP_COLUMNS,
            )
        except Exception as e:
            logger.error(f"Failed to download PBP for {year}: {e}")
            errors += 1
            continue

        # Filter to regular season and postseason only
        df = df[df["season_type"].isin(["REG", "POST"])].copy()

        if df.empty:
            logger.warning(f"No PBP data for {year} after filtering")
            skipped += 1
            continue

        # Cast key columns
        df["old_game_id"] = df["old_game_id"].astype(str).str.strip()
        df["game_id"] = df["game_id"].astype(str).str.strip()
        df["play_id"] = df["play_id"].astype("Int64")
        df["season"] = df["season"].astype(int)
        df["week"] = df["week"].astype(int)
        df["down"] = df["down"].fillna(0).astype(int)
        df["qtr"] = df["qtr"].fillna(0).astype(int)
        df["play_type"] = df["play_type"].fillna("").astype(str)
        df["desc"] = df["desc"].fillna("").astype(str)
        df["posteam"] = df["posteam"].fillna("").astype(str)
        df["defteam"] = df["defteam"].fillna("").astype(str)
        df["posteam_type"] = df["posteam_type"].fillna("").astype(str)
        df["timeout_team"] = df["timeout_team"].fillna("").astype(str)

        # Compute conversion flags
        df = _compute_conversion_flags(df)

        logger.info(
            f"Loaded {len(df)} plays for {year} "
            f"({df['game_id'].nunique()} games)"
        )

        # Prepare for bulk insert
        records = df.to_dict("records")

        if replace:
            logger.info(f"Removing existing PBP rows for {year}...")
            await db.execute(
                text("DELETE FROM nfl.play_by_play WHERE season = :season"),
                {"season": year},
            )
            await db.flush()

        # Insert in batches
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            batch_objs = []
            for rec in batch:

                pbp = PlayByPlay(
                    game_id=_safe_str(rec.get("game_id")),
                    old_game_id=_safe_str(rec.get("old_game_id")),
                    season=_safe_int(rec.get("season")),
                    week=_safe_int(rec.get("week")),
                    season_type=_safe_str(rec.get("season_type")),
                    posteam=_safe_str(rec.get("posteam")),
                    defteam=_safe_str(rec.get("defteam")),
                    posteam_type=_safe_str(rec.get("posteam_type")),
                    down=_safe_int(rec.get("down")),
                    ydstogo=_safe_float(rec.get("ydstogo")),
                    yardline_100=_safe_float(rec.get("yardline_100")),
                    play_type=_safe_str(rec.get("play_type")),
                    play_id=_safe_int(rec.get("play_id")),
                    drive=_safe_float(rec.get("drive")),
                    qtr=_safe_int(rec.get("qtr")),
                    first_down=_safe_int(rec.get("first_down")),
                    yards_gained=_safe_float(rec.get("yards_gained")),
                    game_seconds_remaining=_safe_float(
                        rec.get("game_seconds_remaining")
                    ),
                    quarter_seconds_remaining=_safe_float(
                        rec.get("quarter_seconds_remaining")
                    ),
                    pass_attempt=_safe_int(rec.get("pass")),
                    rush_attempt=_safe_int(rec.get("rush")),
                    complete_pass=_safe_int(rec.get("complete_pass")),
                    interception=_safe_int(rec.get("interception")),
                    fumble_lost=_safe_int(rec.get("fumble_lost")),
                    touchdown=_safe_int(rec.get("touchdown")),
                    scoring_play=_safe_int(rec.get("sp")),
                    timeout=_safe_int(rec.get("timeout")),
                    timeout_team=_safe_str(rec.get("timeout_team")),
                    desc=_safe_str(rec.get("desc")),
                    third_down_attempted=_safe_int(
                        rec.get("third_down_attempted")
                    ),
                    third_down_converted=_safe_int(
                        rec.get("third_down_converted")
                    ),
                    fourth_down_attempted=_safe_int(
                        rec.get("fourth_down_attempted")
                    ),
                    fourth_down_converted=_safe_int(
                        rec.get("fourth_down_converted")
                    ),
                )
                batch_objs.append(pbp)

            db.add_all(batch_objs)
            await db.flush()
            loaded += len(batch_objs)

        logger.info(f"Finished PBP ingestion for {year}: {loaded} plays total")
        await db.commit()

    logger.info(
        f"PBP ingestion complete: {loaded} loaded, {skipped} skipped, "
        f"{errors} errors"
    )
    return {"loaded": loaded, "skipped": skipped, "errors": errors}
