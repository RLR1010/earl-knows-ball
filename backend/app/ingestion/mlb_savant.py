"""Baseball Savant (Statcast) leaderboard ingest — 2015+.

Comprehensive: expected stats, exit-velo/barrels, percentile ranks, pitch arsenal/movement/spin,
defense (OAA / directional OAA / catch prob / outfield jump / catcher poptime + framing),
baserunning (sprint speed / running splits).

Source = Baseball Savant CSV endpoints (structured data, NOT html scraping).
Idempotent per season via `load_df` (delete-season -> insert). Dev-first.
"""
from __future__ import annotations

import asyncio
import io
import logging

import pandas as pd
import pybaseball as pb
import requests
from pybaseball.statcast_pitcher import statcast_pitcher_pitch_movement, statcast_pitcher_active_spin

from app.ingestion.mlb_research import load_df

log = logging.getLogger("earl.mlb_savant")

OAA_POSITIONS = [3, 4, 5, 6, 7, 8, 9]          # 1B..RF (catcher excluded by Savant)
MOVEMENT_PITCH_TYPES = ["FF", "SL", "CH", "CU", "SI", "FC", "KC", "ST", "SV", "FS"]

# (function, table, kwargs, extra_cols)
SIMPLE = [
    (lambda y: pb.statcast_batter_exitvelo_barrels(y), "savant_bat_exitvelo", {}, None),
    (lambda y: pb.statcast_batter_expected_stats(y, minPA=1), "savant_bat_expected", {}, None),
    (lambda y: pb.statcast_batter_percentile_ranks(y), "savant_bat_percentile", {}, None),
    (lambda y: pb.statcast_batter_pitch_arsenal(y, minPA=1), "savant_bat_pitch_arsenal", {}, None),
    (lambda y: pb.statcast_pitcher_exitvelo_barrels(y), "savant_pitch_exitvelo", {}, None),
    (lambda y: pb.statcast_pitcher_expected_stats(y, minPA=1), "savant_pitch_expected", {}, None),
    (lambda y: pb.statcast_pitcher_percentile_ranks(y), "savant_pitch_percentile", {}, None),
    (lambda y: pb.statcast_pitcher_arsenal_stats(y, minPA=1), "savant_pitch_arsenal_stats", {}, None),
    (lambda y: pb.statcast_pitcher_pitch_arsenal(y, minP=1, arsenal_type="avg_speed"), "savant_pitch_arsenal", {}, {"arsenal_type": "avg_speed"}),
    (lambda y: statcast_pitcher_active_spin(y, minP=1), "savant_pitch_active_spin", {}, None),
    (lambda y: pb.statcast_outfield_directional_oaa(y, min_opp=1), "savant_fielding_oaa_directional", {}, None),
    (lambda y: pb.statcast_outfield_catch_prob(y, min_opp=1), "savant_fielding_catch_prob", {}, None),
    (lambda y: pb.statcast_outfielder_jump(y, min_att=1), "savant_fielding_jump", {}, None),
    (lambda y: pb.statcast_catcher_poptime(y, min_2b_att=0, min_3b_att=0), "savant_catcher_poptime", {}, None),
    (lambda y: pb.statcast_sprint_speed(y, min_opp=0), "savant_sprint_speed", {}, None),
    (lambda y: pb.statcast_running_splits(y, min_opp=0, raw_splits=True), "savant_running_splits", {}, None),
]


async def _pull(fn, **log_kw):
    try:
        df = await asyncio.to_thread(fn)
        return df
    except Exception as e:  # Savant datasets vary by year; tolerate gaps
        log.warning("savant pull failed %s: %s", log_kw, str(e)[:140])
        return None


async def ingest_savant_year(year: int) -> dict:
    out = {}
    for fn, table, kw, extra in SIMPLE:
        df = await _pull(lambda fn=fn: fn(year), table=table, year=year)
        if df is not None and not df.empty:
            df["year"] = year
            if extra:
                for k, v in extra.items():
                    df[k] = v
            out[table] = out.get(table, 0) + await load_df(table, df, "year")
        await asyncio.sleep(0.3)

    # OAA is per position -> accumulate ALL positions, then ONE load per season
    oaa_frames = []
    for pos in OAA_POSITIONS:
        df = await _pull(lambda p=pos: pb.statcast_outs_above_average(year, pos=p, min_att=1), table="savant_fielding_oaa", year=year, pos=pos)
        if df is not None and not df.empty:
            df["year"] = year
            df["position"] = pos
            oaa_frames.append(df)
        await asyncio.sleep(0.3)
    if oaa_frames:
        out["savant_fielding_oaa"] = await load_df("savant_fielding_oaa", pd.concat(oaa_frames, ignore_index=True), "year")

    # pitch movement is per pitch type -> accumulate ALL types, then ONE load per season
    mv_frames = []
    for pt in MOVEMENT_PITCH_TYPES:
        df = await _pull(lambda p=pt: statcast_pitcher_pitch_movement(year, minP=1, pitch_type=p), table="savant_pitch_movement", year=year, pitch_type=pt)
        if df is not None and not df.empty:
            df["year"] = year
            df["pitch_type"] = pt
            mv_frames.append(df)
        await asyncio.sleep(0.3)
    if mv_frames:
        out["savant_pitch_movement"] = await load_df("savant_pitch_movement", pd.concat(mv_frames, ignore_index=True), "year")

    # catcher framing: use Savant's modern hyphen endpoint (clean CSV; pybaseball's legacy path breaks)
    try:
        n = await _load_framing(year)
        if n:
            out["savant_catcher_framing"] = n
    except Exception as e:
        log.warning("savant catcher framing failed %d: %s", year, str(e)[:140])

    log.info("savant %d done: %s", year, out)
    return out


async def _load_framing(year: int) -> int:
    """Savant catcher framing (modern endpoint: /leaderboard/catcher-framing -> rv_tot)."""
    url = f"https://baseballsavant.mlb.com/leaderboard/catcher-framing?year={year}&team=&min=1&sort=4&csv=true"
    raw = await asyncio.to_thread(lambda: requests.get(url, timeout=60).content)
    df = pd.read_csv(io.StringIO(raw.decode("utf-8")))
    if df.empty:
        return 0
    df.columns = [str(c).replace('"', "").strip().lower() for c in df.columns]
    if "id" in df.columns:
        df = df.rename(columns={"id": "player_id"})
    if "player_id" in df.columns:
        df = df.loc[df["player_id"].notna()]
    df["year"] = year
    return await load_df("savant_catcher_framing", df, "year")


async def ingest_savant_framing(seasons) -> dict:
    total = 0
    for y in seasons:
        total += await _load_framing(y)
        await asyncio.sleep(0.3)
    return {"savant_catcher_framing": total}


async def ingest_savant(seasons) -> dict:
    total = {}
    for y in seasons:  # caller supplies newest-first
        res = await ingest_savant_year(y)
        for k, v in res.items():
            total[k] = total.get(k, 0) + v
    return total
