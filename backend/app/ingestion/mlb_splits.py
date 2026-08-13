"""Per-player MLB split stat ingestion.

Populates ``mlb.player_splits`` for the MLB Stats API-driven splits and
derived city splits. These back Earl's chat research (``get_player_split_stats``)
and the premium Prop Bets writeup.

Split types stored (``split_type`` string, see model docstring):
  - vs_lhp / vs_rhp   batter vs left/right-handed pitcher  (API sitCodes vl/vr)
  - home / away       game location                        (API sitCodes h/a)
  - day / night       game time                            (API sitCodes d/n)
  - grass / turf      field surface                        (API sitCodes g/t)
  - city_<slug>       derived from game home-team city (via batting_game_stats)

``season_id IS NULL`` rows are the CAREER aggregates; set rows are that season.
Current-tenure season is loaded fresh; career is also (re)loaded since it is
the dominant prop-bet research view.

The API-driven splits are fetched via ``/people/{id}/stats?stats=statSplits``
with ``group=hitting`` and the relevant ``sitCodes``. Verified live on the
public MLB Stats API (2026-08-12).
"""

import logging
from collections import defaultdict
from typing import Optional

import httpx
from sqlalchemy import text, select, delete

from app.database import async_session
from app.models.mlb.player import MLBPlayer
from app.models.mlb.player_split import MLBPlayerSplit
from app.models.mlb.team_split import MLBTeamSplit
from app.models.mlb.season import MLBSeason
from app.ingestion.mlb_stats import MLB_API_BASE, _safe_float, _safe_int

logger = logging.getLogger("earl.mlb_splits")

# Bat-side context is available on players but we only store hitter split stats here.
BATCH_SIZE = 10

# "sitCodes" -> split_type / label mapping for API-driven splits.
API_SPLIT_TYPES = [
    # (sitCodes, split_type, label)
    ("vl", "vs_lhp", "vs LHP"),
    ("vr", "vs_rhp", "vs RHP"),
    ("h", "home", "Home"),
    ("a", "away", "Away"),
    ("d", "day", "Day"),
    ("n", "night", "Night"),
    ("g", "grass", "Grass"),
    ("t", "turf", "Turf"),
]

# Home city for each MLB team (abbreviation -> normalized city slug + label).
# City splits are derived from the home team at the game venue. Stable data.
# Per-team home-venue descriptor: abbreviation -> (unique_slug, human_label).
# Each MLB team owns exactly one home park, so a unique per-team slug keeps
# shared-city rivals distinct (CHC vs CWS, NYY vs NYM, LAA vs LAD, SF vs OAK).
TEAM_HOME: dict[str, tuple[str, str]] = {
    "ARI": ("arizona", "Chase Field (ARI)"), "ATL": ("atlanta", "Truist Park (ATL)"),
    "BAL": ("baltimore", "Oriole Park (BAL)"), "BOS": ("boston", "Fenway Park (BOS)"),
    "CHC": ("cubs", "Wrigley Field (CHC)"), "CWS": ("whitesox", "Rate Field (CWS)"),
    "CIN": ("cincinnati", "Great American (CIN)"), "CLE": ("cleveland", "Progressive Field (CLE)"),
    "COL": ("rockies", "Coors Field (COL)"), "DET": ("detroit", "Comerica Park (DET)"),
    "HOU": ("houston", "Daikin Park (HOU)"), "KC": ("royals", "Kauffman (KC)"),
    "LAA": ("angels", "Angel Stadium (LAA)"), "LAD": ("dodgers", "Dodger Stadium (LAD)"),
    "MIA": ("marlins", "loanDepot Park (MIA)"), "MIL": ("brewers", "AmFam Field (MIL)"),
    "MIN": ("twins", "Target Field (MIN)"), "NYM": ("mets", "Citi Field (NYM)"),
    "NYY": ("yankees", "Yankee Stadium (NYY)"), "OAK": ("athletics", "Coliseum (ATH)"),
    "PHI": ("phillies", "Citizens Bank (PHI)"), "PIT": ("pirates", "PNC Park (PIT)"),
    "SD": ("padres", "Petco Park (SD)"), "SF": ("giants", "Oracle Park (SF)"),
    "SEA": ("mariners", "T-Mobile Park (SEA)"), "STL": ("cardinals", "Busch Stadium (STL)"),
    "TB": ("rays", "Tropicana (TB)"), "TEX": ("rangers", "Globe Life (TEX)"),
    "TOR": ("bluejays", "Rogers Centre (TOR)"), "WSH": ("nationals", "Nationals Park (WSH)"),
}


