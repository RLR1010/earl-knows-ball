"""NFL per-player situational/career splits (nfl.player_splits).

Computes high-ROI splits for Earl chat research + props from data we ALREADY
have — `nfl.player_weekly_stats` (game-level lines) joined to `nfl.games`
(game context: temperature, roof_type, surface, home/away, date, week).

Split types produced (season_id NULL = career aggregate; source rows grouped by
season_id also written):
  home, away                -> game location (team_id vs home/away)
  cold, mild, warm          -> temperature buckets (cold <40F, mild 40-69, warm >=70)
  outdoor_cold              -> temp <40F AND roof outdoor (the "bad weather game")
  dome, outdoor             -> roof_type (dome/retractable-in = dome; else outdoor)
  grass, turf               -> surface_type (Grass vs Artificial)
  division, non_division    -> vs division rivals (via TEAM_DIVISIONS map)
  primetime, day            -> game is Sun Night/Mon Night/Thu Night/Fri vs day
NOTE: 'precipitation/rain' requires real game-time precip (weather_condition is
only 'Historical'/'None' and weather_forecasts is empty) -> NOT computed until a
weather source is added. outdoor_cold is the reliable proxy.

Positions handled: QB (pass/rush), RB (rush/recv), WR/TE (recv), DEF (def cols).

Runner: app/scripts/ingress/run_nfl_splits_refresh.py (subprocess).
Idempotent full-replace: deletes then re-inserts this build's rows.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.nfl_splits")

# ---- Split type definitions ------------------------------------------------
# name -> (label). 'career' always computed per season + as NULL=career.

SPLIT_TYPES: Dict[str, str] = {
    "home": "Home",
    "away": "Away",
    "cold": "Cold (<40F)",
    "mild": "Mild (40-69F)",
    "warm": "Warm (>=70F)",
    "outdoor_cold": "Cold + Outdoor",
    "dome": "Dome / Retractable-in",
    "outdoor": "Outdoor",
    "grass": "Grass",
    "turf": "Artificial / Turf",
    "division": "Division",
    "non_division": "Non-division",
    "primetime": "Primetime",
    "day": "Day game",
}

# Team id -> division (NFC East/West/North/South, AFC East/West/North/South).
# Verified against nfl.teams in refresh (warn on any id missing from this map).
TEAM_DIVISIONS: Dict[int, str] = {
    1: "NFC North", 2: "NFC East", 3: "AFC East", 4: "AFC West", 5: "NFC South",
    6: "AFC North", 7: "AFC South", 8: "NFC West", 9: "AFC South", 10: "AFC North",
    11: "NFC North", 12: "AFC West", 13: "NFC West", 14: "NFC East", 15: "AFC North",
    16: "NFC North", 17: "AFC East", 18: "AFC South", 19: "AFC East", 20: "NFC West",
    21: "AFC East", 22: "AFC North", 23: "NFC South", 24: "NFC East", 25: "AFC West",
    26: "AFC West", 27: "NFC South", 28: "AFC South", 29: "AFC South", 30: "NFC North",
    31: "NFC South", 32: "AFC West", 33: "NFC North", 34: "AFC South",
    35: "NFC South", 36: "AFC East", 37: "NFC East", 38: "AFC West",
}


def _temp_bucket(temp: Optional[float]) -> List[str]:
    """Return split types implied by temperature (cold/mild/warm + outdoor_cold)."""
    if temp is None:
        return []
    out = []
    if temp < 40:
        out.append("cold")
    elif temp < 70:
        out.append("mild")
    else:
        out.append("warm")
    return out


def _roof_bucket(roof: Optional[str]) -> List[str]:
    """roof_type -> dome/outdoor split types."""
    r = (roof or "").strip().lower()
    if r in ("dome", "retractable"):
        return ["dome"]
    if r == "outdoor":
        return ["outdoor"]
    return []


def _surface_bucket(surface: Optional[str]) -> List[str]:
    s = (surface or "").strip().lower()
    if s.startswith("grass"):
        return ["grass"]
    if s in ("artificial", "turf", "fieldturf", "astroturf", "sportsturf"):
        return ["turf"]
    return []


def _is_primetime(weekday: Optional[int], hour: Optional[int]) -> bool:
    """weekday 0=Mon..6=Sun. Primetime = Sun>=20h, Mon, Thu, Fri, Sat (>=18h)."""
    if weekday is None:
        return False
    if weekday == 6:  # Sunday
        return hour is not None and hour >= 20
    if weekday == 0:  # Monday
        return hour is not None and hour >= 18
    if weekday in (3, 4):  # Thursday / Friday
        return True
    if weekday == 5:  # Saturday
        return hour is not None and hour >= 18
    return False


def _game_split_types(g: dict, home_team_id, away_team_id, team_id, div: str) -> List[str]:
    """Map one game row + player's team to the split types it contributes to."""
    splits = []
    # home / away
    splits.append("home" if team_id == home_team_id else "away")
    # temperature
    t = g.get("temperature")
    tb = _temp_bucket(t)
    splits.extend(tb)
    if t is not None and t < 40 and _roof_bucket(g.get("roof_type")) == ["outdoor"]:
        splits.append("outdoor_cold")
    # roof
    splits.extend(_roof_bucket(g.get("roof_type")))
    # surface
    splits.extend(_surface_bucket(g.get("surface")))
    # division
    if div:
        # division if opponent shares our division
        opp = away_team_id if team_id == home_team_id else home_team_id
        opp_div = TEAM_DIVISIONS.get(opp)
        splits.append("division" if (div and div == opp_div) else "non_division")
    # primetime/day
    splits.append("primetime" if _is_primetime(g.get("weekday"), g.get("hour")) else "day")
    return splits


