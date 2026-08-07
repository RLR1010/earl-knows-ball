"""ESPN NFL API data ingestion for schedules, scores, and live data."""

import httpx
from datetime import datetime, timedelta
from dateutil import parser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Team, Game, Season
from app.models.nfl.game import GameStatus
from sqlalchemy import or_, and_


ESPN_TEAM_MAP = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BUF": "BUF",
    "CAR": "CAR", "CHI": "CHI", "CIN": "CIN", "CLE": "CLE",
    "DAL": "DAL", "DEN": "DEN", "DET": "DET", "GB": "GB",
    "HOU": "HOU", "IND": "IND", "JAX": "JAX", "KC": "KC",
    "LAC": "LAC", "LAR": "LAR", "LV": "LV",
    "OAK": "LV",  # Raiders (Oakland) -> Las Vegas
    "SD": "LAC",  # Chargers (San Diego) -> LA Chargers
    "STL": "LAR",  # Rams (St. Louis) -> LA Rams
    "MIA": "MIA", "MIN": "MIN", "NE": "NE", "NO": "NO",
    "NYG": "NYG", "NYJ": "NYJ", "PHI": "PHI", "PIT": "PIT",
    "SEA": "SEA", "SF": "SF", "TB": "TB", "TEN": "TEN",
    "WSH": "WAS",
}


SEASON_DATE_RANGES = {
    # NFL season approximate date ranges: (start, end) as mmdd
    2025: ("20250901", "20260215"),
    2024: ("20240901", "20250215"),
    2023: ("20230901", "20240215"),
    2022: ("20220901", "20230215"),
    2021: ("20210901", "20220215"),
    2020: ("20200901", "20210215"),
    2019: ("20190901", "20200215"),
    2018: ("20180901", "20190215"),
    2017: ("20170901", "20180215"),
    2016: ("20160901", "20170215"),
    2015: ("20150901", "20160215"),
    2014: ("20140901", "20150215"),
    2013: ("20130901", "20140215"),
    2012: ("20120901", "20130215"),
    2011: ("20110901", "20120215"),
    2010: ("20100901", "20110215"),
    2009: ("20090901", "20100215"),
    2008: ("20080901", "20090215"),
    2007: ("20070901", "20080215"),
    2006: ("20060901", "20070215"),
    2005: ("20050901", "20060215"),
}


async def fetch_espn_scoreboard(season: int, seasontype: int, week: int | None = None, force_dates: str | None = None) -> list:
    """Fetch all games for a season/week with pagination.
    
    Uses the dates parameter for historical seasons (pre-2025) since the
    year parameter only works for the current/upcoming season.
    """
    all_events = []
    page = 1

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
            
            date_range = SEASON_DATE_RANGES.get(season)
            if force_dates:
                # Override: use a specific date range (e.g. one month), no pagination needed
                params = {"dates": force_dates, "seasontype": seasontype}
                # fetch all events without pagination; month-level data fits in one page
                # (but we still need to break from the while loop after first fetch)
            elif date_range:
                # Use dates param for complete season data (handles future seasons correctly)
                params = {"dates": f"{date_range[0]}-{date_range[1]}", "seasontype": seasontype, "page": page, "limit": 100}
            else:
                # Use year param for current/recent seasons without a date range
                params = {"year": season, "seasontype": seasontype, "page": page}
            
            if week is not None and not date_range and not force_dates:
                # Only add week param when using year-based query
                params["week"] = week

            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

            events = data.get("events", [])
            if not events:
                break
            all_events.extend(events)

            # If force_dates was used, we only need the first page (month-level fits in one)
            if force_dates:
                break

            # Pagination
            total_pages = data.get("pageCount")
            if total_pages is not None:
                # year+week mode: API tells us how many pages
                if page >= total_pages:
                    break
            elif date_range:
                # dates mode: API returns partial pages until empty
                if len(events) < 100:
                    break
            else:
                # year-only mode (future seasons): each page has ~16 events, no pageCount
                # Stop only when a page has NO events
                if len(events) == 0:
                    break

            page += 1

            if page > 60:
                break

    return all_events


