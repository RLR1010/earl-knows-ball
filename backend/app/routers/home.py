"""Home page router — upcoming games across all sports (or a single sport)."""

import math
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.routers.games import _records_as_of_batch


# ---------------------------------------------------------------------------
# Best Bets helpers
# ---------------------------------------------------------------------------

def _american_to_implied(odds: float | int | str | None) -> float | None:
    """Convert American odds to implied probability (0..1).
    +120 -> 1/(1+1.2)=0.4545 ; -150 -> 150/(150+100)=0.60 .
    Returns None for missing/zero/irregular odds."""
    if odds is None:
        return None
    try:
        o = float(odds)
    except (TypeError, ValueError):
        return None
    if o == 0 or abs(o) == float("inf") or abs(o) > 100000:
        return None
    if o > 0:
        return 100.0 / (o + 100.0)
    return (-o) / (-o + 100.0)


# Map each sport to the columns that hold the calibrated probability + odds for
# each of the three bet markets. MLB's spread is the run line -> rl_conf_cal.
_BEST_BET_MARKETS = [
    # (market key, conf column, odds column, feature-friendly label)
    ("ats", "ats_conf_cal", "ats_odds"),
    ("ou", "ou_conf_cal", "ou_odds"),
    ("ml", "ml_conf_cal", "ml_odds"),
]


_MARKET_CONF_COL = {"mlb": {"ats": "rl_conf_cal"}, "nba": {}, "nfl": {}}


def _build_best_bets_sql(schema: str, kind: str):
    """Return the best game-prediction rows (source='api') with the shared card
    shape plus the raw conf/odds columns needed to compute edge.

    Reuses _pick_aliases for the pick_* shape. Adds *_cal / *_odds columns from
    gp; the caller computes edge = conf_cal - implied(odds) on the winning leg.
    Where clause requires an actual api pick (any of the three markets) and the
    game still scheduled.
    """
    p = _pick_aliases(kind)
    conf_cols = {
        "conf_ats": f"gp." + _MARKET_CONF_COL.get(kind, {}).get("ats", "ats_conf_cal") + " AS conf_ats",
        "conf_ou": "gp.ou_conf_cal AS conf_ou",
        "conf_ml": "gp.ml_conf_cal AS conf_ml",
    }
    pitcher_cols = (
        "g.home_pitcher_name AS home_pitcher_name,\n        g.away_pitcher_name AS away_pitcher_name,"
        if kind == "mlb"
        else "NULL AS home_pitcher_name,\n        NULL AS away_pitcher_name,"
    )
    external_id = (
        f"g.{kind}_game_id AS external_id" if kind in ("mlb", "nba") else "NULL AS external_id"
    )
    # NBA resolves numeric team-id picks to abbreviations via psp/pml joins
    # (mirrors _build_sql); other sports pass through pick text directly.
    joins = (
        f"LEFT JOIN {schema}.teams psp ON psp.id = CASE WHEN gp.spread_pick ~ '^[0-9]+$' "
        f"THEN gp.spread_pick::bigint END\n"
        f"    LEFT JOIN {schema}.teams pml ON pml.id = CASE WHEN gp.ml_pick ~ '^[0-9]+$' "
        f"THEN gp.ml_pick::bigint END"
        if kind == "nba"
        else ""
    )
    conf_ats_col = _MARKET_CONF_COL.get(kind, {}).get("ats", "ats_conf_cal")
    return f"""
    SELECT
        '{kind}'::text AS sport,
        g.id,
        {external_id},
        g.date,
        (g.date AT TIME ZONE 'America/Chicago')::date AS game_date,
        g.season_id,
        g.home_team_id,
        g.away_team_id,
        g.status::text AS status,
        ht.abbreviation AS home_team,
        at.abbreviation AS away_team,
        g.home_score,
        g.away_score,
        {pitcher_cols}
        g.venue,
        c.closing_spread AS spread,
        c.closing_ou AS over_under,
        c.closing_home_ml AS home_moneyline,
        c.closing_away_ml AS away_moneyline,
        gp.predicted_margin,
        {p['pick_spread']},
        {p['pick_over_under']},
        {p['pick_moneyline']},
        {p['pick_ats_ev']},
        {p['pick_ou_ev']},
        {p['pick_ml_ev']},
        gp.ats_odds AS ats_odds,
        gp.ou_odds AS ou_odds,
        gp.ml_odds AS ml_odds,
        {conf_cols['conf_ats']},
        {conf_cols['conf_ou']},
        {conf_cols['conf_ml']}
    FROM {schema}.games g
    JOIN {schema}.teams ht ON ht.id = g.home_team_id
    JOIN {schema}.teams at ON at.id = g.away_team_id
    LEFT JOIN {schema}.betting_lines_consolidated c ON c.game_id = g.id
    LEFT JOIN {schema}.game_predictions gp ON gp.game_id = g.id
    {joins}
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
      AND g.date <= :horizon
      AND gp.source = 'api'
      AND (
          gp.{conf_ats_col} IS NOT NULL
          OR gp.ou_conf_cal IS NOT NULL
          OR gp.ml_conf_cal IS NOT NULL
      )
    ORDER BY g.date ASC
    LIMIT :limit
    """