def _empty_agg():
    return {
        "games": 0,
        "pass_attempts": 0, "pass_completions": 0, "pass_yards": 0, "pass_tds": 0,
        "pass_int": 0, "pass_rating": None, "pass_rating_weighted": 0.0,
        "rush_attempts": 0, "rush_yards": 0, "rush_tds": 0,
        "targets": 0, "receptions": 0, "receiving_yards": 0, "receiving_tds": 0,
        "fumbles": 0,
        "fantasy_std": 0.0, "fantasy_half": 0.0, "fantasy_ppr": 0.0,
        "def_points_allowed": 0, "def_tackles": 0, "def_sacks": 0, "def_takeaways": 0,
        "def_fantasy_pts": 0.0,
    }


def _merge(agg: dict, row) -> None:
    agg["games"] += 1
    for col in ("pass_attempts", "pass_completions", "pass_yards", "pass_tds", "pass_int",
                "rush_attempts", "rush_yards", "rush_tds",
                "targets", "receptions", "receiving_yards", "receiving_tds", "fumbles"):
        v = row.get(col)
        if v:
            agg[col] += int(v)
    # defense: row aliases points_allowed/sacks -> agg cols def_points_allowed/def_sacks
    for rcol, acol in (("points_allowed", "def_points_allowed"), ("sacks", "def_sacks")):
        v = row.get(rcol)
        if v:
            agg[acol] += int(v)
    # fantasy
    for fcol in ("fantasy_points_std", "fantasy_points_half", "fantasy_points_ppr", "def_fantasy_pts"):
        v = row.get(fcol)
        if v is not None:
            if fcol == "def_fantasy_pts":
                agg[fcol] += float(v)
            else:
                agg[fcol.replace("fantasy_points_", "fantasy_")] += float(v)
    # def_tackles: weekly stats store 'tackles'? use interceptions+fumbles_recovered as takeaways approx
    tkl = float(row.get("tackles") or 0)
    agg["def_tackles"] += int(tkl)
    ints = int(row.get("interceptions") or 0)
    f_rec = int(row.get("fumbles_recovered") or 0)
    agg["def_takeaways"] += ints + f_rec
    # passer rating weighted (for avg)
    pr = row.get("passer_rating")
    if pr:
        agg["pass_rating_weighted"] += float(pr)
        # track by pass_attempts presence below via games? use count of non-null
        agg["_rating_n"] += 1


def _finalize(name: str, agg: dict) -> dict:
    """Compute rate fields + avg passer rating, drop internal keys."""
    out = dict(agg)
    out.pop("_rating_n", None)
    out.pop("pass_rating_weighted", None)
    g = out.get("games", 0) or 1
    out.setdefault("passer_rating", None)
    out["ypc"] = round(out["rush_yards"] / out["rush_attempts"], 2) if out["rush_attempts"] else None
    out["ypr"] = round(out["receiving_yards"] / out["receptions"], 2) if out["receptions"] else None
    if agg.get("_rating_n"):
        out["passer_rating"] = round(agg["pass_rating_weighted"] / agg["_rating_n"], 1)
    return out