async def ingest_espn_schedule(
    session: AsyncSession,
    season_year: int = 2025,
    seasontype: int = 2,
) -> dict:
    """Load schedule from ESPN API."""
    # Delete existing games for this season to get a clean import
    result = await session.execute(select(Season).where(Season.year == season_year))
    season = result.scalar_one_or_none()
    if not season:
        season = Season(year=season_year)
        session.add(season)
        await session.flush()

    # Don't delete existing games — just skip duplicates (stats depend on them)
    # We'll only add new games that don't exist yet

    games_loaded = 0

    # Determine how to fetch the season:
    # For current/past seasons: use year+week params (returns correct data)
    # For future seasons: year+week returns stale data. Use month-by-month dates instead.
    all_events = []
    weeks = list(range(1, 23)) if seasontype in (2, 3) else [None]

    # Peek at just page 1 of week 1 to see if the API returns the correct season
    async with httpx.AsyncClient(timeout=30.0) as client:
        peek_url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
        peek_resp = await client.get(peek_url, params={"year": season_year, "seasontype": seasontype, "page": 1, "week": 1})
        peek_data = peek_resp.json()
        peek_events = peek_data.get("events", [])

    if not peek_events:
        return {"games_loaded": 0, "total_games": 0}

    event_season = peek_events[0].get("season", {}).get("year")

    if event_season == season_year:
        # Week-by-week with force_dates month chunks to avoid pagination issues with season-length date ranges
        # (The year+week param works, but SEASON_DATE_RANGES causes fetch_espn_scoreboard to paginate
        #  the entire season for each week when a date range exists. Month-by-month avoids this.)
        if seasontype == 1:
            # Preseason runs Aug–Sep; fetch those months (current year only)
            for month in range(8, 11):  # Aug-Oct
                month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)
        else:
            for month in range(9, 13):  # Sep-Dec
                month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)
            for month in range(1, 3):  # Jan-Feb (next year)
                month_range = f"{season_year+1}{month:02d}01-{season_year+1}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)
    else:
        # Week param returned stale data — fall back to month-by-month dates
        print(f"  [Earl] Week param returned {event_season} data, falling back to month-by-month")
        if seasontype == 1:
            for month in range(8, 11):  # Aug-Oct
                month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)
        else:
            for month in range(9, 13):  # Sep-Dec
                month_range = f"{season_year}{month:02d}01-{season_year}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)
            for month in range(1, 3):  # Jan-Feb (next year)
                month_range = f"{season_year+1}{month:02d}01-{season_year+1}{month:02d}31"
                events = await fetch_espn_scoreboard(season_year, seasontype, None, force_dates=month_range)
                if events:
                    all_events.extend(events)

    for event in all_events:
        competitions = event.get("competitions", [])
        if not competitions:
            continue

        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home_raw or not away_raw:
            continue

        home_abbr = ESPN_TEAM_MAP.get(home_raw["team"]["abbreviation"], home_raw["team"]["abbreviation"])
        away_abbr = ESPN_TEAM_MAP.get(away_raw["team"]["abbreviation"], away_raw["team"]["abbreviation"])

        r = await session.execute(select(Team).where(Team.abbreviation == home_abbr))
        home_team = r.scalar_one_or_none()
        r = await session.execute(select(Team).where(Team.abbreviation == away_abbr))
        away_team = r.scalar_one_or_none()

        if not home_team or not away_team:
            continue

        date_str = comp.get("date") or event.get("date", "")
        try:
            game_date = parser.parse(date_str)
        except (ValueError, TypeError):
            game_date = datetime.now()

        status_type = comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
        game_status = _map_espn_status(status_type)
        venue = comp.get("venue", {})

        game_id = int(event["id"])

        # Skip if already exists (use no_autoflush to avoid query-triggered flush of pending games)
        with session.no_autoflush:
            existing = await session.execute(select(Game).where(Game.id == game_id))
            if existing.scalar_one_or_none():
                continue

        # Get week from the event data
        event_week = event.get("week", {}).get("number", 0)
        game_type = "REG" if seasontype == 2 else ("PRE" if seasontype == 1 else "POST")

        # ESPN reuses week numbers across preseason (1-4) and regular season (1-18),
        # which would collide in our schedule. Renumber preseason weeks into a
        # distinct unused range (30-33) so they stay separate from regular season/playoffs.
        if game_type == "PRE" and event_week:
            stored_week = 29 + event_week  # PRE week 1 -> 30, ... PRE week 4 -> 33
        else:
            stored_week = event_week if event_week else 1

        game = Game(
            id=game_id,
            season_id=season.id,
            week=stored_week,
            game_type=game_type,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
            date=game_date,
            status=game_status,
            home_score=_safe_int(home_raw.get("score")),
            away_score=_safe_int(away_raw.get("score")),
            venue=venue.get("fullName") if venue else None,
            roof_type=_get_roof_type(venue),
            surface=venue.get("surface") if venue else None,
        )
        session.add(game)
        games_loaded += 1

        if games_loaded % 50 == 0:
            await session.flush()

    await session.commit()
    return {"games_loaded": games_loaded, "total_games": games_loaded}


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _get_roof_type(venue: dict | None) -> str | None:
    if not venue:
        return None
    raw = venue.get("indoor", None)
    if raw is True:
        return "dome"
    if raw is False:
        return "outdoor"
    return "outdoor"