router = APIRouter(tags=["home"])

DECIMAL_FIELDS = [
    "spread", "over_under", "home_moneyline", "away_moneyline",
    "opening_spread", "opening_total",
    "opening_home_moneyline", "opening_away_moneyline",
    "predicted_margin",
    "pick_ats_ev", "pick_ou_ev", "pick_ml_ev",
]


def _fix_decimals(row: dict) -> dict:
    """Cast Decimal values to Python floats for JSON serialization."""
    for field in DECIMAL_FIELDS:
        val = row.get(field)
        if val is not None:
            try:
                row[field] = float(val)
            except (TypeError, ValueError, OverflowError):
                row[field] = None
    return row


# Shared SELECT column names so all three sports return the SAME shape the
# schedule-page shared card (ScheduleGameCard) expects: home_team/away_team are
# abbreviations, and the rich pick_* / result_* fields drive the picks panel.
def _pick_aliases(kind: str):
    """Return SQL alias fragments mapping each sport's prediction columns to the
    shared pick_* / result_* shape."""
    if kind == "mlb":
        return {
            "pick_spread": "gp.run_line_pick AS pick_spread",
            "pick_over_under": "gp.ou_pick AS pick_over_under",
            "pick_moneyline": "gp.ml_pick AS pick_moneyline",
            "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
            "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
            "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
            "result_spread": "gp.run_line_result AS result_spread",
            "result_over_under": "gp.ou_result AS result_over_under",
            "result_moneyline": "gp.ml_result AS result_moneyline",
        }
    if kind == "nba":
        # NBA resolves numeric team-id picks to abbreviations to match nba_stats.py
        return {
            "pick_spread": "CASE WHEN gp.spread_pick IS NOT NULL "
            "THEN (CASE WHEN psp.name = ht.name THEN ht.abbreviation "
            "WHEN psp.name = at.name THEN at.abbreviation "
            "ELSE COALESCE(psp.abbreviation, gp.spread_pick) END) "
            "ELSE NULL END AS pick_spread",
            "pick_over_under": "gp.ou_pick AS pick_over_under",
            "pick_moneyline": "CASE WHEN gp.ml_pick IS NOT NULL "
            "THEN (CASE WHEN pml.name = ht.name THEN ht.abbreviation "
            "WHEN pml.name = at.name THEN at.abbreviation "
            "ELSE COALESCE(pml.abbreviation, gp.ml_pick) END) "
            "ELSE NULL END AS pick_moneyline",
            "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
            "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
            "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
            "result_spread": "gp.ats_result AS result_spread",
            "result_over_under": "gp.ou_result AS result_over_under",
            "result_moneyline": "gp.ml_result AS result_moneyline",
        }
    # nfl uses raw spread_pick / ats_result (matches games.py)
    return {
        "pick_spread": "gp.spread_pick AS pick_spread",
        "pick_over_under": "gp.ou_pick AS pick_over_under",
        "pick_moneyline": "gp.ml_pick AS pick_moneyline",
        "pick_ats_ev": "gp.ats_ev AS pick_ats_ev",
        "pick_ou_ev": "gp.ou_ev AS pick_ou_ev",
        "pick_ml_ev": "gp.ml_ev AS pick_ml_ev",
        "result_spread": "gp.ats_result AS result_spread",
        "result_over_under": "gp.ou_result AS result_over_under",
        "result_moneyline": "gp.ml_result AS result_moneyline",
    }