async def _api_get(url: str, params: dict = None) -> Optional[dict]:
    """Async GET to the MLB Stats API (matches mlb_stats style)."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"API error: {url} - {e}")
            return None


async def _resolve_player(db, mlb_id: int) -> Optional[int]:
    """Return the local MLBPlayer.id for an MLB Stats API person id, or None."""
    r = await db.execute(select(MLBPlayer).where(MLBPlayer.mlb_id == mlb_id))
    p = r.scalar_one_or_none()
    return p.id if p else None


async def load_api_splits(db, year: int, season_id: int, mlb_player_ids: list[int]):
    """Fetch season + career split stats from the MLB Stats API and upsert.

    For each player, hit ``/people/{id}/stats?stats=statSplits&group=hitting``
    once per player: one request fetches all 8 split types via comma-separated
    ``sitCodes``. Two requests per player (season + career).
    """
    count = 0
    all_sitcodes = ",".join(sitcodes for sitcodes, _, _ in API_SPLIT_TYPES)
    for i in range(0, len(mlb_player_ids), BATCH_SIZE):
        batch = mlb_player_ids[i:i + BATCH_SIZE]
        for pid in batch:
            db_player_id = await _resolve_player(db, pid)
            if not db_player_id:
                continue
            # Season splits
            await _fetch_and_store(db, pid, db_player_id, year, season_id, all_sitcodes, season=True)
            # Career splits (season_id NULL)
            await _fetch_and_store(db, pid, db_player_id, year, season_id, all_sitcodes, season=False)
            count += 1
        if (i // BATCH_SIZE) % 10 == 0 and i > 0:
            await db.commit()
            logger.info(f"  Splits progress: {i}/{len(mlb_player_ids)} players")
    await db.commit()
    logger.info(f"Splits fetched for {count} players (season + career).")
    return count


# split code (from sitCodes) -> (split_type, label)
_SITCODE_MAP = {sitcodes: (split_type, label) for sitcodes, split_type, label in API_SPLIT_TYPES}


async def _fetch_and_store(
    db, mlb_id: int, db_player_id: int, year: int, season_id: int,
    all_sitcodes: str, season: bool,
):
    params = {"stats": "statSplits", "group": "hitting", "gameType": "R", "sitCodes": all_sitcodes}
    if season:
        params["season"] = str(year)
    url = f"{MLB_API_BASE}/people/{mlb_id}/stats"
    data = await _api_get(url, params)
    if not data:
        return
    for se in data.get("stats", []):
        if se.get("group", {}).get("displayName", "").lower() != "hitting":
            continue
        for sp in se.get("splits", []):
            code = sp.get("split", {}).get("code")
            mapped = _SITCODE_MAP.get(code)
            if not mapped:
                continue
            split_type, label = mapped
            stat = sp.get("stat", {})
            if not stat.get("gamesPlayed"):
                continue
            target_season = season_id if season else None
            await _upsert_split_row(db, db_player_id, target_season, split_type, label, stat)


async def _upsert_split_row(db, player_id: int, season_id, split_type: str, label: str, stat: dict):
    r = await db.execute(
        select(MLBPlayerSplit).where(
            MLBPlayerSplit.player_id == player_id,
            MLBPlayerSplit.split_type == split_type,
            MLBPlayerSplit.season_id == season_id,
        )
    )
    existing = r.scalar_one_or_none()
    row = existing or MLBPlayerSplit(player_id=player_id, split_type=split_type, season_id=season_id)
    row.split_label = label

    row.games_played = _safe_int(stat.get("gamesPlayed"))
    row.plate_appearances = _safe_int(stat.get("plateAppearances"))
    row.at_bats = _safe_int(stat.get("atBats"))
    row.runs = _safe_int(stat.get("runs"))
    row.hits = _safe_int(stat.get("hits"))
    row.doubles = _safe_int(stat.get("doubles"))
    row.triples = _safe_int(stat.get("triples"))
    row.home_runs = _safe_int(stat.get("homeRuns"))
    row.runs_batted_in = _safe_int(stat.get("rbi"))
    row.base_on_balls = _safe_int(stat.get("baseOnBalls"))
    row.strikeouts = _safe_int(stat.get("strikeOuts"))
    row.hit_by_pitch = _safe_int(stat.get("hitByPitch"))
    row.sacrifice_flies = _safe_int(stat.get("sacFlies"))
    row.babip = _safe_float(stat.get("babip"))
    row.total_bases = _safe_int(stat.get("totalBases"))

    row.avg = _safe_float(stat.get("avg"))
    row.obp = _safe_float(stat.get("obp"))
    row.slg = _safe_float(stat.get("slg"))
    row.ops = _safe_float(stat.get("ops"))
    row.woba = _safe_float(stat.get("woba"))
    # iso derived = slg - avg
    if row.slg is not None and row.avg is not None:
        row.iso = round(row.slg - row.avg, 3)

    if not existing:
        db.add(row)


# ── City splits (derived from game log) ──────────────────────────────

async def load_city_splits(db, year: Optional[int] = None):
    """Compute player home-venue splits from the stored game log.

    "City" is derived from the game's home team, so each team's home park is a
    distinct split — keep shared-city rivals apart (Cubs vs White Sox, Yankees
    vs Mets, Angels vs Dodgers). Aggregates ``batting_game_stats`` joined to
    ``games`` -> home team -> per-team slug via TEAM_HOME, per player + home
    venue for a season (if ``year``) and career (has the whole table).

    Uses raw SQL for the aggregate (matches how the rest of the app reads the
    game-log tables) and upserts ``mlb.player_splits`` city_<slug> rows.
    NOTE: run after batting_game_stats/games are ingested.
    """
    # Resolve team abbreviation -> local team id -> per-team home descriptor
    team_rows = await db.execute(text(
        "SELECT id, abbreviation FROM mlb.teams"
    ))
    id_to_abbr = {tid: abbr for tid, abbr in team_rows.fetchall()}
    # slug -> readable label (e.g. 'cubs' -> 'Wrigley Field (CHC)')
    slug_to_label = {slug: label for slug, label in TEAM_HOME.values()}

    def city_of(home_team_id):
        abbr = id_to_abbr.get(home_team_id)
        if not abbr or abbr not in TEAM_HOME:
            return None
        return TEAM_HOME[abbr]

    # Aggregate batting_game_stats joined to games (season) + teams (home team)
    # Grouped by player_id, home-team city, season_id.
    agg = defaultdict(lambda: {
        "pa": 0, "ab": 0, "hits": 0, "2b": 0, "3b": 0,
        "hr": 0, "rbi": 0, "bb": 0, "k": 0, "hbp": 0, "sf": 0, "tb": 0,
        "gp_games": set(),
    })

    rows = await db.execute(text(
        """
        SELECT b.player_id, b.game_id, g.home_team_id, g.season_id,
               b.plate_appearances, b.at_bats, b.hits, b.doubles, b.triples,
               b.home_runs, b.runs_batted_in, b.base_on_balls, b.strikeouts,
               b.hit_by_pitch, b.sacrifice_flies
        FROM mlb.batting_game_stats b
        JOIN mlb.games g ON g.id = b.game_id
        """
    ))

    for r in rows.fetchall():
        (pid, gid, home_team_id, gseason_id,
         pa, ab, hits, dbl, tri, hr, rbi, bb, k, hbp, sf) = r
        if pid is None:
            continue
        city = city_of(home_team_id)
        if city is None:
            continue
        city_slug, city_label = city
        # Always accumulate career scope (None); accumulate season scope only when
        # it matches the requested year (if a year filter is given).
        scopes = [None]
        if year is None or gseason_id == year:
            scopes.append(gseason_id)
        for sc in scopes:
            key = (pid, city_slug, sc)
            c = agg[key]
            c["gp_games"].add(gid)
            c["pa"] += (pa or 0)
            c["ab"] += (ab or 0)
            c["hits"] += (hits or 0)
            c["2b"] += (dbl or 0)
            c["3b"] += (tri or 0)
            c["hr"] += (hr or 0)
            c["rbi"] += (rbi or 0)
            c["bb"] += (bb or 0)
            c["k"] += (k or 0)
            c["hbp"] += (hbp or 0)
            c["sf"] += (sf or 0)
            c["tb"] += (hits or 0) + (dbl or 0) + 2 * (tri or 0) + 3 * (hr or 0)

    # Delete all existing city_* rows then reinsert (stays in sync with game log).
    await db.execute(delete(MLBPlayerSplit).where(MLBPlayerSplit.split_type.like("city_%")))

    inserted = 0
    for (pid, city_slug, sc), c in agg.items():
        if not c["ab"] and not c["pa"]:
            continue
        split_type = f"city_{city_slug}"
        season_id = sc if sc is not None else None
        row = MLBPlayerSplit(player_id=pid, split_type=split_type, season_id=season_id)
        row.split_label = slug_to_label.get(city_slug, f"Home: {city_slug}")
        row.city = city_slug
        row.games_played = len(c["gp_games"])
        row.plate_appearances = c["pa"]
        row.at_bats = c["ab"]
        row.hits = c["hits"]
        row.doubles = c["2b"]
        row.triples = c["3b"]
        row.home_runs = c["hr"]
        row.runs_batted_in = c["rbi"]
        row.base_on_balls = c["bb"]
        row.strikeouts = c["k"]
        row.hit_by_pitch = c["hbp"]
        row.sacrifice_flies = c["sf"]
        row.total_bases = c["tb"]
        if c["ab"]:
            row.avg = round(c["hits"] / c["ab"], 3)
            row.slg = round(c["tb"] / c["ab"], 3)
        if c["pa"]:
            obp_num = c["hits"] + c["bb"] + c["hbp"]
            obp_den = c["ab"] + c["bb"] + c["hbp"] + c["sf"]
            if obp_den:
                row.obp = round(obp_num / obp_den, 3)
        if row.avg is not None and row.slg is not None:
            row.iso = round(row.slg - row.avg, 3)
        if row.obp is not None and row.slg is not None:
            row.ops = round(row.obp + row.slg, 3)
        db.add(row)
        inserted += 1

    await db.commit()
    logger.info(f"City splits upserted for {inserted} (player, city, scope) keys.")
    return inserted



# ── Team L/R splits (API) ──────────────────────────────────────────
# Team hitting vs LHP/RHP lives in mlb.team_splits (split_type 'vs_lhp'/'vs_rhp').
# Unlike the situational splits (home/away/day/night/surface) it CANNOT be derived
# from batting_game_stats (no pitcher handedness there), so it comes straight from
# the MLB Stats API like the player splits.

async def _upsert_team_split_row(db, team_id: int, season_id: int, split_type: str, stat: dict):
    res = await db.execute(
        select(MLBTeamSplit).where(
            MLBTeamSplit.team_id == team_id,
            MLBTeamSplit.season_id == season_id,
            MLBTeamSplit.split_type == split_type,
        )
    )
    existing = res.scalar_one_or_none()
    row = existing or MLBTeamSplit(
        team_id=team_id, season_id=season_id, split_type=split_type
    )
    # The team hitting L/R endpoint (group=hitting, sitCodes=vl/vr) returns
    # gamesPlayed/homeRuns/avg/obp/slg/ops but DOES NOT return runs, wins, or
    # losses for the L/R splits. Those columns are NOT NULL in mlb.team_splits,
    # so calling _safe_int() on a missing key yields None and crashes the
    # insert/update with an IntegrityError. Coerce missing -> 0 (meaningless for
    # a hitting-only split, but keeps the row valid; the same 0s are what the
    # game-log-derived rows leave for these columns too).
    row.games = _safe_int(stat.get("gamesPlayed")) or 0
    row.runs_scored = _safe_int(stat.get("runs")) or 0
    row.wins = _safe_int(stat.get("wins")) or 0
    row.losses = _safe_int(stat.get("losses")) or 0
    row.home_runs = _safe_int(stat.get("homeRuns")) or 0
    row.avg = _safe_float(stat.get("avg"))
    row.obp = _safe_float(stat.get("obp"))
    row.slg = _safe_float(stat.get("slg"))
    row.ops = _safe_float(stat.get("ops"))
    # team L/R ERA from pitching group is a separate call; leave era/whip for
    # the existing compute_team_splits.py which already fills those from game log.
    if not existing:
        db.add(row)


async def load_team_lr_splits(db, year: int, season_id: int) -> int:
    """Fetch team hitting splits vs LHP/RHP from the MLB Stats API and upsert
    into ``mlb.team_splits`` (split_type 'vs_lhp'/'vs_rhp').

    Uses ``/teams/{id}/stats?group=hitting&sitCodes=vl,vr``. Returns # teams.
    """
    team_abbr_to_local = {}
    team_rows = await db.execute(text("SELECT id, abbreviation FROM mlb.teams"))
    for tid, abbr in team_rows.fetchall():
        team_abbr_to_local[abbr.upper()] = tid

    count = 0
    for api_team_id, abbr, name, league, division in MLB_TEAMS_FOR_SPLITS():
        local_id = team_abbr_to_local.get(abbr.upper())
        data = await _api_get(
            f"{MLB_API_BASE}/teams/{api_team_id}/stats",
            {"stats": "statSplits", "group": "hitting", "sitCodes": "vl,vr", "season": str(year), "gameType": "R"},
        )
        if not data:
            continue
        for se in data.get("stats", []):
            for sp in se.get("splits", []):
                code = sp.get("split", {}).get("code")
                if code == "vl":
                    await _upsert_team_split_row(db, local_id, season_id, "vs_lhp", sp.get("stat", {}))
                elif code == "vr":
                    await _upsert_team_split_row(db, local_id, season_id, "vs_rhp", sp.get("stat", {}))
        count += 1
    await db.commit()
    logger.info(f"Team L/R splits fetched for {count} teams.")
    return count


# ── Top-level entry ──────────────────────────────────────────────────

async def refresh_all(db, year: int):
    """Run the full split refresh for a season: API splits + city splits.

    Returns dict of counts for logging.
    """
    # ensure season id
    season_row = (await db.execute(select(MLBSeason).where(MLBSeason.year == year))).scalar_one_or_none()
    if not season_row:
        logger.warning(f"No MLB season row for {year}; skipping split season stats.")
        season_id = None
    else:
        season_id = season_row.id

    # Gather all non-pitcher MLB player ids from the API (active hitters)
    # Reuse team rosters to build the hitter id list, matching mlb_stats approach.
    api_hitters: list[int] = []
    seen = set()
    for api_team_id, abbr, name, league, division in MLB_TEAMS_FOR_SPLITS():
        roster_data = await _api_get(f"{MLB_API_BASE}/teams/{api_team_id}/roster", {"season": year})
        if not roster_data:
            continue
        for entry in roster_data.get("roster", []):
            pid = entry.get("person", {}).get("id")
            pos_abbr = entry.get("position", {}).get("abbreviation", "")
            if pid and pos_abbr != "P" and pid not in seen:
                seen.add(pid)
                api_hitters.append(pid)

    api_count = await load_api_splits(db, year, season_id, api_hitters)
    city_count = await load_city_splits(db, year)
    team_lr_count = await load_team_lr_splits(db, year, season_id)
    return {"api_splits": api_count, "city_keys": city_count, "team_lr": team_lr_count, "hitters": len(api_hitters)}


def MLB_TEAMS_FOR_SPLITS():
    """Import MLB_TEAMS lazily to avoid heavy import at module load."""
    from app.ingestion.mlb_stats import MLB_TEAMS
    return MLB_TEAMS


async def run_once(year: int):
    """Idempotent entry for the scheduler: open a session and refresh."""
    async with async_session() as db:
        return await refresh_all(db, year)
