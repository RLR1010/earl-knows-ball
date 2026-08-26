"""Team matchup — bends #2 (TrendSquad trends) + #3 (side-by-side comparison)
into one user-facing endpoint. Reuses the same sport logic that already powers
the chat tools' get_team_trends / get_team_comparison / get_team_split_stats,
so the data shown in the matchup modal matches what Earl would say in chat.

Endpoint: GET /matchup?sport={nfl|nba|mlb}&game_id={id}  (user-facing, 8001)
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..core.security import get_optional_current_user, user_is_premium

router = APIRouter(tags=["matchup"])

_VALID = ("nfl", "nba", "mlb")

# Premium-only advanced handicapping stats: (label, dot-path into team.trends,
# True when lower is better). Only returned/rendered for premium subscribers;
# stripped from the payload for free/anonymous callers so the data stays
# protected at the API layer (not just hidden in the UI).
_PREMIUM_STATS = {
    "nba": [
        ("Offensive Rtg L10", ["last_10", "ortg"], False),
        ("Defensive Rtg L10", ["last_10", "drtg"], True),
        ("eFG% L10", ["last_10", "efg_pct"], False),
        ("ATS Margin L10", ["last_10", "ats_margin"], False),
        ("Off Rtg vs League", ["year_adjusted", "off_rating"], False),
        ("PPG Consistency CV10", ["consistency", "ppg_cv10"], True),  # low CV = reliable team
    ],
    # MLB trends only expose 5/10-game windows (no 15/20 or BB/9 there), so the
    # premium edge comes from the pitching-efficiency COMPARISON metrics, which
    # we gate behind premium (free users keep the public batting comparison).
    "mlb": [
        ("Team ERA", ["#comp", "Team ERA"], True),
        ("Team WHIP", ["#comp", "Team WHIP"], True),
        ("Team K/9", ["#comp", "Team K/9"], False),
        ("Team BB/9", ["#comp", "Team BB/9"], True),
    ],
    "nfl": [],
}

# MLB pitching-efficiency comparison metrics that are premium (stripped from the
# public head-to-head table for free/anonymous viewers).
_MLB_PREMIUM_COMP_KEYS = {"Team ERA", "Team WHIP", "Team K/9", "Team BB/9"}

# Keys to strip from the raw ``trends`` dict for a non-premium caller so the
# advanced data isn't reachable by just curling the endpoint.
_PREMIUM_TREND_KEYS = {
    "nba": {("last_10", "ortg"), ("last_10", "drtg"), ("last_10", "efg_pct"),
            ("last_10", "ats_margin"), ("last_10", "ou_margin"),
            ("last_5", "ortg"), ("last_5", "drtg"), ("last_5", "efg_pct"),
            ("last_5", "ats_margin"), ("last_5", "ou_margin"),
            ("year_adjusted", "*"), ("consistency", "*"), ("star_ppg_5", "*")},
    "mlb": {("latest_summary", "avg15"), ("latest_summary", "slg15"),
            ("latest_summary", "ops15"), ("latest_summary", "era15"),
            ("latest_summary", "whip15"), ("latest_summary", "avg20"),
            ("latest_summary", "slg20"), ("latest_summary", "ops20"),
            ("latest_summary", "era20"), ("latest_summary", "whip20"),
            ("latest_summary", "bb9_5"), ("latest_summary", "bb9_10"),
            ("latest_summary", "k9_15"), ("latest_summary", "k9_20")},
    "nfl": set(),
}

# Lazy-import the sport chat-tool modules so we don't pay import cost unless
# that sport is hit, and so a single sport's import errors don't break the app.
def _tools(sport: str):
    if sport == "nfl":
        from ..chat_tools import nfl as m
    elif sport == "nba":
        from ..chat_tools import nba as m
    else:
        from ..chat_tools import mlb as m
    return m


def _num(v) -> float | None:
    try:
        n = float(v)
        return n if n == n else None  # NaN guard
    except (TypeError, ValueError):
        return None


def _path_get(obj, path):
    """Walk a dict path (list of keys) returning the value or None."""
    cur = obj
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _strip_premium(obj, sport, seen=None):
    """Return a copy of `obj` with premium-only keys removed for non-premium.
    Handles the ``("parent", "*")`` wildcard (drop whole subkey) and exact
    (``(parent, key)`` pairs. Comparison dict not touched here (gated elsewhere).
    """
    if not isinstance(obj, dict):
        return obj
    out = {}
    rules = _PREMIUM_TREND_KEYS.get(sport, set())
    for k, v in obj.items():
        if isinstance(v, dict):
            drop_whole = (k, "*") in rules
            if drop_whole:
                continue
            out[k] = {sk: sv for sk, sv in v.items() if (k, sk) not in rules}
        else:
            out[k] = v
    return out


def _build_premium_stats(home_trends, away_trends, comparison, sport):
    """Curated premium-only stat rows per team for the frontend.
    `comparison` is the raw {compare:{metric:{abbr:val}}, ...} dict.
    Paths starting with "#comp" read from comparison.compare by metric key.
    Returns [{label, av, bv, lower}] paired for a head-to-head table.
    """
    spec = _PREMIUM_STATS.get(sport, [])
    rows = []
    home, away = home_trends or {}, away_trends or {}
    comp = comparison.get("compare", {}) if isinstance(comparison, dict) else {}
    ta = comparison.get("team_a") if isinstance(comparison, dict) else None
    tb = comparison.get("team_b") if isinstance(comparison, dict) else None
    # Resolve abbrevs: comparison rows are keyed by abbr; ``team_a/team_b`` may be
    # "Full (ABBR)" (MLB) or "ABBR" (NBA) -> pull the bare abbrev from either.
    def abbr_of(s):
        if not s:
            return None
        m = re.search(r"\(([^)]+)\)$", s)
        return (m.group(1) if m else s).strip()
    home_abbr = abbr_of(ta)
    away_abbr = abbr_of(tb)
    for label, path, lower in spec:
        if sport == "mlb" and path and path[0] == "#comp":
            key = path[1]
            row = comp.get(key, {}) if isinstance(comp, dict) else {}
            hv = _num(row.get(home_abbr) if home_abbr else None)
            av = _num(row.get(away_abbr) if away_abbr else None)
        else:
            hv = _num(_path_get(home, path))
            av = _num(_path_get(away, path))
        rows.append({"label": label, "av": hv, "bv": av, "lower": lower})
    return rows


@router.get("/matchup")
async def matchup(
    sport: str = Query(..., description="nfl | nba | mlb"),
    game_id: int | None = Query(None, description="Game id (resolves home/away teams)"),
    home: str | None = Query(None, description="Home team name or abbr (optional if game_id given)"),
    away: str | None = Query(None, description="Away team name or abbr (optional if game_id given)"),
    db: AsyncSession = Depends(get_db),
    current_user: "User | None" = Depends(get_optional_current_user),
):
    premium = bool(current_user and user_is_premium(current_user))
    sport = (sport or "").strip().lower()
    if sport not in _VALID:
        raise HTTPException(400, f"Invalid sport '{sport}'. Choose one of: {', '.join(_VALID)}.")

    if game_id is None and home is None and away is None:
        raise HTTPException(400, "Provide a game_id, or both home and away team names.")

    mod = _tools(sport)

    # Resolve team names: prefer game_id, else require home+away.
    if game_id is not None:
        g = (await db.execute(
            text(f"SELECT home_team_id, away_team_id, date FROM {sport}.games WHERE id = :gid"),
            {"gid": game_id},
        )).mappings().first()
        if not g:
            raise HTTPException(404, f"No {sport} game with id {game_id}.")
        names = {}
        for side, tid in (("home", g["home_team_id"]), ("away", g["away_team_id"])):
            t = (await db.execute(
                text(f"SELECT name, abbreviation FROM {sport}.teams WHERE id = :tid"),
                {"tid": tid},
            )).mappings().first()
            names[side] = {"id": tid, "name": t["name"] if t else str(tid), "abbr": t["abbreviation"] if t else str(tid)}
        home_name = names["home"]["name"]
        away_name = names["away"]["name"]
        game_date = str(g["date"])[:10]
    else:
        if not home or not away:
            raise HTTPException(400, "Provide both home and away team names/abbrs (or a game_id).")
        home_name, away_name = home, away
        game_date = None

    # 1) Trends for each team (recent form, ATS, O/U). Pass the abbreviation
    # (unique) rather than the full name, since _resolve_team_id raises
    # MultipleResultsFound on ambiguous full names.
    trends_home = await mod._get_team_trends(db, {"team_name": names["home"]["abbr"] if game_id is not None else home})
    trends_away = await mod._get_team_trends(db, {"team_name": names["away"]["abbr"] if game_id is not None else away})

    # 2) Split stats (home/road, F1/full) if available.
    split_home = None
    split_away = None
    if hasattr(mod, "_get_team_split_stats"):
        try:
            split_home = await mod._get_team_split_stats(db, {"team_name": names["home"]["abbr"] if game_id is not None else home})
        except Exception:
            split_home = None
        try:
            split_away = await mod._get_team_split_stats(db, {"team_name": names["away"]["abbr"] if game_id is not None else away})
        except Exception:
            split_away = None

    # 3) Side-by-side comparison.
    comparison = await mod._get_team_comparison(
        db,
        {
            "team_a": names["home"]["abbr"] if game_id is not None else home,
            "team_b": names["away"]["abbr"] if game_id is not None else away,
        },
    )

    # Premium gating: strip advanced stats for free/anonymous callers.
    if not premium:
        trends_home = _strip_premium(trends_home, sport) if isinstance(trends_home, dict) else trends_home
        trends_away = _strip_premium(trends_away, sport) if isinstance(trends_away, dict) else trends_away
        # MLB: remove premium pitching-efficiency metrics from the public
        # head-to-head table (free users keep the public batting comparison).
        if sport == "mlb" and isinstance(comparison, dict) and isinstance(comparison.get("compare"), dict):
            comparison['compare'] = {
                k: v for k, v in comparison['compare'].items() if k not in _MLB_PREMIUM_COMP_KEYS
            }

    premium_stats = (
        _build_premium_stats(trends_home, trends_away, comparison, sport) if premium else []
    )

    teams = {
        "home": {
            "name": home_name,
            "id": names["home"]["id"] if game_id is not None else None,
            "abbr": names["home"]["abbr"] if game_id is not None else (home or home_name),
            "trends": trends_home if not isinstance(trends_home, dict) or "error" not in trends_home else None,
            "trends_error": trends_home.get("error") if isinstance(trends_home, dict) and "error" in trends_home else None,
            "splits": split_home if isinstance(split_home, dict) and "error" not in split_home else None,
        },
        "away": {
            "name": away_name,
            "id": names["away"]["id"] if game_id is not None else None,
            "abbr": names["away"]["abbr"] if game_id is not None else (away or away_name),
            "trends": trends_away if not isinstance(trends_away, dict) or "error" not in trends_away else None,
            "trends_error": trends_away.get("error") if isinstance(trends_away, dict) and "error" in trends_away else None,
            "splits": split_away if isinstance(split_away, dict) and "error" not in split_away else None,
        },
    }

    return {
        "sport": sport,
        "game_id": game_id,
        "game_date": game_date,
        "teams": teams,
        "comparison": comparison if isinstance(comparison, dict) and "error" not in comparison else None,
        "comparison_error": comparison.get("error") if isinstance(comparison, dict) and "error" in comparison else None,
        "viewer_premium": premium,
        "premium_stats": premium_stats,
    }