def _build_sql(schema: str, kind: str):
    """Build the per-sport SELECT. Shared columns are identical so downstream
    consumers (site home + sport home pages) get one uniform shape."""
    p = _pick_aliases(kind)
    pitcher_cols = (
        "g.home_pitcher_name AS home_pitcher_name,\n"
        "        g.away_pitcher_name AS away_pitcher_name,"
        if kind == "mlb"
        else "NULL AS home_pitcher_name,\n        NULL AS away_pitcher_name,"
    )
    # external_id: only MLB/NBA have a *_game_id column on the games table.
    external_id = (
        f"g.{kind}_game_id AS external_id"
        if kind in ("mlb", "nba")
        else "NULL AS external_id"
    )
    joins = (
        f"LEFT JOIN {schema}.teams psp ON psp.id = CASE WHEN gp.spread_pick ~ '^[0-9]+$' "
        f"THEN gp.spread_pick::bigint END\n"
        f"    LEFT JOIN {schema}.teams pml ON pml.id = CASE WHEN gp.ml_pick ~ '^[0-9]+$' "
        f"THEN gp.ml_pick::bigint END"
        if kind == "nba"
        else ""
    )
    return f"""
    SELECT
        '{kind}'::text AS sport,
        g.id,
        {external_id},
        g.date,
        (g.date AT TIME ZONE 'America/Chicago')::date AS game_date,
        g.season_id,
        g.home_team_id,
        g.away_team_id,
        g.status::text AS status,
        ht.abbreviation AS home_team,
        at.abbreviation AS away_team,
        g.home_score,
        g.away_score,
        {pitcher_cols}
        g.venue,
        c.closing_spread AS spread,
        c.closing_ou AS over_under,
        c.closing_home_ml AS home_moneyline,
        c.closing_away_ml AS away_moneyline,
        c.opening_spread,
        c.opening_ou AS opening_total,
        c.opening_home_ml AS opening_home_moneyline,
        c.opening_away_ml AS opening_away_moneyline,
        gp.predicted_margin,
        {p['pick_spread']},
        {p['pick_over_under']},
        {p['pick_moneyline']},
        {p['pick_ats_ev']},
        {p['pick_ou_ev']},
        {p['pick_ml_ev']},
        {p['result_spread']},
        {p['result_over_under']},
        {p['result_moneyline']}
    FROM {schema}.games g
    JOIN {schema}.teams ht ON ht.id = g.home_team_id
    JOIN {schema}.teams at ON at.id = g.away_team_id
    LEFT JOIN {schema}.betting_lines_consolidated c ON c.game_id = g.id
    LEFT JOIN {schema}.game_predictions gp ON gp.game_id = g.id
    {joins}
    WHERE g.status::text = 'SCHEDULED'
      AND g.date > :now
      AND g.date <= :horizon
    ORDER BY g.date ASC
    LIMIT :limit
    """


@router.get("/home/upcoming-games")
async def upcoming_games(
    sport: str = Query("all", description="Filter by sport: all, mlb, nba, nfl"),
    days: int = Query(5, description="Only show games within this many days from now"),
    db: AsyncSession = Depends(get_db),
):
    """Return upcoming scheduled games, sorted by date ascending.

    - sport=all (default): up to 6 games PER SPORT, for every sport that has
      games within the next `days` days (site home). So the home page can show
      e.g. 6 MLB + 6 NFL + 6 NBA side by side.
    - sport=mlb|nba|nfl: up to 6 games from that sport only (sport home pages).
    """
    sport = (sport or "all").lower()
    if sport not in ("all", "mlb", "nba", "nfl"):
        sport = "all"
    days = max(1, min(days, 14))

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=days)
    results = []

    specs = []
    if sport in ("all", "mlb"):
        specs.append(("mlb", "mlb"))
    if sport in ("all", "nba"):
        specs.append(("nba", "nba"))
    if sport in ("all", "nfl"):
        specs.append(("nfl", "nfl"))

    for schema, kind in specs:
        sql = _build_sql(schema, kind)
        rows = (
            await db.execute(
                text(sql), {"now": now, "horizon": horizon, "limit": 6}
            )
        ).mappings().all()
        out = [_fix_decimals(dict(r)) for r in rows]
        # Attach each team's record AT THE TIME of the game (same as schedule pages)
        pairs = []
        for g in out:
            if g.get("home_team_id") and g.get("season_id"):
                pairs.append((g["home_team_id"], g["game_date"], g["season_id"]))
            if g.get("away_team_id") and g.get("season_id"):
                pairs.append((g["away_team_id"], g["game_date"], g["season_id"]))
        records = await _records_as_of_batch(db, schema, pairs)
        for g in out:
            g["home_record"] = records.get(
                (g.get("home_team_id"), str(g.get("game_date")), g.get("season_id"))
            )
            g["away_record"] = records.get(
                (g.get("away_team_id"), str(g.get("game_date")), g.get("season_id"))
            )
        results.extend(out)

    return results


