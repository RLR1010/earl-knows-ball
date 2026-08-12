"""NBA team splits (home/away, vs East/West team form) + ATS/O/U records.

Derived entirely from existing tables (no new external data):

  - nba.games           -> scores, team box stats, venue, opponents
  - nba.teams           -> opponent conference (East/West)
  - nba.betting_lines   -> consensus closing spread / total for ATS & O/U

Produces nba.team_splits rows:

  split_type: home | away | vs_east | vs_west
  season_id NULL -> CAREER row (all seasons); set -> that season's split

Rates are per-GAME totals/games. ATS/O/U computed against the consensus
(non-opening) line averaged across sportsbooks per game. Full-replace build.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("earl.nba_team_splits")

SPLIT_TYPES = {
    "home": "Home",
    "away": "Away",
    "vs_east": "vs Eastern Conf",
    "vs_west": "vs Western Conf",
}


def _empty_agg() -> dict:
    return {
        "games": 0,
        "wins": 0,
        "losses": 0,
        "pf": 0, "pa": 0,           # points for / against
        "fgm": 0, "fga": 0, "threem": 0, "threea": 0,
        "ftm": 0, "fta": 0,
        "reb": 0, "ast": 0, "stl": 0, "blk": 0, "tov": 0, "fouls": 0,
        # ATS / OU
        "ats_w": 0, "ats_l": 0, "ats_p": 0,
        "ou_o": 0, "ou_u": 0, "ou_p": 0,
        # pace (total possessions proxy): (FGA + 0.44*FTA + TO - ORB) per team
        "pace_sum": 0.0,
    }


def _possession(h_fga, h_fta, h_tov, h_oreb, a_oreb) -> float:
    """Team possessions estimate using the standard NBA possession formula."""
    return h_fga + 0.44 * h_fta + h_tov - h_oreb + 0.5 * (a_oreb)


def _finalize(sp: str, agg: dict) -> dict:
    out = dict(agg)
    g = max(out["games"], 1)
    out["win_pct"] = round(out["wins"] / g, 3) if g else None
    out["points_for"] = round(out["pf"] / g, 1)
    out["points_against"] = round(out["pa"] / g, 1)
    out["point_differential"] = round((out["pf"] - out["pa"]) / g, 1)
    out["pace"] = round(out["pace_sum"] / g, 1) if out["pace_sum"] else None
    out["field_goal_pct"] = round(out["fgm"] / out["fga"], 3) if out["fga"] else None
    out["three_point_pct"] = round(out["threem"] / out["threea"], 3) if out["threea"] else None
    out["free_throw_pct"] = round(out["ftm"] / out["fta"], 3) if out["fta"] else None
    out["rebounds_per_game"] = round(out["reb"] / g, 2)
    out["assists_per_game"] = round(out["ast"] / g, 2)
    out["steals_per_game"] = round(out["stl"] / g, 2)
    out["blocks_per_game"] = round(out["blk"] / g, 2)
    out["turnovers_per_game"] = round(out["tov"] / g, 2)
    out["fouls_per_game"] = round(out["fouls"] / g, 2)
    total_ats = out["ats_w"] + out["ats_l"] + out["ats_p"]
    out["ats_pct"] = round(out["ats_w"] / total_ats, 3) if total_ats else None
    total_ou = out["ou_o"] + out["ou_u"] + out["ou_p"]
    out["ou_overs_pct"] = round(out["ou_o"] / total_ou, 3) if total_ou else None
    return out


def _team_split_types(is_home: bool, opp_conf: Optional[str]) -> List[str]:
    splits = ["home"] if is_home else ["away"]
    if opp_conf:
        splits.append(f"vs_{opp_conf.lower()}")
    return splits


# Build one row per (team, game) from the joined games query.
_GAME_SQL = f"""
WITH closing AS (
    SELECT bl.game_id,
           AVG(bl.spread)      AS spread,
           AVG(bl.over_under)  AS total
    FROM nba.betting_lines bl
    WHERE bl.is_opening = 'f'
      AND bl.spread IS NOT NULL
    GROUP BY bl.game_id
)
SELECT
    g.id, g.season_id, g.home_team_id, g.away_team_id,
    g.home_score, g.away_score,
    g.home_field_goals_made, g.home_field_goals_attempted,
    g.home_three_points_made, g.home_three_points_attempted,
    g.home_free_throws_made, g.home_free_throws_attempted,
    g.home_rebounds, g.home_assists, g.home_steals, g.home_blocks,
    g.home_turnovers, g.home_fouls,
    g.away_field_goals_made, g.away_field_goals_attempted,
    g.away_three_points_made, g.away_three_points_attempted,
    g.away_free_throws_made, g.away_free_throws_attempted,
    g.away_rebounds, g.away_assists, g.away_steals, g.away_blocks,
    g.away_turnovers, g.away_fouls,
    th.conference AS home_conf, ta.conference AS away_conf,
    c.spread, c.total
