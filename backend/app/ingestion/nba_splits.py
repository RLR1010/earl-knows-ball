"""NBA player splits + career stats ingestion.

Computes nba.player_splits from nba.player_game_stats x nba.games
(no new external data source). Mirrors the MLB/NFL player_splits design:

  - season_id NULL  -> CAREER row (aggregated across ALL seasons)
  - season_id set   -> that season's split

Split types (SPLIT_TYPES keys):
  home                        home games
  away                        away games
  vs_east | vs_west           vs opponent conference (respecting intra-division)
  starter | bench             started or came off bench
  rest0 | rest_ge1            back-to-back (0 days rest) vs >=1 day rest
  month_<oct..apr>            calendar month (per-season only; not emitted as career)

Rates below are per-GAME (totals/G), except FG%/3P%/FT%/TS% which are
shooting percentages computed from makes/attempts, and plus-minus which
is averaged per game. Full-replace rebuild (like NFL) for the given seasons.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.nba_splits")

# ---------------------------------------------------------------------------
# Split type definitions
# ---------------------------------------------------------------------------
SPLIT_TYPES = {
    "home": "Home",
    "away": "Away",
    "vs_east": "vs Eastern Conf",
    "vs_west": "vs Western Conf",
    "starter": "Starter",
    "bench": "Bench",
    "rest0": "Back-to-Back (0 rest)",
    "rest_ge1": "1+ Days Rest",
    # months (per-season only)
    "month_oct": "October", "month_nov": "November", "month_dec": "December",
    "month_jan": "January", "month_feb": "February", "month_mar": "March",
    "month_apr": "April", "month_may": "May", "month_jun": "June",
    "month_jul": "July",
}

# Rest split handling: how to bucket
_REST_KEYS = {0: "rest0"}  # exact 0 -> back-to-back


def _is_month_split(sp: str) -> bool:
    return sp.startswith("month_")


def _game_split_types(
    g: dict, home_team_id: Optional[int], away_team_id: Optional[int],
    team_id: Optional[int], opp_conf: Optional[str],
) -> List[str]:
    """Return the list of split-type keys a single game row belongs to.

    NBA is fully indoor, so no temperature/surface/dome buckets (unlike MLB).
    """
    splits = []
    if team_id is None:
        return splits

    # home / away
    if home_team_id is not None and team_id == home_team_id:
        splits.append("home")
    elif away_team_id is not None and team_id == away_team_id:
        splits.append("away")

    # vs conference
    if opp_conf:
        splits.append(f"vs_{opp_conf.lower()}")

    # starter / bench
    if g.get("is_starter"):
        splits.append("starter")
    else:
        splits.append("bench")

    # rest bucket
    rest = g.get("rest_days")  # integer days since previous game (None if unknown)
    if rest is not None:
        splits.append(_REST_KEYS.get(rest, "rest_ge1"))

    # calendar month (per-season only; career rows filtered out later)
    date = g.get("date")
    if date is not None:
        mkey = "month_" + date.strftime("%b").lower() if hasattr(date, "strftime") else ""
        if mkey:
            splits.append(mkey)

    return splits


def _empty_agg() -> dict:
    return {
        "games": 0,
        "games_started": 0,
        "minutes_played": 0,          # minutes as float (parse "29:55")
        "points": 0,
        "fgm": 0, "fga": 0,
        "threem": 0, "threea": 0,
        "ftm": 0, "fta": 0,
        "oreb": 0, "dreb": 0, "treb": 0,
        "ast": 0, "stl": 0, "blk": 0, "tov": 0, "pf": 0,
        "plus_minus_sum": 0.0,
        "plus_minus_n": 0,
    }


def _parse_minutes(m) -> Optional[float]:
    """'29:55' -> 29.92 minutes. Returns None if missing/unparseable."""
    if not m:
        return None
    try:
        mm, ss = str(m).split(":")
        return round(float(mm) + float(ss) / 60.0, 2)
    except (ValueError, AttributeError):
        try:
            return float(m)
        except (ValueError, TypeError):
            return None


def _merge(agg: dict, row: dict) -> None:
    agg["games"] += 1
    if row.get("is_starter"):
        agg["games_started"] += 1
    mins = _parse_minutes(row.get("minutes"))
    if mins is not None:
        agg["minutes_played"] += mins
    for col in ("points", "fgm", "fga", "threem", "threea",
                "ftm", "fta", "oreb", "dreb", "treb",
                "ast", "stl", "blk", "tov", "pf"):
        v = row.get(col)
        if v:
            agg[col] += int(v)
    # plus_minus: average (ignores NULLs)
    pm = row.get("plus_minus")
    if pm is not None:
        agg["plus_minus_sum"] += float(pm)
        agg["plus_minus_n"] += 1


def _finalize(sp: str, agg: dict) -> dict:
    """Compute per-game rates + shooting pct from aggregates."""
    out = dict(agg)
    g = max(out["games"], 1)
    out["minutes_per_game"] = round(out["minutes_played"] / g, 2)
    out["points_per_game"] = round(out["points"] / g, 2)
    out["rebounds_per_game"] = round(out["treb"] / g, 2)
    out["offensive_rebounds_per_game"] = round(out["oreb"] / g, 2)
    out["defensive_rebounds_per_game"] = round(out["dreb"] / g, 2)
    out["assists_per_game"] = round(out["ast"] / g, 2)
    out["steals_per_game"] = round(out["stl"] / g, 2)
    out["blocks_per_game"] = round(out["blk"] / g, 2)
    out["turnovers_per_game"] = round(out["tov"] / g, 2)
    out["fouls_per_game"] = round(out["pf"] / g, 2)
    out["plus_minus_per_game"] = (
        round(out["plus_minus_sum"] / out["plus_minus_n"], 2)
        if out["plus_minus_n"] else None
    )

    # shooting pct (ratio of makes to attempts)
    out["field_goals_pct"] = round(out["fgm"] / out["fga"], 3) if out["fga"] else None
    out["three_point_pct"] = round(out["threem"] / out["threea"], 3) if out["threea"] else None
    out["free_throw_pct"] = round(out["ftm"] / out["fta"], 3) if out["fta"] else None
    # true shooting: pts / (2 * (FGA + 0.44*FTA))
    denom = 2 * (out["fga"] + 0.44 * out["fta"])
    out["true_shooting_pct"] = round(out["points"] / denom, 3) if denom else None
    return out


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
_SPLIT_GAME_SQL = """
SELECT
    pgs.player_id, pgs.team_id, pgs.game_id, pgs.is_starter,
    pgs.minutes, pgs.points,
    pgs.field_goals_made AS fgm, pgs.field_goals_attempted AS fga,
    pgs.three_pointers_made AS threem, pgs.three_pointers_attempted AS threea,
    pgs.free_throws_made AS ftm, pgs.free_throws_attempted AS fta,
    pgs.rebounds_offensive AS oreb, pgs.rebounds_defensive AS dreb,
    pgs.rebounds_total AS treb,
    pgs.assists AS ast, pgs.steals AS stl, pgs.blocks AS blk,
    pgs.turnovers AS tov, pgs.fouls_personal AS pf, pgs.plus_minus,
    g.home_team_id, g.away_team_id, g.date,
    g.season_id AS season_id,
    t.conference AS opp_conference
