"""
Pre-compute and store cumulative (season-to-date-before-game) MLB stats.

Computes running totals from batting_game_stats and pitcher_game_stats,
storing one row per (game_id, team_side) so the big GAME_QUERY in
data_loader.py no longer needs expensive window-function recalculations.

Incremental — only processes games that don't already have a row in
cumulative_game_stats.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text as sa_text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

# ── Column mappings: given_name → db_column_name ──────────────────────────

BAT_COL_MAP = {
    "at_bats": "at_bats",
    "hits": "hits",
    "runs": "runs",
    "doubles": "doubles",
    "triples": "triples",
    "home_runs": "home_runs",
    "walks": "base_on_balls",
    "strikeouts": "strikeouts",
    "stolen_bases": "stolen_bases",
    "caught_stealing": "caught_stealing",
    "hbp": "hit_by_pitch",
    "sac_flies": "sacrifice_flies",
    "total_bases": "total_bases",
    "plate_appearances": "plate_appearances",
}

PITCH_COL_MAP = {
    "ip": "ip",
    "er": "er",
    "hits_allowed": "h",
    "walks_allowed": "bb",
    "strikeouts": "k",
    "home_runs_allowed": "hr",
    "hit_by_pitch": "hit_by_pitch",
    "batters_faced": "batters_faced",
}

CUM_TABLE = "mlb.cumulative_game_stats"

CREATE_TABLE_SQL = f"""\
CREATE TABLE IF NOT EXISTS {CUM_TABLE} (
    game_id     INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    team_side   TEXT    NOT NULL CHECK (team_side IN ('home', 'away')),
    season_id   INTEGER NOT NULL,
    game_date   DATE    NOT NULL,

    -- Batting accumulators
    bat_at_bats              INTEGER DEFAULT 0,
    bat_hits                 INTEGER DEFAULT 0,
    bat_runs                 INTEGER DEFAULT 0,
    bat_doubles              INTEGER DEFAULT 0,
    bat_triples              INTEGER DEFAULT 0,
    bat_home_runs            INTEGER DEFAULT 0,
    bat_walks                INTEGER DEFAULT 0,
    bat_strikeouts           INTEGER DEFAULT 0,
    bat_stolen_bases         INTEGER DEFAULT 0,
    bat_caught_stealing      INTEGER DEFAULT 0,
    bat_hbp                  INTEGER DEFAULT 0,
    bat_sac_flies            INTEGER DEFAULT 0,
    bat_total_bases          INTEGER DEFAULT 0,
    bat_plate_appearances    INTEGER DEFAULT 0,

    -- Derived cumulative batting stats
    cum_avg     DOUBLE PRECISION,
    cum_obp     DOUBLE PRECISION,
    cum_slg     DOUBLE PRECISION,
    cum_ops     DOUBLE PRECISION,
    cum_babip   DOUBLE PRECISION,
    cum_k_rate  DOUBLE PRECISION,
    cum_bb_rate DOUBLE PRECISION,

    -- Pitching accumulators
    pitch_ip                 DOUBLE PRECISION DEFAULT 0,
    pitch_er                 INTEGER DEFAULT 0,
    pitch_hits_allowed       INTEGER DEFAULT 0,
    pitch_walks_allowed      INTEGER DEFAULT 0,
    pitch_strikeouts         INTEGER DEFAULT 0,
    pitch_home_runs_allowed  INTEGER DEFAULT 0,
    pitch_hit_by_pitch       INTEGER DEFAULT 0,
    pitch_batters_faced      INTEGER DEFAULT 0,

    -- Derived cumulative pitching stats
    cum_era     DOUBLE PRECISION,
    cum_whip    DOUBLE PRECISION,
    cum_k9      DOUBLE PRECISION,
    cum_bb9     DOUBLE PRECISION,

    PRIMARY KEY (game_id, team_side)
);
"""

# ── SQL for fetching per-game batting/pitching aggregates ───────────────────

GET_BATTING_GAME_SQL = f"""\
SELECT
    sub.game_id,
    sub.team_id,
    sub.team_side,
    sub.season_id,
    sub.game_date,
    {', '.join(f'sub.{alias}' for alias in BAT_COL_MAP)}
FROM (
    SELECT
        g.id           AS game_id,
        CASE WHEN bg.team_side = 'home' THEN g.home_team_id
             ELSE g.away_team_id
        END            AS team_id,
        bg.team_side   AS team_side,
        g.season_id    AS season_id,
        g.date         AS game_date,
        {', '.join(f'SUM(bg.{db}) AS {alias}' for alias, db in BAT_COL_MAP.items())}
    FROM mlb.batting_game_stats bg
    JOIN mlb.games g ON g.id = bg.game_id
    WHERE g.status = 'FINAL'
      AND g.season_id IS NOT NULL
    GROUP BY g.id, bg.team_side, g.season_id, g.date, g.home_team_id, g.away_team_id
) sub
ORDER BY sub.season_id, sub.team_id, sub.game_date, sub.game_id
"""

# ── Abbreviation mapping ──────────────────────────────────────────────────
# Some data sources (pitcher_game_stats) use different abbreviations
TEAM_ABBR_MAP_SQL = """\
CASE pg.team_abbr
    WHEN 'ATH' THEN 'OAK'
    WHEN 'AZ'  THEN 'ARI'
    ELSE pg.team_abbr