@router.get("/home/best-bets")
async def best_bets(
    sport: str = Query("all", description="Filter by sport: all, mlb, nba, nfl"),
    limit: int = Query(6, description="Max best bets to return across all sports"),
    db: AsyncSession = Depends(get_db),
):
    """Return Earl's single best bet per upcoming game, ranked by edge.

    For each upcoming game with a real api prediction, compute the edge
    (calibrated model confidence minus the book's implied probability) for all
    three markets (ATS/OU/ML), keep the single highest-edge leg, then sort
    across sports by edge and return the top `limit`.

    Each returned row is the SAME shared shape the ScheduleGameCard expects
    (so the frontend can drop it straight into a card), plus best-bet metadata:
      - best_bet_type: "ats" | "ou" | "ml"
      - best_bet_label: human readable pick for that leg (e.g. "LAD -1.5")
      - best_bet_edge:   decimal edge (0..1), conf_cal - implied
      - best_bet_edge_pct / best_bet_confidence_pct: whole-number percentages
      - best_bet_ev:     model EV for that leg
    """
    sport = (sport or "all").lower()
    if sport not in ("all", "mlb", "nba", "nfl"):
        sport = "all"
    limit = max(1, min(limit, 20))

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=14)
    specs = []
    if sport in ("all", "mlb"):
        specs.append(("mlb", "mlb"))
    if sport in ("all", "nba"):
        specs.append(("nba", "nba"))
    if sport in ("all", "nfl"):
        specs.append(("nfl", "nfl"))

    candidates = []
    for schema, kind in specs:
        sql = _build_best_bets_sql(schema, kind)
        rows = (
            await db.execute(
                text(sql), {"now": now, "horizon": horizon, "limit": 30}
            )
        ).mappings().all()
        for r in rows:
            g = _fix_decimals(dict(r))
            g.setdefault("pick_ats_ev", r.get("pick_ats_ev"))
            best = _best_leg(g)
            if best is None or best["edge"] is None or best["edge"] <= 0:
                continue
            # Resolve pick label / EV for the chosen leg.
            bt = best["type"]
            if bt == "ats":
                label = g.get("pick_spread")
                ev = g.get("pick_ats_ev")
            elif bt == "ou":
                label = g.get("pick_over_under")
                ev = g.get("pick_ou_ev")
            else:
                label = g.get("pick_moneyline")
                ev = g.get("pick_ml_ev")
            if label in (None, "", "Push / No edge"):
                continue
            g["best_bet_type"] = bt
            g["best_bet_label"] = str(label)
            g["best_bet_edge"] = round(best["edge"], 4)
            g["best_bet_edge_pct"] = round(best["edge"] * 100, 1)
            g["best_bet_confidence_pct"] = (
                round(best["conf"] * 100, 1) if best["conf"] is not None else None
            )
            g["best_bet_implied_pct"] = (
                round(best["implied"] * 100, 1) if best["implied"] is not None else None
            )
            g["best_bet_ev"] = ev
            candidates.append(g)

    candidates.sort(key=lambda x: x["best_bet_edge"], reverse=True)
    results = candidates[:limit]

    # Attach team records as-of the game (same as the schedule/home cards).
    pairs = []
    for g in results:
        if g.get("home_team_id") and g.get("season_id"):
            pairs.append((g["home_team_id"], g["game_date"], g["season_id"]))
        if g.get("away_team_id") and g.get("season_id"):
            pairs.append((g["away_team_id"], g["game_date"], g["season_id"]))
    spec_map = {"mlb": "mlb", "nba": "nba", "nfl": "nfl"}
    # records attach per-schema; dedupe by (schema)
    schema_rows = {}
    for g in results:
        schema_rows.setdefault(spec_map[g["sport"]], []).append(g)
    for schema, gs in schema_rows.items():
        spairs = []
        for gg in gs:
            if gg.get("home_team_id") and gg.get("season_id"):
                spairs.append((gg["home_team_id"], gg["game_date"], gg["season_id"]))
            if gg.get("away_team_id") and gg.get("season_id"):
                spairs.append((gg["away_team_id"], gg["game_date"], gg["season_id"]))
        records = await _records_as_of_batch(db, schema, spairs)
        for gg in gs:
            gg["home_record"] = records.get(
                (gg.get("home_team_id"), str(gg.get("game_date")), gg.get("season_id"))
            )
            gg["away_record"] = records.get(
                (gg.get("away_team_id"), str(gg.get("game_date")), gg.get("season_id"))
            )

    return results