def _map_espn_status(status: str) -> GameStatus:
    mapping = {
        "STATUS_SCHEDULED": GameStatus.SCHEDULED,
        "STATUS_IN_PROGRESS": GameStatus.IN_PROGRESS,
        "STATUS_FINAL": GameStatus.FINAL,
        "STATUS_POSTPONED": GameStatus.POSTPONED,
        "STATUS_CANCELLED": GameStatus.CANCELLED,
    }
    return mapping.get(status, GameStatus.SCHEDULED)


# How many hours before/after a scheduled game's start we still consider it
# "live-critical" and worth polling ESPN for. Games outside this window that
# are SCHEDULED have not started yet and nights-shifted games have all gone FINAL.
LIVE_WINDOW_HOURS_BEFORE = 3
LIVE_WINDOW_HOURS_AFTER = 5


async def update_live_nfl_games(session: AsyncSession) -> dict:
    """Sync live NFL game statuses/scores from ESPN for games currently being played.

    This is the "live update" mirror of MLB's update_game_statuses. It is designed
    to be called frequently (every few minutes) by a scheduled task, but it is
    self-gating: it checks the local DB first and makes NO ESPN call unless there
    is a game that is in progress (or scheduled to start soon). When no NFL games
    are live (off-season, non-game days, or between game windows) it returns
    immediately without touching ESPN, so it costs nothing to keep running.

    Returns a dict describing what it did.
    """
    now = datetime.now()

    # --- Step 1 (no ESPN yet): find games that might be live or about to be. ---
    # IN_PROGRESS games are always candidates. SCHEDULED games are candidates only
    # if their start is within the live window (they may have just kicked off, or
    # ESPN has marked them live on the scoreboard before we saw it).
    window_start = now - timedelta(hours=LIVE_WINDOW_HOURS_AFTER)
    window_end = now + timedelta(hours=LIVE_WINDOW_HOURS_BEFORE)

    result = await session.execute(
        select(Game).where(
            or_(
                Game.status == GameStatus.IN_PROGRESS,
                and_(
                    Game.status == GameStatus.SCHEDULED,
                    Game.date >= window_start,
                    Game.date <= window_end,
                ),
            )
        )
    )
    candidates = result.scalars().all()

    if not candidates:
        # No live games and nothing about to start. Do not call ESPN.
        return {"checked_games": 0, "updated_games": 0, "live_games": False}

    # --- Step 2: fetch the ESPN scoreboard once for the candidate games' dates. ---
    # Build the set of calendar dates we have candidate games for, then build a
    # single comma-separated dates param so one scoreboard call covers all of them
    # (game windows can straddle midnight, e.g. a 00:05 ET night game).
    cand_ids = {g.id for g in candidates}
    # Build a set of ESPN scoreboard `dates` keys that cover every candidate game.
    # The DB stores UTC dates (e.g. a 00:00Z kickoff is Aug 7), but ESPN groups
    # games by local/ET date (that same game appears under Aug 6). To never miss
    # a game due to that shift, expand each candidate's UTC date to +/- 1 day and
    # query the union — this also covers games straddling midnight.
    date_set = set()
    for g in candidates:
        d = g.date.date()
        # normalize to naive date (g.date is tz-aware; .date() strips tz)
        date_set.add(d.isoformat().replace("-", ""))
        date_set.add((d - timedelta(days=1)).isoformat().replace("-", ""))
        date_set.add((d + timedelta(days=1)).isoformat().replace("-", ""))
    dates_param = ",".join(sorted(date_set))

    all_events = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # ESPN's scoreboard accepts only a SINGLE `dates` value (comma-separated
            # multi-date returns 400). Loop over each candidate date separately.
            for one_date in sorted(date_set):
                pag = 1
                while True:
                    resp = await client.get(
                        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                        params={"dates": one_date},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    events = data.get("events", [])
                    all_events.extend(events)
                    total_pages = data.get("pageCount")
                    if total_pages is None:
                        if len(events) < 100:
                            break
                    elif pag >= total_pages:
                        break
                    pag += 1
                    if pag > 10:
                        break
    except Exception as exc:  # never let a live-refresh burst take the task down
        return {"checked_games": len(candidates), "updated_games": 0, "error": str(exc)}

    # --- Step 3: apply status/score updates to candidate games by ESPN event id. ---
    event_scores = {}
    for event in all_events:
        comps = event.get("competitions", []) or []
        if not comps:
            continue
        comp = comps[0]
        edata = event.get("date", "")
        try:
            event_date = parser.parse(edata)
        except (ValueError, TypeError):
            continue
        gid = event.get("id")
        if gid is None:
            continue
        try:
            gid = int(gid)
        except (ValueError, TypeError):
            continue
        if gid not in cand_ids:
            continue
        competitors = comp.get("competitors", [])
        home_raw = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away_raw = next((c for c in competitors if c.get("homeAway") == "away"), None)
        status_type = comp.get("status", {}).get("type", {}).get("name", "STATUS_SCHEDULED")
        stblock = comp.get("status", {})
        event_scores[gid] = {
            "status": _map_espn_status(status_type),
            "home_score": _safe_int(home_raw.get("score")) if home_raw else None,
            "away_score": _safe_int(away_raw.get("score")) if away_raw else None,
            "date": event_date,
            "quarter": _safe_int(stblock.get("period")) if stblock.get("period") is not None else None,
            "clock": stblock.get("displayClock") or None,
        }

    updated = 0
    for g in candidates:
        snap = event_scores.get(g.id)
        if not snap:
            continue
        changed = False
        if snap["status"] != g.status:
            g.status = snap["status"]
            changed = True
        # Only write scores for live/final games (ESPN returns them); keep scheduled
        # 0/None rows untouched until kickoff so pages don't show fake 0-0 live scores.
        if snap["status"] in (GameStatus.IN_PROGRESS, GameStatus.FINAL):
            if snap["home_score"] is not None and snap["home_score"] != g.home_score:
                g.home_score = snap["home_score"]
                changed = True
            if snap["away_score"] is not None and snap["away_score"] != g.away_score:
                g.away_score = snap["away_score"]
                changed = True
            # Quarter + game clock, only meaningful while in progress.
            if snap["status"] == GameStatus.IN_PROGRESS:
                if snap["quarter"] is not None and snap["quarter"] != g.quarter:
                    g.quarter = snap["quarter"]
                    changed = True
                if snap["clock"] and snap["clock"] != g.clock:
                    g.clock = snap["clock"]
                    changed = True
        if snap["date"] and snap["date"] != g.date:
            g.date = snap["date"]
            changed = True
        if changed:
            updated += 1

    await session.commit()

    # --- Step 4: sync live boxscore stats for IN_PROGRESS games ---
    # Team + player stats come from the per-game ESPN summary feed. We only do
    # this for games that are actually live (not scheduled/final), to keep the
    # ESPN request count minimal.
    live_games = [g for g, s in ((g, event_scores.get(g.id)) for g in candidates)
                  if s and s["status"] == GameStatus.IN_PROGRESS]
    live_games = [g for g in live_games if event_scores.get(g.id)]

    team_abbr_map = {}
    player_espn_map = {}
    boxscore_counts = {"players": 0, "teams": 0}

    if live_games:
        # Build team_id -> abbreviation map for the teams in these games.
        live_ids = set()
        for g in live_games:
            live_ids.add(g.home_team_id)
            live_ids.add(g.away_team_id)
        if live_ids:
            teams_res = await session.execute(
                select(Team.abbreviation, Team.id).where(Team.id.in_(live_ids))
            )
            team_abbr_map = {tid: abbr for abbr, tid in teams_res.all()}

        # Build espn_player_id -> player_id map via nfl.players.espn_id.
        from sqlalchemy import text as _text
        resp = await session.execute(_text(
            "SELECT id, espn_id FROM nfl.players WHERE espn_id IS NOT NULL"
        ))
        player_espn_map = {str(eid): pid for pid, eid in resp.all()}

        # One summary call per live game; each updates team + player stats.
        async with httpx.AsyncClient(timeout=30.0) as client:
            for g in live_games:
                try:
                    # Resolve the season YEAR (reader keys game_stats on year,
                    # not season_id).
                    sy_res = await session.execute(
                        select(Season.year).where(Season.id == g.season_id)
                    )
                    season_year = sy_res.scalar_one_or_none()
                    if not season_year:
                        continue
                    sresp = await client.get(
                        _ESPN_SUMMARY_URL, params={"event": g.id}
                    )
                    sresp.raise_for_status()
                    sdata = sresp.json()
                    counts = await _sync_live_boxscore(
                        session, g, sdata, team_abbr_map, player_espn_map, season_year
                    )
                    boxscore_counts["players"] += counts["players"]
                    boxscore_counts["teams"] += counts["teams"]
                except Exception:
                    # A single game's boxscore failure shouldn't abort the rest.
                    continue

    return {
        "checked_games": len(candidates),
        "updated_games": updated,
        "live_games": bool(live_games),
        "boxscore": boxscore_counts,
    }


# --- Live boxscore stats (team + player) from ESPN summary feed ---
#
# During a game we fetch ESPN's /summary feed per live game, which returns team
# totals (game_stats row) + per-player stats (player_weekly_stats rows). This
# mirrors what the /box-score endpoint renders, so in-progress games get real
# stats — not just a score.

_ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary"

# Safely parse a stat display value like "19/28" -> (19, 28) or "1-5" -> (1, 5)
def _parse_n_or_n(val, sep="/"):
    if not val:
        return (None, None)
    s = str(val)
    if sep in s:
        try:
            a, b = s.split(sep)
            return (int(a), int(b))
        except (ValueError, TypeError):
            return (None, None)
    try:
        return (int(s), 0)
    except (ValueError, TypeError):
        return (None, None)


def _stat(block, name):
    """Look up a single display value by stat name in a list of {name:..} dicts."""
    for s in block or []:
        if s.get("name") == name:
            return s.get("displayValue")
    return None


def _team_stats_field(team_stats_list, name):
    return _stat(team_stats_list, name)


def _time_to_seconds(val):
    """Convert '14:15' to seconds (855)."""
    if not val:
        return 0
    try:
        parts = str(val).split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        return 0
    return 0


def _build_game_stats_payload(g, game, team_abbr, opp_abbr, ts, season_year):
    """Map an ESPN team-statistics block to nfl.game_stats column values."""
    first_down = _safe_int(_team_stats_field(ts, "firstDowns"))
    third = _parse_n_or_n(_team_stats_field(ts, "thirdDownEff"), sep="-")
    fourth = _parse_n_or_n(_team_stats_field(ts, "fourthDownEff"), sep="-")
    pass_ca = _parse_n_or_n(_team_stats_field(ts, "completionAttempts"))
    sacks_yards = _parse_n_or_n(_team_stats_field(ts, "sacksYardsLost"), sep="-")
    penald = _parse_n_or_n(_team_stats_field(ts, "totalPenaltiesYards"), sep="-")
    redzone = _parse_n_or_n(_team_stats_field(ts, "redZoneAttempts"), sep="-")

    return {
        "season": season_year,
        "week": g.week,
        "season_type": g.game_type or "REG",
        "team_abbr": team_abbr,
        "opponent_abbr": opp_abbr,
        "total_yards": _safe_int(_team_stats_field(ts, "totalYards")),
        "pass_yards": _safe_int(_team_stats_field(ts, "netPassingYards")),
        "rush_yards": _safe_int(_team_stats_field(ts, "rushingYards")),
        "pass_attempts": pass_ca[1] if pass_ca[0] is not None else None,
        "pass_completions": pass_ca[0],
        "rush_attempts": _safe_int(_team_stats_field(ts, "rushingAttempts")),
        "yards_per_play": _float_or_none(_team_stats_field(ts, "yardsPerPlay")),
        "pass_tds": _safe_int(_team_stats_field(ts, "passingTouchdowns")),
        "rush_tds": _safe_int(_team_stats_field(ts, "rushingTouchdowns")),
        "interceptions_thrown": _safe_int(_team_stats_field(ts, "interceptions")),
        "sacks_suffered": sacks_yards[0] if sacks_yards[0] is not None else None,
        "sack_yards_lost": sacks_yards[1],
        "fumbles_lost": _safe_int(_team_stats_field(ts, "fumblesLost")),
        "penalties": penald[0],
        "penalty_yards": penald[1],
        "first_downs": first_down,
        "third_down_conversions": third[0],
        "third_down_attempts": third[1],
        "fourth_down_conversions": fourth[0],
        "fourth_down_attempts": fourth[1],
        "turnovers": _safe_int(_team_stats_field(ts, "turnovers")),
        "red_zone_trips": redzone[1] if redzone[0] is not None else None,
        "red_zone_tds": redzone[0],
    }


def _float_or_none(val):
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


async def _upsert_game_stats(session, payload):
    """Upsert nfl.game_stats so there is exactly ONE row per (season, week, team_abbr).

    The /box-score reader selects game_stats by (season, week, team_abbr) only and
    uses fetchone(), so duplicates for the same team+week cause it to pick a stale
    one. Legacy rows may have an empty opponent_abbr; we match on team_abbr alone
    (update first, insert only if nothing matched) to collapse onto them instead of
    creating a second row.
    """
    from sqlalchemy import text
    key_where = "season = :season AND week = :week AND team_abbr = :team_abbr"
    upd_cols = [c for c in payload.keys() if c not in ("season", "week", "team_abbr", "opponent_abbr", "season_type")]
    up_sets = ", ".join(f"{c}=:{c}" for c in upd_cols)

    update_sql = f"UPDATE nfl.game_stats SET {up_sets}, opponent_abbr=:opponent_abbr WHERE {key_where}"
    res = await session.execute(text(update_sql), payload)
    if res.rowcount == 0:
        cols = list(payload.keys())
        col_str = ", ".join(cols)
        val_str = ", ".join(f":{c}" for c in cols)
        insert_sql = f"INSERT INTO nfl.game_stats ({col_str}) VALUES ({val_str})"
        await session.execute(text(insert_sql), payload)


def _player_stat_value(stats, index):
    """Return the display value at a given index from a player's stats array."""
    try:
        return stats[index] if 0 <= index < len(stats) else None
    except (TypeError, IndexError):
        return None


async def _sync_live_boxscore(session, g, game, team_abbr_map, player_espn_map, season_year):
    """Fetch ESPN summary for an in-progress game and upsert live team + player stats.

    g          : the Game ORM object (has .home_team_id/.away_team_id/.week/.season_id)
    game       : the fetched ESPN summary JSON
    team_abbr_map: {team_id: abbreviation}
    player_espn_map: {espn_player_id_str: player_id}
    Returns dict of counts for diagnostics.
    """
    from sqlalchemy import text
    bs = game.get("boxscore") or {}
    teams = bs.get("teams") or []
    players_blocks = bs.get("players") or []

    counts = {"players": 0, "teams": 0}
    if not teams:
        return counts

    # 1) Team stats -> game_stats
    for t in teams:
        team_obj = t.get("team") or {}
        abbr = team_obj.get("abbreviation") or team_obj.get("abbreviation")
        if not abbr:
            continue
        # find which DB team this is (home or away)
        is_home = t.get("homeAway") == "home"
        team_id = g.home_team_id if is_home else g.away_team_id
        opp_abbr = team_abbr_map.get(g.away_team_id if is_home else g.home_team_id, "")
        ts = t.get("statistics")
        payload = _build_game_stats_payload(g, game, abbr, opp_abbr, ts, season_year)
        await _upsert_game_stats(session, payload)
        counts["teams"] += 1

    # 2) Player stats -> player_weekly_stats
    # players_blocks is a list: one element per team, each with keyed category stats.
    for team_block in players_blocks:
        stats_by_cat = team_block.get("statistics") if isinstance(team_block, dict) else None
        if not stats_by_cat or not isinstance(stats_by_cat, list):
            continue
        # Determine which team this block belongs to. The player blocks do not
        # reliably include homeAway, so resolve the team by abbreviation against
        # the team_abbr_map ({team_id: abbreviation}).
        team_ref = team_block.get("team") or {}
        team_abbr = team_ref.get("abbreviation")
        team_id = next((tid for tid, abbr in team_abbr_map.items() if abbr == team_abbr), None) if team_abbr else None
        if not team_id:
            continue
        opp_id = g.away_team_id if team_id == g.home_team_id else g.home_team_id
        season_id = g.season_id

        for block in stats_by_cat:
            cat = block.get("name")
            labels = block.get("labels") or []
            athletes = block.get("athletes") or []
            for a in athletes:
                athlete = a.get("athlete") or {}
                # athlete may be a dict {id, displayName} OR a list of href-refs
                espn_id = None
                if isinstance(athlete, dict):
                    espn_id = athlete.get("id")
                elif isinstance(athlete, list):
                    for ref in athlete:
                        href = ref.get("$ref", "") if isinstance(ref, dict) else ""
                        m = None
                        import re
                        m = re.search(r"/athletes/(\d+)", href)
                        if m:
                            espn_id = m.group(1)
                            break
                if not espn_id:
                    continue
                player_id = player_espn_map.get(str(espn_id))
                if not player_id:
                    continue
                sarr = a.get("stats") or []
                # Build a small payload for whichever category columns exist
                row = {
                    "player_id": player_id,
                    "game_id": g.id,
                    "season_id": season_id,
                    "week": g.week,
                    "team_id": team_id,
                    "opponent_id": opp_id,
                }
                labels_l = {k.lower(): i for i, k in enumerate(labels)}

                def gv(key):
                    i = labels_l.get(key.lower())
                    return _player_stat_value(sarr, i) if i is not None else None

                # passing
                if cat == "passing":
                    if "c/att" in labels_l:
                        ca = _parse_n_or_n(gv("c/att"))
                        row["pass_attempts"] = ca[1] if ca[0] is not None else None
                        row["pass_completions"] = ca[0]
                    row["pass_yards"] = _safe_int(gv("yds"))
                    row["pass_tds"] = _safe_int(gv("td"))
                    row["pass_int"] = _safe_int(gv("int"))
                elif cat == "rushing":
                    row["rush_attempts"] = _safe_int(gv("car"))
                    row["rush_yards"] = _safe_int(gv("yds"))
                    row["rush_tds"] = _safe_int(gv("td"))
                    row["rush_long"] = _safe_int(gv("long"))
                elif cat == "receiving":
                    row["targets"] = _safe_int(gv("tgts"))
                    row["receptions"] = _safe_int(gv("rec"))
                    row["receiving_yards"] = _safe_int(gv("yds"))
                    row["receiving_tds"] = _safe_int(gv("td"))
                    row["receiving_long"] = _safe_int(gv("long"))
                elif cat == "defensive":
                    row["sacks"] = _safe_int(gv("sacks"))
                    row["interceptions"] = _safe_int(gv("int"))
                    row["fumbles_recovered"] = _safe_int(gv("ff"))
                    row["defensive_tds"] = _safe_int(gv("td"))
                elif cat == "kicking":
                    fg = _parse_n_or_n(gv("fg"))
                    xp = _parse_n_or_n(gv("xp"))
                    row["field_goals_made"] = fg[0]
                    row["field_goals_attempted"] = fg[1]
                    row["extra_points_made"] = xp[0]
                    row["extra_points_attempted"] = xp[1]
                elif cat == "interceptions":
                    row["interceptions"] = _safe_int(gv("int"))
                elif cat == "fumbles":
                    row["fumbles_recovered"] = _safe_int(gv("rec"))
                else:
                    # punting, kickReturns, puntReturns and any other category have
                    # no targeted columns in player_weekly_stats — skip them.
                    continue

                # Only write non-empty rows (at least one category field set)
                if any(v is not None for v in row.values()):
                    await _upsert_player_stats(session, row)
                    counts["players"] += 1

    await session.commit()
    return counts


async def _upsert_player_stats(session, payload):
    from sqlalchemy import text
    cols = list(payload.keys())
    col_str = ", ".join(cols)
    val_str = ", ".join(f":{c}" for c in cols)
    # upsert on unique (player_id, game_id); update football stat fields only
    updates = ", ".join(f'{c}=EXCLUDED.{c}' for c in cols if c not in ("player_id", "game_id", "season_id", "team_id", "opponent_id"))
    sql = f"""
        INSERT INTO nfl.player_weekly_stats ({col_str})
        VALUES ({val_str})
        ON CONFLICT (player_id, game_id)
        DO UPDATE SET {updates}
    """
    await session.execute(text(sql), payload)