async def build_player_splits(db: AsyncSession, season_ids: Optional[Sequence[int]] = None) -> dict:
    """Compute nfl.player_splits for ALL players across given seasons (default all),
    then full-replace the table's rows for those seasons.
    Returns {'players': N, 'rows_written': M, 'season_ids': [...]}."""
    # Determine seasons to process
    if not season_ids:
        r = await db.execute(text("SELECT DISTINCT season_id FROM nfl.player_weekly_stats"))
        season_ids = [x[0] for x in r.fetchall()]
    season_ids = sorted(s for s in season_ids if s is not None)
    logger.info("nfl splits: processing season_ids %s", season_ids)

    # Pull joined game+line rows (game context + player line) for these seasons.
    sq = ",".join([":s%d" % i for i in range(len(season_ids))]) or "NULL"
    params = {f"s{i}": s for i, s in enumerate(season_ids)}
    rows = (await db.execute(text(f"""
        SELECT pws.player_id, pws.season_id, pws.week, pws.team_id, pws.opponent_id,
               pws.pass_attempts, pws.pass_completions, pws.pass_yards, pws.pass_tds,
               pws.pass_int, pws.passer_rating,
               pws.rush_attempts, pws.rush_yards, pws.rush_tds,
               pws.targets, pws.receptions, pws.receiving_yards, pws.receiving_tds,
               pws.fumbles, pws.interceptions, pws.fumbles_recovered, pws.sacks,
               pws.fantasy_points_std, pws.fantasy_points_half, pws.fantasy_points_ppr,
               pws.points_allowed, pws.snaps_defense,
               g.home_team_id, g.away_team_id, g.date, g.temperature, g.roof_type,
               g.surface, g.season_id AS g_season_id
        FROM nfl.player_weekly_stats pws
        JOIN nfl.games g ON g.id = pws.game_id
        WHERE pws.season_id IN ({sq})
    """), params)).mappings().all()

    logger.info("nfl splits: loaded %d game-line rows", len(rows))

    # agg[(season_key)][split_type][player_id] -> agg dict
    #   season_key = None (career) or int (specific season)
    agg = defaultdict(lambda: defaultdict(dict))

    for row in rows:
        pid = row["player_id"]
        team_id = row["team_id"]
        home = row["home_team_id"]
        away = row["away_team_id"]
        div = TEAM_DIVISIONS.get(team_id)
        g = dict(row)
        g["weekday"] = row["date"].weekday() if row["date"] is not None else None
        g["hour"] = row["date"].hour if row["date"] is not None else None
        split_types = _game_split_types(g, home, away, team_id, div)
        season_key = row["season_id"]
        for sp in split_types:
            for key in ((None,), (season_key,)):
                d = agg[key][sp].get(pid)
                if d is None:
                    d = _empty_agg()
                    d["_rating_n"] = 0
                    agg[key][sp][pid] = d
                _merge(d, row)

    # Build final rows (delete+insert full-replace)
    await db.execute(text("DELETE FROM nfl.player_splits"))
    to_insert = []
    pg = 0
    for key, by_split in agg.items():
        season_key = key[0]  # None or int
        for sp, players in by_split.items():
            label = SPLIT_TYPES.get(sp, sp)
            for pid, a in players.items():
                fin = _finalize(sp, a)
                to_insert.append({
                    "player_id": pid, "season_id": season_key, "split_type": sp,
                    "split_label": label, "games_played": fin["games"],
                    "pass_attempts": fin["pass_attempts"], "pass_completions": fin["pass_completions"],
                    "pass_yards": fin["pass_yards"], "pass_tds": fin["pass_tds"], "pass_int": fin["pass_int"],
                    "passer_rating": fin["passer_rating"],
                    "rush_attempts": fin["rush_attempts"], "rush_yards": fin["rush_yards"], "rush_tds": fin["rush_tds"],
                    "targets": fin["targets"], "receptions": fin["receptions"],
                    "receiving_yards": fin["receiving_yards"], "receiving_tds": fin["receiving_tds"],
                    "fumbles": fin["fumbles"],
                    "fantasy_std": fin["fantasy_std"], "fantasy_half": fin["fantasy_half"], "fantasy_ppr": fin["fantasy_ppr"],
                    "def_points_allowed": fin["def_points_allowed"], "def_tackles": fin["def_tackles"],
                    "def_sacks": fin["def_sacks"], "def_takeaways": fin["def_takeaways"],
                    "def_fantasy_pts": fin["def_fantasy_pts"],
                    "ypc": fin["ypc"], "ypr": fin["ypr"],
                })
                pg += 1
                if len(to_insert) >= 2000:
                    await _bulk_insert(db, to_insert)
                    to_insert = []
    if to_insert:
        await _bulk_insert(db, to_insert)

    return {"players": len({r["player_id"] for r in rows}), "rows_written": pg,
            "season_ids": season_ids}


async def _bulk_insert(db: AsyncSession, rows: Sequence[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    col_sql = ", ".join(cols)
    ph = ", ".join([":" + c for c in cols])
    await db.execute(text(f"""
        INSERT INTO nfl.player_splits ({col_sql})
        VALUES ({ph})
    """), rows)


async def run_once(db: AsyncSession) -> dict:
    return await build_player_splits(db)