FROM nba.player_game_stats pgs
JOIN nba.games g ON g.id = pgs.game_id
LEFT JOIN nba.teams t ON t.id = (CASE WHEN pgs.team_id = g.home_team_id
                                       THEN g.away_team_id ELSE g.home_team_id END)
WHERE g.season_id IN ({sq})
"""


def _game_rest_map(db_rows: List[dict]) -> Dict[Tuple[Optional[int], int], int]:
    """Map (team_id, game_id) -> days rest (days since that team's previous game).

    NBA season typically has 1-2 days between games; back-to-backs are rest=0.
    Computed from the games' dates grouped by team (chronological).
    """
    # collect each team's (date, game_id); dedupe by game_id because
    # player_game_stats has one row per player-line per game (12+ dupes/game).
    by_team: Dict[int, List[Tuple]] = defaultdict(list)
    seen: set = set()
    for r in db_rows:
        team_id = r["team_id"]
        game_id = r["game_id"]
        date = r["date"]
        if team_id is None or game_id is None or date is None:
            continue
        if (team_id, game_id) in seen:
            continue
        seen.add((team_id, game_id))
        by_team[team_id].append((date, game_id))
    out: Dict[Tuple[Optional[int], int], int] = {}
    for team_id, entries in by_team.items():
        entries.sort(key=lambda x: x[0])
        for i, (date, game_id) in enumerate(entries):
            if i == 0:
                out[(team_id, game_id)] = None  # first game, unknown rest
            else:
                prev_date = entries[i - 1][0]
                gap = (date - prev_date).days
                out[(team_id, game_id)] = max(gap - 1, 0)  # rest days between games
    return out


async def build_player_splits(
    db: AsyncSession, season_ids: Optional[Sequence[int]] = None
) -> dict:
    """Compute nba.player_splits from game logs. Full-replace for given seasons
    (default: all), plus a career aggregate (season_id=NULL) across all seasons.

    Returns {'players': N, 'rows_written': M, 'season_ids': [...]}."""
    # Determine seasons (from game logs so we only span loaded seasons)
    if not season_ids:
        r = await db.execute(text("SELECT DISTINCT g.season_id FROM nba.games g WHERE g.season_id IS NOT NULL"))
        season_ids = [x[0] for x in r.fetchall()]
    season_ids = sorted(s for s in season_ids if s is not None)
    logger.info("nba splits: processing season_ids %s", season_ids)
    if not season_ids:
        logger.warning("nba splits: no seasons found; nothing to do.")
        return {"players": 0, "rows_written": 0, "season_ids": []}

    sq = ",".join([":s%d" % i for i in range(len(season_ids))]) or "NULL"
    params = {f"s{i}": s for i, s in enumerate(season_ids)}
    rows = (await db.execute(text(_SPLIT_GAME_SQL.format(sq=sq)), params)).mappings().all()
    logger.info("nba splits: loaded %d game-line rows", len(rows))

    rest_map = _game_rest_map([dict(r) for r in rows])

    # agg[(season_key)][split_type][player_id] -> agg dict
    #   season_key = None (career) or int (specific season)
    agg: Dict[Tuple, Dict[str, Dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))

    for r in rows:
        row = dict(r)
        pid = row["player_id"]
        team_id = row["team_id"]
        home = row["home_team_id"]
        away = row["away_team_id"]
        game_id = row["game_id"]
        rest = rest_map.get((team_id, game_id), None)
        row["rest_days"] = rest
        row["is_starter"] = bool(row.get("is_starter"))
        split_types = _game_split_types(row, home, away, team_id, row.get("opp_conference"))
        season_key = row["season_id"]
        for sp in split_types:
            for key in ((None,), (season_key,)):
                d = agg[key][sp].get(pid)
                if d is None:
                    d = _empty_agg()
                    agg[key][sp][pid] = d
                _merge(d, row)

    # Full-replace rebuild (delete all, re-insert)
    await db.execute(text("DELETE FROM nba.player_splits"))
    to_insert = []
    pg = 0
    for key, by_split in agg.items():
        season_key = key[0]  # None or int
        for sp, players in by_split.items():
            # Skip calendar-month career rows (months are season-context only)
            if season_key is None and _is_month_split(sp):
                continue
            # Per-season should not include month X as career; career keeps non-month only
            label = SPLIT_TYPES.get(sp, sp)
            for pid, a in players.items():
                fin = _finalize(sp, a)
                to_insert.append({
                    "player_id": pid, "season_id": season_key, "team_id": None,
                    "split_type": sp, "split_label": label,
                    "games": fin["games"], "games_started": fin["games_started"],
                    "minutes_per_game": fin["minutes_per_game"],
                    "points_per_game": fin["points_per_game"],
                    "field_goals_pct": fin["field_goals_pct"],
                    "three_point_pct": fin["three_point_pct"],
                    "free_throw_pct": fin["free_throw_pct"],
                    "rebounds_per_game": fin["rebounds_per_game"],
                    "offensive_rebounds_per_game": fin["offensive_rebounds_per_game"],
                    "defensive_rebounds_per_game": fin["defensive_rebounds_per_game"],
                    "assists_per_game": fin["assists_per_game"],
                    "steals_per_game": fin["steals_per_game"],
                    "blocks_per_game": fin["blocks_per_game"],
                    "turnovers_per_game": fin["turnovers_per_game"],
                    "fouls_per_game": fin["fouls_per_game"],
                    "plus_minus_per_game": fin["plus_minus_per_game"],
                    "true_shooting_pct": fin["true_shooting_pct"],
                    "usage_pct": None,
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
    await db.execute(text(
        f"INSERT INTO nba.player_splits ({col_sql}) VALUES ({ph})"
    ), rows)


async def run_once(db: AsyncSession) -> dict:
    return await build_player_splits(db)
