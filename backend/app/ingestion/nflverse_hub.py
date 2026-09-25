"""nflverse_hub — authoritative nflverse -> Postgres canonical loader.

Design goals (accuracy first):
  * Single source of truth = official nflverse-data release assets only.
  * Preserve source types and NULLs exactly (no NaN->0 coercion).
  * Idempotent per season: DELETE season range, then COPY in.
  * Every load is validated (row counts vs source, per-season).

Canonical tables live in the `nfl` schema with the SAME column names as
nflverse so that docs / nflfastR references map 1:1.

Usage (library):
    from app.ingestion.nflverse_hub import DATASETS, backfill
    await backfill("pbp", 1999, 2015)

CLI: app/scripts/backfill_nflverse_history.py
"""
from __future__ import annotations

import asyncio
import gzip
import io
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
BASE = "https://github.com/nflverse/nflverse-data/releases/download"
CACHE_DIR = Path(
    os.environ.get(
        "NFLVERSE_CACHE",
        str(Path(__file__).resolve().parents[2] / "var" / "nflverse_cache"),
    )
)
CACHE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Dataset:
    key: str                 # our handle
    tag: str                 # nflverse release tag
    pattern: str             # asset name pattern ({y} -> season)
    table: str               # target table in `nfl`
    yearly: bool = True
    start: int = 1999
    end: int = 2026
    pk: list[str] = field(default_factory=list)
    season_col: str = "season"
    season_from: str | None = None   # id column to parse season from (e.g. nflverse_game_id)
    rolling: bool = False            # asset is a single rolling file covering many seasons
    compression: str | None = None   # None | "gzip"
    fmt: str = "parquet"


DATASETS: dict[str, Dataset] = {
    # ---- Core history (Phase 1) ----
    "pbp": Dataset("pbp", "pbp", "play_by_play_{y}.parquet", "pbp",
                   start=1999, end=2026),
    "stats_team_week": Dataset("stats_team_week", "stats_team",
                               "stats_team_week_{y}.parquet", "stats_team_week",
                               start=1999, end=2026),
    "stats_player_week": Dataset("stats_player_week", "stats_player",
                                 "stats_player_week_{y}.parquet", "stats_player_week",
                                 start=1999, end=2026),
    "schedules": Dataset("schedules", "schedules", "games.parquet", "schedules",
                         yearly=False),
    # ---- Enrichment (Phase 2) ----
    "injuries": Dataset("injuries", "injuries", "injuries_{y}.parquet", "injuries_full",
                        start=2009, end=2026),
    "snap_counts": Dataset("snap_counts", "snap_counts", "snap_counts_{y}.parquet",
                           "snap_counts", start=2012, end=2026),
    "participation": Dataset("participation", "pbp_participation",
                             "pbp_participation_{y}.parquet", "participation",
                             start=2016, end=2025, season_from="nflverse_game_id"),
    "ngs_passing": Dataset("ngs_passing", "nextgen_stats", "ngs_passing.parquet",
                           "ngs_passing", start=2016, end=2026, rolling=True,
                           fmt="parquet"),
    "ngs_receiving": Dataset("ngs_receiving", "nextgen_stats", "ngs_receiving.parquet",
                             "ngs_receiving", start=2016, end=2026, rolling=True,
                             fmt="parquet"),
    "ngs_rushing": Dataset("ngs_rushing", "nextgen_stats", "ngs_rushing.parquet",
                           "ngs_rushing", start=2016, end=2026, rolling=True,
                           fmt="parquet"),
    "ftn_charting": Dataset("ftn_charting", "ftn_charting", "ftn_charting_{y}.parquet",
                            "ftn_charting", start=2022, end=2026),
    "advstats_def": Dataset("advstats_def", "pfr_advstats", "advstats_week_def_{y}.parquet",
                            "pfr_advstats_def", start=2018, end=2026),
    "advstats_pass": Dataset("advstats_pass", "pfr_advstats", "advstats_week_pass_{y}.parquet",
                             "pfr_advstats_pass", start=2018, end=2026),
    "advstats_rush": Dataset("advstats_rush", "pfr_advstats", "advstats_week_rush_{y}.parquet",
                             "pfr_advstats_rush", start=2018, end=2026),
    "advstats_rec": Dataset("advstats_rec", "pfr_advstats", "advstats_week_rec_{y}.parquet",
                            "pfr_advstats_rec", start=2018, end=2026),
    "roster_weekly": Dataset("roster_weekly", "weekly_rosters", "roster_weekly_{y}.parquet",
                             "roster_weekly", start=2002, end=2026),
    # ---- Reference ----
    "players": Dataset("players", "players", "players.parquet", "nflverse_players", yearly=False),
    "teams": Dataset("teams", "teams", "teams_colors_logos.parquet", "nflverse_teams", yearly=False),
    "draft_picks": Dataset("draft_picks", "draft_picks", "draft_picks.parquet",
                           "draft_picks_ref", yearly=False),
    "officials": Dataset("officials", "officials", "officials.parquet", "officials",
                         yearly=False),
    "combine": Dataset("combine", "combine", "combine.parquet", "combine_ref", yearly=False),
    "contracts": Dataset("contracts", "contracts", "historical_contracts.parquet",
                         "contracts_ref", yearly=False),
}


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------
def asset_url(ds: Dataset, season: int | None) -> str:
    name = ds.pattern.format(y=season) if (ds.yearly and not ds.rolling) else ds.pattern
    return f"{BASE}/{ds.tag}/{name}"