def _best_leg(g: dict):
    """Pick the single highest-edge market for a game row.

    Returns {\"type\", \"edge\", \"conf\", \"implied\"} or None if nothing usable.
    edge = conf_cal - implied(odds); only positive edges qualify."""
    best = None
    for market, _, _ in _BEST_BET_MARKETS:
        conf_key = "conf_ats" if market == "ats" else ("conf_ou" if market == "ou" else "conf_ml")
        odds_key = market + "_odds"
        conf = g.get(conf_key)
        odds = g.get(odds_key)
        if conf is None or odds is None:
            continue
        try:
            conf = float(conf)
        except (TypeError, ValueError):
            continue
        implied = _american_to_implied(odds)
        if implied is None or conf <= 0 or conf >= 1:
            continue
        edge = conf - implied
        if edge <= 0:
            continue
        if best is None or edge > best["edge"]:
            best = {"type": market, "edge": edge, "conf": conf, "implied": implied}
    return best


# ---------------------------------------------------------------------------
# Standings / Down-the-Stretch frames
# ---------------------------------------------------------------------------

# Sport -> team grouping column + games status (all three schemas are parallel).
_SPORT_TEAM_COL = {"nfl": "conference", "nba": "conference", "mlb": "league"}
_VALID_SPORTS = tuple(_SPORT_TEAM_COL.keys())


