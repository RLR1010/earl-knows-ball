"""External MLB research-source ingest (reliable, unprotected sources).

Sources:
  * Baseball-Reference WAR (via pybaseball `bwar_bat` / `bwar_pitch`)  -> 1871+ (we load 2002+)
  * Baseball Savant / Statcast expected-stats leaderboards (via pybaseball) -> 2015+

Generic dataframe loader: sanitizes columns, creates/extends the target table, and loads
idempotently per season (DELETE season -> INSERT). Dev-first.
"""
from __future__ import annotations

import asyncio
import logging
import re

import pandas as pd
from sqlalchemy import text

from app.database import async_session

log = logging.getLogger("earl.mlb_research")


def _san(name: str) -> str:
    return re.sub(r"\W+", "_", str(name).strip().lower()).strip("_")


def _pgtype(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "bigint"
    if pd.api.types.is_float_dtype(series):
        return "double precision"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "timestamptz"
    return "text"


def _clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, (int, float, str, bool)):
        return v
    return str(v)


async def _ensure_table(table: str, df: pd.DataFrame) -> list[str]:
    cols = [_san(c) for c in df.columns]
    types = {c: _pgtype(df[col]) for c, col in zip(cols, df.columns)}
    coldefs = ",\n  ".join(f'"{c}" {types[c]}' for c in cols)
    async with async_session() as s:
        await s.execute(text(
            f'CREATE TABLE IF NOT EXISTS mlb.{table} (\n  {coldefs},\n'
            f'  loaded_at timestamptz NOT NULL DEFAULT now()\n);'))
        for c in cols:
            await s.execute(text(f'ALTER TABLE mlb.{table} ADD COLUMN IF NOT EXISTS "{c}" {types[c]}'))
        await s.commit()
    return cols


async def load_df(table: str, df: pd.DataFrame, season_col: str) -> int:
    if df is None or df.empty:
        return 0
    df = df.copy()
    df.columns = [_san(c) for c in df.columns]
    season_col = _san(season_col)
    cols = await _ensure_table(table, df)
    collist = ",".join(f'"{c}"' for c in cols)
    placeholders = ",".join(":" + c for c in cols)
    stmt = text(f'INSERT INTO mlb.{table} ({collist}) VALUES ({placeholders})')
    total = 0
    for season, part in df.groupby(season_col):
        records = [{k: _clean(v) for k, v in rec.items()} for rec in part.to_dict("records")]
        try:
            y = int(season)
        except (TypeError, ValueError):
            y = None
        async with async_session() as s:
            await s.execute(text(f'DELETE FROM mlb.{table} WHERE "{season_col}" IS NOT DISTINCT FROM :y'), {"y": y})
            if records:
                await s.execute(stmt, records)
            await s.commit()
        total += len(records)
        log.info("%s %s=%s: %d rows", table, season_col, season, len(records))
    return total


# ---------------------------------------------------------------- Baseball-Reference WAR
async def ingest_bwar(start: int = 2002, end: int = 2026) -> dict:
    import pybaseball as pb
    bat = await asyncio.to_thread(pb.bwar_bat)
    pit = await asyncio.to_thread(pb.bwar_pitch)
    bat = bat[(bat["year_ID"] >= start) & (bat["year_ID"] <= end)]
    pit = pit[(pit["year_ID"] >= start) & (pit["year_ID"] <= end)]
    nb = await load_df("bref_war_bat", bat, "year_ID")
    np_ = await load_df("bref_war_pitch", pit, "year_ID")
    return {"bref_war_bat": nb, "bref_war_pitch": np_}


# ---------------------------------------------------------------- Savant expected stats
async def ingest_savant_expected(seasons) -> dict:
    import pybaseball as pb
    out = {}
    for y in seasons:
        b = await asyncio.to_thread(lambda: pb.statcast_batter_expected_stats(y, minPA=1))
        p = await asyncio.to_thread(lambda: pb.statcast_pitcher_expected_stats(y, minPA=1))
        out["savant_bat_expected"] = out.get("savant_bat_expected", 0) + await load_df("savant_bat_expected", b, "year")
        out["savant_pitch_expected"] = out.get("savant_pitch_expected", 0) + await load_df("savant_pitch_expected", p, "year")
    return out