END"""

GET_PITCHING_GAME_SQL = f"""\
SELECT
    g.id           AS game_id,
    t.id           AS team_id,
    CASE WHEN g.home_team_id = t.id THEN 'home' ELSE 'away' END AS team_side,
    g.season_id    AS season_id,
    g.date         AS game_date,
    {', '.join(f'SUM(pg.{db}) AS {alias}' for alias, db in PITCH_COL_MAP.items())}
FROM mlb.pitcher_game_stats pg
JOIN mlb.teams t ON t.abbreviation = {TEAM_ABBR_MAP_SQL}
JOIN mlb.games g ON g.id = pg.game_id
WHERE g.status = 'FINAL'
  AND g.season_id IS NOT NULL
GROUP BY g.id, t.id, g.season_id, g.date, g.home_team_id
ORDER BY g.season_id, t.id, g.date, g.id
"""

# ── Derived stat formulas ───────────────────────────────────────────────────


def _compute_cumulative_batting(row: dict) -> dict:
    """Derive cumulative batting stats from raw accumulators."""
    ab = row.get("bat_at_bats", 0) or 0
    h = row.get("bat_hits", 0) or 0
    bb = row.get("bat_walks", 0) or 0
    hbp = row.get("bat_hbp", 0) or 0
    sf = row.get("bat_sac_flies", 0) or 0
    pa = row.get("bat_plate_appearances", 0) or 0
    tb = row.get("bat_total_bases", 0) or 0
    hr = row.get("bat_home_runs", 0) or 0
    k = row.get("bat_strikeouts", 0) or 0

    avg = round(h / ab, 4) if ab > 0 else 0.0
    obp_denom = ab + bb + hbp + sf
    obp = round((h + bb + hbp) / obp_denom, 4) if obp_denom > 0 else 0.0
    slg = round(tb / ab, 4) if ab > 0 else 0.0
    ops = round(obp + slg, 4)
    babip_denom = ab - k - hr + sf
    babip = round((h - hr) / babip_denom, 4) if babip_denom > 0 else 0.0
    k_rate = round(k / pa, 4) if pa > 0 else 0.0
    bb_rate = round(bb / pa, 4) if pa > 0 else 0.0

    return {
        "cum_avg": avg, "cum_obp": obp, "cum_slg": slg,
        "cum_ops": ops, "cum_babip": babip,
        "cum_k_rate": k_rate, "cum_bb_rate": bb_rate,
    }


def _compute_cumulative_pitching(row: dict) -> dict:
    """Derive cumulative pitching stats from raw accumulators."""
    ip = row.get("pitch_ip", 0.0) or 0.0
    er = row.get("pitch_er", 0) or 0
    ha = row.get("pitch_hits_allowed", 0) or 0
    bb = row.get("pitch_walks_allowed", 0) or 0
    k = row.get("pitch_strikeouts", 0) or 0

    era = round((er / ip) * 9.0, 2) if ip > 0 else 0.0
    whip = round((ha + bb) / ip, 4) if ip > 0 else 0.0
    k9 = round((k / ip) * 9.0, 2) if ip > 0 else 0.0
    bb9 = round((bb / ip) * 9.0, 2) if ip > 0 else 0.0

    return {"cum_era": era, "cum_whip": whip, "cum_k9": k9, "cum_bb9": bb9}


# ── Main populator ─────────────────────────────────────────────────────────


def populate_cumulative_stats(
    db_url: str,
    seasons: Optional[list[int]] = None,
    force_rebuild: bool = False,
) -> dict[str, int]:
    """Populate mlb.cumulative_game_stats from scratch or incrementally.

    Parameters
    ----------
    db_url :
        PostgreSQL connection string (sync).
    seasons :
        If set, only process these season years.  Otherwise, all FINAL games.
    force_rebuild :
        If True, drop and re-create the table completely.
        If False, only process games not yet in the table (incremental).

    Returns
    -------
    dict
        Summary of rows inserted / updated.
    """
    engine = create_engine(db_url)
    try:
        return _populate(engine, seasons=seasons, force_rebuild=force_rebuild)
    finally:
        engine.dispose()


def _populate(
    engine: Engine,
    seasons: Optional[list[int]] = None,
    force_rebuild: bool = False,
) -> dict[str, int]:
    """Internal implementation."""
    summary: dict[str, int] = {"batting_processed": 0, "pitching_processed": 0}

    # ── Ensure table exists ──
    with engine.begin() as conn:
        conn.execute(sa_text(CREATE_TABLE_SQL))
        logger.info("Table %s ready.", CUM_TABLE)

    if force_rebuild:
        with engine.begin() as conn:
            conn.execute(sa_text(f"TRUNCATE {CUM_TABLE}"))
            logger.info("Truncated %s (force_rebuild=True).", CUM_TABLE)

    # ── Load per-game batting aggregates ──
    bat_sql = GET_BATTING_GAME_SQL
    if seasons:
        season_list = ", ".join(str(s) for s in seasons)
        bat_sql = bat_sql.replace(
            "WHERE g.status = 'FINAL'\n  AND g.season_id IS NOT NULL",
            f"WHERE g.status = 'FINAL'\n  AND g.season_id IN ({season_list})",
        )

    batting_df = pd.read_sql(bat_sql, engine)
    logger.info("Loaded %d per-game batting aggregates.", len(batting_df))

    # ── Load per-game pitching aggregates ──
    pitch_sql = GET_PITCHING_GAME_SQL
    if seasons:
        pitch_sql = pitch_sql.replace(
            "WHERE g.status = 'FINAL'\n  AND g.season_id IS NOT NULL",
            f"WHERE g.status = 'FINAL'\n  AND g.season_id IN ({season_list})",
        )
    pitching_df = pd.read_sql(pitch_sql, engine)
    logger.info("Loaded %d per-game pitching aggregates.", len(pitching_df))

    # ── Load existing cumulative rows (game_id, team_side) for incremental skip ──
    existing: set[tuple[int, str]] = set()
    if not force_rebuild:
        existing_df = pd.read_sql(
            f"SELECT game_id, team_side FROM {CUM_TABLE}", engine
        )
        existing = set(
            (int(row["game_id"]), str(row["team_side"]))
            for _, row in existing_df.iterrows()
        )
        logger.info("Already have %d cumulative rows — will skip them.", len(existing))

    # ── Process batting: compute running totals ──
    batting_df = batting_df.sort_values(
        ["team_id", "season_id", "game_date", "game_id"]
    )

    bats_to_write: list[dict] = []
    running: dict[tuple[int, int], dict[str, float]] = {}

    for _, row in batting_df.iterrows():
        key = (int(row["team_id"]), int(row["season_id"]))
        gid = int(row["game_id"])
        side = str(row["team_side"])

        # Convert any NaN/Inf to 0
        row_vals = {
            k: (0 if (v is not None and v != v) or (isinstance(v, float) and math.isinf(v)) else v)
            for k, v in row.items()
        }

        if (gid, side) in existing:
            # Still update running totals for future games
            run = running.setdefault(key, {c: 0.0 for c in BAT_COL_MAP})
            for alias in BAT_COL_MAP:
                v = row_vals.get(alias, 0)
                run[alias] = run.get(alias, 0) + float(v)
            continue

        run = running.get(key)
        if run is None:
            run = {c: 0.0 for c in BAT_COL_MAP}
            running[key] = run

        cum_row: dict = {
            "game_id": gid,
            "team_id": int(row_vals["team_id"]),
            "team_side": side,
            "season_id": int(row_vals["season_id"]),
            "game_date": row_vals["game_date"],
        }

        # Raw accumulators (current running totals = stats BEFORE this game)
        for alias in BAT_COL_MAP:
            cum_row[f"bat_{alias}"] = int(run.get(alias, 0))

        # Derived stats
        cum_row.update(_compute_cumulative_batting(cum_row))

        bats_to_write.append(cum_row)

        # Add this game's stats to running totals
        for alias in BAT_COL_MAP:
            v = row_vals.get(alias, 0)
            run[alias] = run[alias] + float(v)

    summary["batting_processed"] = len(bats_to_write)
    logger.info("Batting cumulative: %d new rows to insert.", len(bats_to_write))

    # ── Process pitching: compute running totals ──
    pitching_df = pitching_df.sort_values(
        ["team_id", "season_id", "game_date", "game_id"]
    )

    pitches_to_write: list[dict] = []
    running_p: dict[tuple[int, int], dict[str, float]] = {}

    for _, row in pitching_df.iterrows():
        key = (int(row["team_id"]), int(row["season_id"]))
        gid = int(row["game_id"])
        side = str(row["team_side"])

        row_vals = {
            k: (0 if (v is not None and v != v) or (isinstance(v, float) and math.isinf(v)) else v)
            for k, v in row.items()
        }

        if (gid, side) in existing:
            run = running_p.setdefault(key, {c: 0.0 for c in PITCH_COL_MAP})
            for alias in PITCH_COL_MAP:
                v = row_vals.get(alias, 0)
                run[alias] = run.get(alias, 0) + float(v)
            continue

        run = running_p.get(key)
        if run is None:
            run = {c: 0.0 for c in PITCH_COL_MAP}
            running_p[key] = run

        cum_row = {
            "game_id": gid,
            "team_id": int(row_vals["team_id"]),
            "team_side": side,
            "season_id": int(row_vals["season_id"]),
            "game_date": row_vals["game_date"],
        }

        for alias in PITCH_COL_MAP:
            raw = run.get(alias, 0)
            if alias == "ip":
                cum_row["pitch_ip"] = round(raw, 1)
            else:
                cum_row[f"pitch_{alias}"] = int(raw)

        cum_row.update(_compute_cumulative_pitching(cum_row))
        pitches_to_write.append(cum_row)

        for alias in PITCH_COL_MAP:
            v = row_vals.get(alias, 0)
            run[alias] = run[alias] + float(v)

    summary["pitching_processed"] = len(pitches_to_write)

    # ── Merge batting and pitching into one row per (game_id, team_side) ──
    merged: dict[tuple[int, str], dict] = {}

    def _empty_cum() -> dict:
        d = {}
        for alias in BAT_COL_MAP:
            d[f"bat_{alias}"] = 0
        for alias in PITCH_COL_MAP:
            d[f"pitch_{alias}"] = 0.0 if alias == "ip" else 0
        for k in [
            "cum_avg", "cum_obp", "cum_slg", "cum_ops",
            "cum_babip", "cum_k_rate", "cum_bb_rate",
            "cum_era", "cum_whip", "cum_k9", "cum_bb9",
        ]:
            d[k] = 0.0
        return d

    meta_cols = ["game_id", "team_id", "team_side", "season_id", "game_date"]

    for row in bats_to_write:
        key = (row["game_id"], row["team_side"])
        entry = _empty_cum()
        for c in meta_cols:
            entry[c] = row[c]
        for k, v in row.items():
            entry[k] = v
        merged[key] = entry

    for row in pitches_to_write:
        key = (row["game_id"], row["team_side"])
        if key not in merged:
            entry = _empty_cum()
            for c in meta_cols:
                entry[c] = row[c]
            for k, v in row.items():
                entry[k] = v
            merged[key] = entry
        else:
            for k, v in row.items():
                if k in meta_cols:
                    continue
                merged[key][k] = v

    if not merged:
        logger.info("No new cumulative rows to insert.")
        return summary

    _bulk_upsert(engine, list(merged.values()))

    summary["total_inserted"] = len(merged)
    return summary


# ── Helper columns list (for INSERT) ────────────────────────────────────────

def _all_columns() -> list[str]:
    cols = ["game_id", "team_id", "team_side", "season_id", "game_date"]
    cols += [f"bat_{a}" for a in BAT_COL_MAP]
    cols += [f"pitch_{a}" for a in PITCH_COL_MAP]
    cols += [
        "cum_avg", "cum_obp", "cum_slg", "cum_ops",
        "cum_babip", "cum_k_rate", "cum_bb_rate",
        "cum_era", "cum_whip", "cum_k9", "cum_bb9",
    ]
    return cols


ALL_COLS = _all_columns()

UPSERT_COLS = [c for c in ALL_COLS if c not in ("game_id", "team_side")]


def _sanitize(val):
    """Replace NaN/Inf/NaT with None and convert Timestamps to date."""
    # IEEE 754: NaN != NaN
    if val is not None and val != val:
        return None
    if isinstance(val, float) and math.isinf(val):
        return None
    if isinstance(val, pd.Timestamp):
        try:
            return val.date()
        except Exception:
            return str(val)
    return val


def _bulk_upsert(engine: Engine, rows: list[dict]) -> None:
    """Bulk upsert into cumulative_game_stats using INSERT ON CONFLICT."""
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]

        placeholders: list[str] = []
        params: dict = {}
        for j, row in enumerate(batch):
            pfx = f"r{j}"
            col_refs = ", ".join(f":{pfx}_{c}" for c in ALL_COLS)
            placeholders.append(f"({col_refs})")

            for col in ALL_COLS:
                val = _sanitize(row.get(col))
                params[f"{pfx}_{col}"] = val

        col_names = ", ".join(ALL_COLS)
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in UPSERT_COLS)

        sql = (
            f"INSERT INTO {CUM_TABLE} ({col_names})\n"
            f"VALUES {', '.join(placeholders)}\n"
            f"ON CONFLICT (game_id, team_side) DO UPDATE SET {update_set}"
        )
        with engine.begin() as conn:
            conn.execute(sa_text(sql), params)

    logger.info("Upserted %d rows into %s.", len(rows), CUM_TABLE)