def _local_path(ds: Dataset, season: int | None) -> Path:
    name = ds.pattern.format(y=season) if (ds.yearly and not ds.rolling) else ds.pattern
    return CACHE_DIR / f"{ds.tag}__{name}"


def download(ds: Dataset, season: int | None, *, force: bool = False,
             retries: int = 4) -> Path:
    path = _local_path(ds, season)
    if path.exists() and not force and path.stat().st_size > 0:
        return path
    url = asset_url(ds, season)
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "earl-nflverse-hub"})
            with urllib.request.urlopen(req, timeout=180) as r:
                data = r.read()
            if not data:
                raise IOError(f"empty response for {url}")
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
            return path
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(f"download failed after {retries} tries: {url} ({last})")


def read_asset(ds: Dataset, season: int | None) -> pd.DataFrame:
    path = download(ds, season)
    if ds.fmt == "csv":
        df = pd.read_csv(path, compression=ds.compression or "infer",
                         low_memory=False)
    else:
        df = pd.read_parquet(path)
    if ds.rolling and season is not None and ds.season_col in df.columns:
        df = df[pd.to_numeric(df[ds.season_col], errors="coerce") == season].copy()
    return df


# --------------------------------------------------------------------------
# Schema / types
# --------------------------------------------------------------------------
def pg_type(series: pd.Series) -> str:
    dt = series.dtype
    if pd.api.types.is_bool_dtype(dt):
        return "BOOLEAN"
    if pd.api.types.is_integer_dtype(dt):
        return "BIGINT"
    if pd.api.types.is_float_dtype(dt):
        return "DOUBLE PRECISION"
    if pd.api.types.is_datetime64_any_dtype(dt):
        return "TIMESTAMP"
    return "TEXT"


def _arrow_cat(t) -> str:
    s = str(t)
    if s.startswith(("int", "uint")):
        return "int"
    if s.startswith(("float", "decimal")):
        return "float"
    if s.startswith("bool"):
        return "bool"
    if s.startswith(("timestamp", "date")):
        return "ts"
    if s == "null":
        return "null"
    if s.startswith(("string", "large_string", "dictionary")):
        return "text"
    return "other"


def _pd_cat(dt) -> str:
    if pd.api.types.is_bool_dtype(dt):
        return "bool"
    if pd.api.types.is_integer_dtype(dt):
        return "int"
    if pd.api.types.is_float_dtype(dt):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(dt):
        return "ts"
    return "text"


def _cat_to_pg(cats: set[str]) -> str:
    """Column type from the UNION of per-season categories (accuracy-safe)."""
    if "text" in cats or "other" in cats:
        return "TEXT"
    if "ts" in cats:
        return "TIMESTAMP"
    if "float" in cats:
        return "DOUBLE PRECISION"
    if "bool" in cats and "int" not in cats:
        return "BOOLEAN"
    if "int" in cats:
        return "BIGINT"
    return "TEXT"