FROM nba.games g
JOIN nba.teams th ON th.id = g.home_team_id AND NULLIF(th.conference, '') IS NOT NULL
JOIN nba.teams ta ON ta.id = g.away_team_id AND NULLIF(ta.conference, '') IS NOT NULL
LEFT JOIN closing c ON c.game_id = g.id
WHERE g.status = 'FINAL'
  AND g.home_score IS NOT NULL AND g.away_score IS NOT NULL
  AND g.home_team_id IS NOT NULL AND g.away_team_id IS NOT NULL
"""


async def build_team_splits(
    db: AsyncSession, season_ids: Optional[Sequence[int]] = None
) -> dict:
    """Compute nba.team_splits from finalized games. Full-replace rebuild.

    Returns {'teams': N, 'rows_written': M, 'season_ids': [...]}.
    """
    rows = (await db.execute(text(_GAME_SQL))).mappings().all()
    logger.info("nba team splits: loaded %d finalized games", len(rows))
    if not rows:
        return {"teams": 0, "rows_written": 0, "season_ids": []}

    def season_key(sid):  # None for career
        return sid

    # agg[(season_key)][split_type][team_id] -> agg
    agg: Dict[Tuple, Dict[str, Dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))

    for r in rows:
        g = dict(r)
        home_id, away_id = g["home_team_id"], g["away_team_id"]
        hs, aws = g["home_score"], g["away_score"]
        if home_id is None or away_id is None or hs is None or aws is None:
            continue
        margin = hs - aws  # positive = home wins
        spread = g.get("spread")   # home-team perspective (negative = home fav)
        total = g.get("total")
        combined = hs + aws

        # --- HOME team line ---
        home_splits = _team_split_types(True, g.get("away_conf"))
        home_agg_fields = {
            "pf": hs, "pa": aws,
            "fgm": g.get("home_field_goals_made") or 0,
            "fga": g.get("home_field_goals_attempted") or 0,
            "threem": g.get("home_three_points_made") or 0,
            "threea": g.get("home_three_points_attempted") or 0,
            "ftm": g.get("home_free_throws_made") or 0,
            "fta": g.get("home_free_throws_attempted") or 0,
            "reb": g.get("home_rebounds") or 0,
            "ast": g.get("home_assists") or 0,
            "stl": g.get("home_steals") or 0,
            "blk": g.get("home_blocks") or 0,
            "tov": g.get("home_turnovers") or 0,
            "fouls": g.get("home_fouls") or 0,
        }
        pace_home = _possession(home_agg_fields["fga"], home_agg_fields["fta"],
                                home_agg_fields["tov"], 0, 0)
        _apply(agg, season_key(g["season_id"]), home_splits, home_id, {
            "win": 1 if margin > 0 else 0, "loss": 1 if margin < 0 else 0,
            "ats": _ats_home(margin, spread), "ou": _ou(combined, total),
            "pace": pace_home, **home_agg_fields,
        })

        # --- AWAY team line (flips perspective) ---
        away_splits = _team_split_types(False, g.get("home_conf"))
        away_agg_fields = {
            "pf": aws, "pa": hs,
            "fgm": g.get("away_field_goals_made") or 0,
            "fga": g.get("away_field_goals_attempted") or 0,
            "threem": g.get("away_three_points_made") or 0,
            "threea": g.get("away_three_points_attempted") or 0,
            "ftm": g.get("away_free_throws_made") or 0,
            "fta": g.get("away_free_throws_attempted") or 0,
            "reb": g.get("away_rebounds") or 0,
            "ast": g.get("away_assists") or 0,
            "stl": g.get("away_steals") or 0,
            "blk": g.get("away_blocks") or 0,
            "tov": g.get("away_turnovers") or 0,
            "fouls": g.get("away_fouls") or 0,
        }
        pace_away = _possession(away_agg_fields["fga"], away_agg_fields["fta"],
                                away_agg_fields["tov"], 0, 0)
        _apply(agg, season_key(g["season_id"]), away_splits, away_id, {
            "win": 1 if aws > hs else 0, "loss": 1 if aws < hs else 0,
            # away covers when home margin < spread  (spread is home-perspective)
            "ats": _ats_away(margin, spread), "ou": _ou(combined, total),
            "pace": pace_away, **away_agg_fields,
        })

    # Full-replace rebuild
    all_seasons = sorted({r["season_id"] for r in rows if r["season_id"] is not None})
    await db.execute(text("DELETE FROM nba.team_splits"))
    to_insert = []
    n = 0
    team_set = set()
    for key, by_split in agg.items():
        season_key = key[0]
        for sp, teams in by_split.items():
            label = SPLIT_TYPES.get(sp, sp)
            for tid, a in teams.items():
                team_set.add(tid)
                fin = _finalize(sp, a)
                to_insert.append({
                    "team_id": tid, "season_id": season_key,
                    "split_type": sp, "split_label": label,
                    "games": fin["games"], "wins": fin["wins"],
                    "losses": fin["losses"], "win_pct": fin["win_pct"],
                    "points_for": fin["points_for"], "points_against": fin["points_against"],
                    "point_differential": fin["point_differential"], "pace": fin["pace"],
                    "field_goal_pct": fin["field_goal_pct"],
                    "three_point_pct": fin["three_point_pct"],
                    "free_throw_pct": fin["free_throw_pct"],
                    "rebounds_per_game": fin["rebounds_per_game"],
                    "assists_per_game": fin["assists_per_game"],
                    "steals_per_game": fin["steals_per_game"],
                    "blocks_per_game": fin["blocks_per_game"],
                    "turnovers_per_game": fin["turnovers_per_game"],
                    "fouls_per_game": fin["fouls_per_game"],
                    "ats_wins": fin["ats_w"], "ats_losses": fin["ats_l"],
                    "ats_pushes": fin["ats_p"], "ats_pct": fin["ats_pct"],
                    "ou_overs": fin["ou_o"], "ou_unders": fin["ou_u"],
                    "ou_pushes": fin["ou_p"], "ou_overs_pct": fin["ou_overs_pct"],
                })
                n += 1
                if len(to_insert) >= 2000:
                    await _bulk_insert(db, to_insert)
                    to_insert = []
    if to_insert:
        await _bulk_insert(db, to_insert)

    return {"teams": len(team_set), "rows_written": n, "season_ids": all_seasons}


def _ats_home(margin: float, spread: Optional[float]) -> str:
    """Home-team ATS result given final margin (home_score - away_score)
    and the home-perspective closing spread. Returns 'w'|'l'|'p' or 'NA'."""
    if spread is None or margin is None:
        return "NA"
    # Home covers if margin > spread; margin == spread is a push.
    if margin > spread:
        return "w"
    if margin < spread:
        return "l"
    return "p"


def _ats_away(margin: float, spread: Optional[float]) -> str:
    """Away-team ATS result. spread is home-perspective; away covers when
    home margin < spread (i.e. away did better than the spread)."""
    if spread is None or margin is None:
        return "NA"
    if margin < spread:
        return "w"
    if margin > spread:
        return "l"
    return "p"


def _ou(combined: float, total: Optional[float]) -> str:
    if total is None or combined is None:
        return "NA"
    if combined > total:
        return "o"
    if combined < total:
        return "u"
    return "p"


def _apply(agg, season_key, split_types: List[str], team_id: int, line: dict) -> None:
    """Merge one team-game line into every matching split + career (None)."""
    for sp in split_types:
        for key in ((None,), (season_key,)):
            d = agg[key][sp].get(team_id)
            if d is None:
                d = _empty_agg()
                agg[key][sp][team_id] = d
            d["games"] += 1
            d["wins"] += line["win"]
            d["losses"] += line["loss"]
            for f in ("pf", "pa", "fgm", "fga", "threem", "threea",
                      "ftm", "fta", "reb", "ast", "stl", "blk", "tov", "fouls"):
                d[f] += line[f]
            d["pace_sum"] += line["pace"]
            # ATS / OU
            a = line["ats"]; o = line["ou"]
            if a == "w": d["ats_w"] += 1
            elif a == "l": d["ats_l"] += 1
            elif a == "p": d["ats_p"] += 1
            if o == "o": d["ou_o"] += 1
            elif o == "u": d["ou_u"] += 1
            elif o == "p": d["ou_p"] += 1


async def _bulk_insert(db: AsyncSession, rows: Sequence[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    col_sql = ", ".join(cols)
    ph = ", ".join([":" + c for c in cols])
    await db.execute(text(
        f"INSERT INTO nba.team_splits ({col_sql}) VALUES ({ph})"
    ), rows)


async def run_once(db: AsyncSession) -> dict:
    return await build_team_splits(db)
