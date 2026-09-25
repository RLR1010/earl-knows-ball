"""FanGraphs leaderboard ingestion (2002+).

Source: https://www.fangraphs.com/api/leaders/major-league/data  (JSON, paginated)

Loads player + team SEASON leaderboards for batting / pitching / fielding into canonical
`mlb.fg_*` tables. Each row keeps a curated column set PLUS the full raw FanGraphs record
as `raw jsonb` (so nothing is lost).

Design: additive tables, per-season idempotent (DELETE season -> INSERT), newest-first when
run via the backfill script. Identity key = xMLBAMID (matches MLB Stats API / Statcast).

Splits (vs hand, month) are the same endpoint with `hand=`/`month=` params (added later).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
from sqlalchemy import text

from app.database import async_session

log = logging.getLogger("earl.mlb_fangraphs")

FG_URL = "https://www.fangraphs.com/api/leaders/major-league/data"
UA = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
}
_TAG = re.compile(r"<[^>]+>")
_PID = re.compile(r"playerid=(\d+)")

# (column, fg_key, kind)  kind: i=int f=float s=text
BAT_COLS = [
    ("age", "Age", "f"), ("g", "G", "i"), ("pa", "PA", "i"), ("ab", "AB", "i"), ("h", "H", "i"),
    ("b1", "1B", "i"), ("b2", "2B", "i"), ("b3", "3B", "i"), ("hr", "HR", "i"), ("r", "R", "i"),
    ("rbi", "RBI", "i"), ("bb", "BB", "i"), ("ibb", "IBB", "i"), ("so", "SO", "i"), ("hbp", "HBP", "i"),
    ("sf", "SF", "i"), ("sh", "SH", "i"), ("gdp", "GDP", "i"), ("sb", "SB", "i"), ("cs", "CS", "i"),
    ("avg", "AVG", "f"), ("obp", "OBP", "f"), ("slg", "SLG", "f"), ("ops", "OPS", "f"),
    ("iso", "ISO", "f"), ("babip", "BABIP", "f"),
    ("bb_pct", "BB%", "f"), ("k_pct", "K%", "f"), ("bb_k", "BB/K", "f"),
    ("woba", "wOBA", "f"), ("wraa", "wRAA", "f"), ("wrc", "wRC", "f"), ("wrc_plus", "wRC+", "f"),
    ("war", "WAR", "f"), ("offense", "Offense", "f"), ("defense", "Defense", "f"),
    ("spd", "Spd", "f"), ("ubr", "UBR", "f"), ("wpa", "WPA", "f"), ("re24", "RE24", "f"),
    ("clutch", "Clutch", "f"),
    ("xwoba", "xwOBA", "f"), ("xavg", "xAVG", "f"), ("xslg", "xSLG", "f"),
    ("barrel_pct", "Barrel%", "f"), ("ev", "EV", "f"), ("la", "LA", "f"),
    ("hardhit_pct", "HardHit%", "f"), ("maxev", "maxEV", "f"),
    ("k_pct_plus", "K%+", "f"), ("bb_pct_plus", "BB%+", "f"), ("obp_plus", "OBP+", "f"),
    ("slg_plus", "SLG+", "f"), ("iso_plus", "ISO+", "f"),
    ("o_swing_pct", "O-Swing%", "f"), ("z_swing_pct", "Z-Swing%", "f"), ("swing_pct", "Swing%", "f"),
    ("o_contact_pct", "O-Contact%", "f"), ("z_contact_pct", "Z-Contact%", "f"),
    ("contact_pct", "Contact%", "f"), ("zone_pct", "Zone%", "f"), ("swstr_pct", "SwStr%", "f"),
    ("f_strike_pct", "F-Strike%", "f"),
    ("pull_pct", "Pull%", "f"), ("cent_pct", "Cent%", "f"), ("oppo_pct", "Oppo%", "f"),
    ("soft_pct", "Soft%", "f"), ("med_pct", "Med%", "f"), ("hard_pct", "Hard%", "f"),
]
PIT_COLS = [
    ("age", "Age", "f"), ("w", "W", "i"), ("l", "L", "i"), ("g", "G", "i"), ("gs", "GS", "i"),
    ("cg", "CG", "i"), ("sho", "ShO", "i"), ("sv", "SV", "i"), ("ip", "IP", "f"), ("h", "H", "i"),
    ("er", "ER", "i"), ("hr", "HR", "i"), ("bb", "BB", "i"), ("so", "SO", "i"), ("hbp", "HBP", "i"),
    ("wp", "WP", "i"), ("bf", "TBF", "i"),
    ("era", "ERA", "f"), ("fip", "FIP", "f"), ("xfip", "xFIP", "f"), ("siera", "SIERA", "f"),
    ("xera", "xERA", "f"), ("war", "WAR", "f"), ("whip", "WHIP", "f"), ("k9", "K/9", "f"),
    ("bb9", "BB/9", "f"), ("hr9", "HR/9", "f"), ("k_bb", "K/BB", "f"),
    ("k_pct", "K%", "f"), ("bb_pct", "BB%", "f"), ("k_bb_pct", "K-BB%", "f"),
    ("era_minus", "ERA-", "f"), ("fip_minus", "FIP-", "f"), ("xfip_minus", "xFIP-", "f"),
    ("k_pct_plus", "K%+", "f"), ("bb_pct_plus", "BB%+", "f"),
    ("fbv", "FBv", "f"), ("fbv_plus", "FBv+", "f"),
    ("csw_pct", "CSW%", "f"), ("swstr_pct", "SwStr%", "f"), ("contact_pct", "Contact%", "f"),
    ("zone_pct", "Zone%", "f"), ("o_swing_pct", "O-Swing%", "f"), ("z_swing_pct", "Z-Swing%", "f"),
    ("o_contact_pct", "O-Contact%", "f"), ("z_contact_pct", "Z-Contact%", "f"),
    ("ev", "EV", "f"), ("la", "LA", "f"), ("barrel_pct", "Barrel%", "f"),
    ("hardhit_pct", "HardHit%", "f"), ("maxev", "maxEV", "f"),
]
FLD_COLS = [
    ("age", "Age", "f"), ("pos", "Pos", "s"), ("g", "G", "i"), ("gs", "GS", "i"), ("inn", "Inn", "f"),
    ("po", "PO", "i"), ("a", "A", "i"), ("e", "E", "i"), ("fe", "FE", "i"),
    ("drs", "DRS", "f"), ("uzr", "UZR", "f"), ("uzr_150", "UZR/150", "f"), ("oaa", "OAA", "f"),
    ("rngr", "RngR", "f"), ("errr", "ErrR", "f"), ("rarm", "RARM", "f"), ("rdpr", "RDPR", "f"),
    ("rzr", "RZR", "f"), ("frv", "FRV", "f"), ("rfr", "rFR", "f"), ("fram", "FRM", "f"),
]
SPECS = {"bat": ("fg_batting", BAT_COLS), "pit": ("fg_pitching", PIT_COLS), "fld": ("fg_fielding", FLD_COLS)}

_TYPES = {"i": "integer", "f": "double precision", "s": "text"}


def _clean(s):
    return _TAG.sub("", s).strip() if isinstance(s, str) else s


def _num(v):
    if v is None or v == "" or v == "-" or v == "--":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def ensure_table(table: str, cols) -> None:
    coldefs = ",\n  ".join(f"{c} {_TYPES[k]}" for c, _, k in cols)
    ddl = f"""CREATE TABLE IF NOT EXISTS mlb.{table} (
  season integer NOT NULL,
  is_team boolean NOT NULL DEFAULT false,
  entity_key text NOT NULL,
  name text, fg_id bigint, mlbam_id bigint, team text,
  hand text,
  {coldefs},
  raw jsonb,
  loaded_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (season, is_team, entity_key)
);"""
    async with async_session() as s:
        await s.execute(text(ddl))
        for c, _, k in cols:
            await s.execute(text(f"ALTER TABLE mlb.{table} ADD COLUMN IF NOT EXISTS {c} {_TYPES[k]}"))
        await s.commit()


async def ensure_all() -> None:
    for table, cols in SPECS.values():
        await ensure_table(table, cols)


async def _fetch(client: httpx.AsyncClient, stats: str, season: int, players: bool) -> list:
    out, page, pageitems = [], 1, 200
    while True:
        params = dict(age="", pos="all", stats=stats, lg="all", qual=0, season=season, season1=season,
                      startdate="", enddate="", month=0, hand="", team=0, pageitems=pageitems,
                      pagenum=page, ind=0, rost=0, players=(1 if players else 0), type=8,
                      sortdir="default", sortstat="WAR")
        r = None
        for attempt in range(5):
            r = await client.get(FG_URL, params=params)
            if r.status_code == 200:
                break
            log.warning("FG %s %s -> %s (attempt %d)", stats, season, r.status_code, attempt + 1)
            await asyncio.sleep(2.0 * (attempt + 1))
        if r is None or r.status_code != 200:
            raise RuntimeError(f"FanGraphs fetch failed for stats={stats} season={season}: status={getattr(r,'status_code',None)}")
        data = (r.json().get("data") or []) if "json" in r.headers.get("content-type", "") else []
        out += data
        if len(data) < pageitems:
            break
        page += 1
    return out


def _mkrow(r: dict, season: int, is_team: bool, cols) -> dict:
    name_raw = r.get("Name") or r.get("PlayerName") or ""
    m = _PID.search(str(name_raw))
    fg_id = r.get("playerid") or (int(m.group(1)) if m else None)
    mlbam = r.get("xMLBAMID")
    team = _clean(r.get("TeamNameAbb") or r.get("Team") or "")
    d = {
        "season": season, "is_team": is_team,
        "entity_key": str(fg_id) if fg_id else (team or "?"),
        "name": _clean(name_raw) or None,
        "fg_id": int(fg_id) if fg_id else None,
        "mlbam_id": int(mlbam) if mlbam else None,
        "team": (team or None),
        "hand": r.get("Bats") or r.get("Throws"),
    }
    for c, k, kind in cols:
        v = r.get(k)
        if kind == "s":
            d[c] = _clean(v)
        else:
            n = _num(v)
            d[c] = int(n) if (kind == "i" and n is not None) else n
    d["raw"] = json.dumps(r, default=str)
    return d


async def load_season(season: int, stats=("bat", "pit", "fld")) -> dict:
    counts = {}
    async with httpx.AsyncClient(headers=UA, timeout=90.0) as client:
        for st in stats:
            table, cols = SPECS[st]
            rows_p = await _fetch(client, st, season, True)
            rows_t = await _fetch(client, st, season, False)
            parsed = [_mkrow(r, season, False, cols) for r in rows_p]
            parsed += [_mkrow(r, season, True, cols) for r in rows_t]
            ins_cols = ["season", "is_team", "entity_key", "name", "fg_id", "mlbam_id", "team", "hand"] \
                + [c for c, _, _ in cols] + ["raw"]
            stmt = text(f'INSERT INTO mlb.{table} ({",".join(ins_cols)}) '
                        f'VALUES ({",".join(":" + c for c in ins_cols)}) ON CONFLICT DO NOTHING')
            async with async_session() as s:
                await s.execute(text(f"DELETE FROM mlb.{table} WHERE season = :y"), {"y": season})
                if parsed:
                    await s.execute(stmt, parsed)
                await s.commit()
            counts[table] = len(parsed)
            log.info("FG %s %s: %d rows", table, season, len(parsed))
    return counts


if __name__ == "__main__":
    import sys
    asyncio.run(load_season(int(sys.argv[1]) if len(sys.argv) > 1 else 2024))