def dataset_type_plan(ds: "Dataset", seasons: list[int | None] | None = None,
                      *, force: bool = False) -> dict[str, str]:
    """Scan every season's schema and pick one PG type per column (union)."""
    import json
    cache = CACHE_DIR / f"schema_{ds.key}.json"
    if cache.exists() and not force:
        return json.loads(cache.read_text())
    if ds.yearly and seasons is None:
        seasons = list(range(ds.start, ds.end + 1))
    elif not ds.yearly:
        seasons = [None]
    try:
        import pyarrow.parquet as pq
    except Exception:  # noqa: BLE001
        pq = None
    col_cats: dict[str, set] = {}
    order: list[str] = []
    for s in seasons:
        try:
            path = download(ds, s)
        except Exception as e:  # noqa: BLE001
            print(f"  [plan] skip {ds.key} season={s}: {e}")
            continue
        if ds.fmt == "parquet":
            # Read via pandas so the plan matches the dtypes we will actually COPY
            # (nullable ints materialise as float64, which pyarrow schema would hide).
            df = pd.read_parquet(path)
            items = [(c, _pd_cat(df[c].dtype)) for c in df.columns]
        else:
            df = pd.read_csv(path, compression=ds.compression or "infer", low_memory=False)
            items = [(c, _pd_cat(df[c].dtype)) for c in df.columns]
        for name, cat in items:
            if name not in col_cats:
                col_cats[name] = set()
                order.append(name)
            col_cats[name].add(cat)
    plan = {name: _cat_to_pg(col_cats[name]) for name in order}
    cache.write_text(json.dumps(plan))
    return plan


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def to_records(df: pd.DataFrame, columns: list[str]) -> list[tuple]:
    sub = df.reindex(columns=columns)
    recs: list[tuple] = []
    obj = sub.to_numpy(dtype=object)
    for row in obj:
        vals = []
        for v in row:
            if v is None:
                vals.append(None)
            elif isinstance(v, float) and np.isnan(v):
                vals.append(None)
            elif isinstance(v, (np.floating,)):
                vals.append(float(v))
            elif isinstance(v, (np.integer,)):
                vals.append(int(v))
            elif isinstance(v, (np.bool_,)):
                vals.append(bool(v))
            elif isinstance(v, pd.Timestamp):
                vals.append(v.to_pydatetime())
            elif isinstance(v, float) and np.isinf(v):
                vals.append(None)
            else:
                vals.append(v)
        recs.append(tuple(vals))
    return recs


# --------------------------------------------------------------------------
# DB load
# --------------------------------------------------------------------------
def _dsn() -> str:
    from app.db_urls import PSYCOPG2_DATABASE_URL, ASYNC_ADMIN_DATABASE_URL
    # prefer admin (DDL) — ASYNC_ADMIN is +asyncpg; strip driver suffix
    url = ASYNC_ADMIN_DATABASE_URL.replace("+asyncpg", "")
    return url


async def ensure_table(conn, ds: Dataset, df: pd.DataFrame,
                       plan: dict[str, str] | None = None) -> list[str]:
    """Create table if missing; add any new columns present in df. Returns col list.

    Types come from the cross-season union `plan` when available (accuracy-safe),
    otherwise fall back to the current frame's dtype.
    """
    def _type_of(c: str) -> str:
        if plan and c in plan:
            return plan[c]
        return pg_type(df[c])

    schema = "nfl"
    exists = await conn.fetchval(
        "SELECT 1 FROM information_schema.tables WHERE table_schema=$1 AND table_name=$2",
        schema, ds.table,
    )
    cols = list(df.columns)
    if not exists:
        defs = ", ".join(f"{_ident(c)} {_type_of(c)}" for c in cols)
        await conn.execute(f'CREATE TABLE {schema}.{_ident(ds.table)} ({defs})')
    else:
        have = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name=$2",
                schema, ds.table,
            )
        }
        for c in cols:
            if c not in have:
                await conn.execute(
                    f'ALTER TABLE {schema}.{_ident(ds.table)} ADD COLUMN {_ident(c)} {_type_of(c)}'
                )
    return cols


async def _delete_season(conn, ds: Dataset, season: int | None, df: pd.DataFrame) -> None:
    tbl = f'nfl.{_ident(ds.table)}'
    if not ds.yearly:
        await conn.execute(f"DELETE FROM {tbl}")
        return
    if ds.season_col in df.columns:
        await conn.execute(
            f'DELETE FROM {tbl} WHERE {_ident(ds.season_col)}::text = $1', str(season)
        )
        return
    # fall back: delete by game_id
    if "game_id" in df.columns:
        ids = [str(x) for x in df["game_id"].dropna().unique().tolist()]
        await conn.execute(
            f'DELETE FROM {tbl} WHERE game_id::text = ANY($1::text[])', ids)
        return
    await conn.execute(f"DELETE FROM {tbl}")