@router.get("/home/standings")
async def home_standings(
    sport: str = Query(..., description="nfl | nba | mlb"),
    season_year: int | None = Query(None, description="Season year; defaults to the latest season with finals"),
    conference: str | None = Query(None, description="Optional league/conference filter (AFC, NFC, East, West, AL, NL)"),
    division: str | None = Query(None, description="Optional division filter (North, South, East, West, Atlantic, ...)"),
    db: AsyncSession = Depends(get_db),
):
    """Standings for a sport's latest (or given) season, grouped by
    conference/league then division, with down-the-stretch frames:
    W-L, pct, games back, current streak, last-10, home/road split, and
    points for/against + differential.

    Games back is computed per-group vs. the best win pct in that group's
    division (MLB uses the leader of each division; NFL/NBA use divisional
    leaders per the traditional "games back to lead the division" framing).
    """
    sport = (sport or "").strip().lower()
    if sport not in _VALID_SPORTS:
        raise HTTPException(status_code=400, detail=f"Invalid sport '{sport}'. Choose one of: {', '.join(_VALID_SPORTS)}.")

    # Resolve the season id. If not given, pick the latest season that has any
    # final game (so an off-season year won't return an empty table). We also
    # derive whether the sport is currently "in season": the latest season has
    # any game dated on/after today (upcoming) OR it has non-final games.
    _today = date.today()
    if season_year is None:
        season_sql = f"""
            SELECT s.id, s.year,
                   (SELECT count(*) FROM {sport}.games g
                    WHERE g.season_id = s.id AND g.date >= :today)        AS upcoming,
                   (SELECT count(*) FROM {sport}.games g
                    WHERE g.season_id = s.id
                      AND lower(g.status::text) <> 'final')               AS nonfinal
            FROM {sport}.seasons s
            WHERE EXISTS (SELECT 1 FROM {sport}.games g WHERE g.season_id = s.id AND lower(g.status::text) = 'final')
            ORDER BY s.year DESC
            LIMIT 1
        """
        res = await db.execute(text(season_sql), {"today": _today})
        season = res.mappings().first()
        if not season:
            return {"sport": sport, "season": None, "in_season": False, "conferences": [], "teams": []}
        season_id, season_year = season["id"], season["year"]
        # In season = the league currently has games on/after today (upcoming or
        # in-progress). A purely finished season (all past dates) is off-season.
        in_season = bool(season["upcoming"])
    else:
        season_res = await db.execute(text(f"SELECT id FROM {sport}.seasons WHERE year = :y"), {"y": season_year})
        season_row = season_res.mappings().first()
        if not season_row:
            raise HTTPException(status_code=404, detail=f"No season found for {sport} year {season_year}.")
        season_id = season_row["id"]
        in_season = None  # caller explicitly chose a season; unused


    group_col = _SPORT_TEAM_COL[sport]

    # One pass over all final games of the season to build per-team aggregates.
    games_sql = f"""
        SELECT
            g.home_team_id AS team_id,
            g.date,
            g.home_score, g.away_score,
            'home' AS side,
            CASE WHEN g.home_score > g.away_score THEN 1 ELSE 0 END AS won
        FROM {sport}.games g
        WHERE g.season_id = :sid AND lower(g.status::text) = 'final'
        UNION ALL
        SELECT
            g.away_team_id AS team_id,
            g.date,
            g.home_score, g.away_score,
            'away' AS side,
            CASE WHEN g.away_score > g.home_score THEN 1 ELSE 0 END AS won
        FROM {sport}.games g
        WHERE g.season_id = :sid AND lower(g.status::text) = 'final'
    """
    rows = (await db.execute(text(games_sql), {"sid": season_id})).mappings().all()
    if not rows:
        return {"sport": sport, "season": season_year, "in_season": in_season, "conferences": [], "teams": []}

    # teams + grouping
    teams_sql = f"""
        SELECT id, name, abbreviation, \"{group_col}\" AS \"group\", division, logo_url
        FROM {sport}.teams
    """
    teams = {t["id"]: dict(t) for t in (await db.execute(text(teams_sql))).mappings().all()}

    # Aggregate per team
    agg = {}
    for r in rows:
        tid = r["team_id"]
        a = agg.setdefault(tid, {"games": 0, "wins": 0, "losses": 0, "home_w": 0, "home_l": 0, "away_w": 0, "away_l": 0, "pf": 0, "pa": 0, "results": []})
        a["games"] += 1
        a["wins"] += r["won"]
        a["losses"] += (1 - r["won"])
        if r["side"] == "home":
            a["home_w"] += r["won"]
            a["home_l"] += (1 - r["won"])
        else:
            a["away_w"] += r["won"]
            a["away_l"] += (1 - r["won"])
        a["pf"] += r["home_score"] if r["side"] == "home" else r["away_score"]
        a["pa"] += r["away_score"] if r["side"] == "home" else r["home_score"]
        a["results"].append((r["date"], r["won"]))

    # Sort results chronologically per team to derive streak + last-10.
    def _streak_last10(res):
        res = sorted(res, key=lambda x: x[0])
        streak = 0
        for _, won in reversed(res):
            if won:
                streak = streak + 1 if streak >= 0 else 1
            else:
                streak = streak - 1 if streak <= 0 else -1
        last10 = res[-10:]
        l10_w = sum(1 for _, won in last10 if won)
        return streak, l10_w, len(last10)

    # Assemble team rows
    team_rows = []
    for tid, a in agg.items():
        streak, l10_w, l10_g = _streak_last10(a["results"])
        t = teams.get(tid)
        if not t:
            continue
        team_rows.append({
            "team_id": tid,
            "team_name": t["name"],
            "abbreviation": t["abbreviation"],
            "logo_url": t.get("logo_url"),
            "group": t.get("group"),
            "division": t.get("division"),
            "games": a["games"],
            "wins": a["wins"],
            "losses": a["losses"],
            "win_pct": round(a["wins"] / a["games"], 3) if a["games"] else 0.0,
            "streak": streak,
            "last10": {"wins": l10_w, "losses": l10_g - l10_w},
            "home": {"wins": a["home_w"], "losses": a["home_l"]},
            "away": {"wins": a["away_w"], "losses": a["away_l"]},
            "points_for": a["pf"],
            "points_against": a["pa"],
            "diff": a["pf"] - a["pa"],
        })

    # Group into conferences/leagues, then divisions, computing games back
    # per division (vs. division leader by win pct).
    groups = {}
    for r in team_rows:
        groups.setdefault(r["group"], {})
        groups[r["group"]].setdefault(r["division"], []).append(r)

    conferences = []
    for gname in sorted(groups.keys()):
        divisions = []
        for dname, members in sorted(groups[gname].items()):
            members.sort(key=lambda x: (-x["wins"], x["losses"]))
            leader_pct = members[0]["win_pct"] if members else 0.0
            for r in members:
                # games back vs division leader using win pct (NFL/NBA-style).
                r["games_back"] = round((leader_pct - r["win_pct"]) * r["games"] / 2, 1) if leader_pct > 0 else 0.0
            divisions.append({"division": dname, "teams": members})
        conferences.append({"name": gname, "divisions": divisions})

    if conference:
        conferences = [c for c in conferences if c["name"] == conference]
    if division:
        for c in conferences:
            c["divisions"] = [d for d in c["divisions"] if d["division"] == division]

    return {"sport": sport, "season": season_year, "in_season": in_season, "conferences": conferences, "teams": team_rows}