def _bulk_copy_csv(df: pd.DataFrame, cols: list[str], table: str) -> None:
    """Fast bulk load via pandas->CSV->COPY (C-level both sides)."""
    import csv
    import tempfile
    import psycopg2
    sub = df.reindex(columns=cols)
    # normalise non-finite floats to NULL (Postgres rejects 'inf' in CSV)
    for c in sub.columns:
        if pd.api.types.is_float_dtype(sub[c].dtype):
            sub[c] = sub[c].mask(np.isinf(sub[c]))
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
        tmp = f.name
        sub.to_csv(f, index=False, header=False, na_rep="",
                   quoting=csv.QUOTE_MINIMAL)
    collist = ", ".join(_ident(c) for c in cols)
    try:
        with psycopg2.connect(_psycopg2_dsn()) as pg:
            with pg.cursor() as cur:
                with open(tmp, "r", newline="") as fh:
                    cur.copy_expert(
                        f"COPY nfl.{_ident(table)} ({collist}) FROM STDIN "
                        f"WITH (FORMAT csv, NULL '')",
                        fh,
                    )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _psycopg2_dsn() -> str:
    from app.db_urls import ASYNC_ADMIN_DATABASE_URL
    return ASYNC_ADMIN_DATABASE_URL.replace("+asyncpg", "")


async def load_season(conn, ds: Dataset, season: int | None, *, recreate: bool = False,
                      plan: dict[str, str] | None = None) -> dict:
    df = read_asset(ds, season)
    # derive a season column when the asset has no native one (e.g. participation)
    if ds.yearly and ds.season_col not in df.columns:
        src = ds.season_from or ("game_id" if "game_id" in df.columns else None)
        if src and src in df.columns:
            df[ds.season_col] = pd.to_numeric(
                df[src].astype(str).str.slice(0, 4), errors="coerce")
            if plan is not None and ds.season_col not in plan:
                plan = {**plan, ds.season_col: "INTEGER"}
    src_rows = len(df)
    if plan is None:
        plan = dataset_type_plan(ds, [season] if ds.yearly else None)
    if recreate:
        await conn.execute(f'DROP TABLE IF EXISTS nfl.{_ident(ds.table)} CASCADE')
    cols = await ensure_table(conn, ds, df, plan)
    await _delete_season(conn, ds, season, df)
    if src_rows:
        await asyncio.get_event_loop().run_in_executor(
            None, _bulk_copy_csv, df, cols, ds.table
        )
    loaded = await conn.fetchval(f'SELECT count(*) FROM nfl.{_ident(ds.table)}')
    return {"dataset": ds.key, "season": season, "source_rows": src_rows,
            "table_rows_total": loaded}


async def backfill(key: str, start: int | None = None, end: int | None = None,
                   *, newest_first: bool = True, recreate: bool = False,
                   dry_run: bool = False, plan: dict[str, str] | None = None) -> list[dict]:
    import asyncpg
    ds = DATASETS[key]
    if not ds.yearly:
        seasons = [None]
    else:
        s = start if start is not None else ds.start
        e = end if end is not None else ds.end
        seasons = list(range(s, e + 1))
        if newest_first:
            seasons = seasons[::-1]
    out = []
    conn = await asyncpg.connect(_dsn())
    try:
        if plan is None:
            plan = dataset_type_plan(ds, seasons)
        for i, season in enumerate(seasons):
            if dry_run:
                print(f"[dry-run] {key} season={season} url={asset_url(ds, season)}")
                continue
            t0 = time.time()
            try:
                info = await load_season(conn, ds, season,
                                        recreate=recreate and i == 0, plan=plan)
                info["secs"] = round(time.time() - t0, 1)
                out.append(info)
                print(f"[load] {key} season={season} src={info['source_rows']} "
                      f"total={info['table_rows_total']} ({info['secs']}s)")
            except Exception as ex:  # noqa: BLE001
                print(f"[FAIL] {key} season={season}: {ex!r}")
                raise
    finally:
        await conn.close()
    return out
