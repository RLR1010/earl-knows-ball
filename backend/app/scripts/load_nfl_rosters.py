"""
NFL roster loader — populate nfl.players.espn_id and nfl.players.team_id.

Source: ESPN NFL team roster endpoints (site.api.espn.com), the same source the
NBA player loader uses. For each NFL team we fetch the active roster, then match
each player to nfl.players by normalized name and write back espn_id + team_id.

This fixes the 96% NULL nfl.players.team_id problem (the players table has no
team assignments). Running it once for current rosters resolves most active NFL
players; award/season props on nfl.player_season_props then get a team_id via
db._resolve_player_team_id.

Name normalization: lowercase, accent-fold (NFD), strip all non-alphanumerics,
drop Jr./Sr./II/III/IV/V suffix tokens.

Run:
    cd <repo>/backend && PYTHONPATH=$PWD <repo>/venv/bin/python \
        app/scripts/load_nfl_rosters.py
"""
import logging
import re
import sys
import time
import unicodedata

import requests
from sqlalchemy import create_engine, text

sys.path.insert(0, ".")
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("nfl_rosters")

# our nfl.teams abbreviation -> ESPN abbreviation (WAS vs WSH differs)
ESPN_TEAM_ABBR = {
    "ARI": "ARI", "ATL": "ATL", "BAL": "BAL", "BUF": "BUF", "CAR": "CAR",
    "CHI": "CHI", "CIN": "CIN", "CLE": "CLE", "DAL": "DAL", "DEN": "DEN",
    "DET": "DET", "GB": "GB", "HOU": "HOU", "IND": "IND", "JAX": "JAX",
    "KC": "KC", "LAC": "LAC", "LAR": "LAR", "LV": "LV", "MIA": "MIA",
    "MIN": "MIN", "NE": "NE", "NO": "NO", "NYG": "NYG", "NYJ": "NYJ",
    "PHI": "PHI", "PIT": "PIT", "SEA": "SEA", "SF": "SF", "TB": "TB",
    "TEN": "TEN", "WAS": "WSH",
}
# ESPN team ids (from site.api.espn.com football/nfl/teams)
ESPN_TEAM_IDS = {
    "ARI": 22, "ATL": 1, "BAL": 33, "BUF": 2, "CAR": 29, "CHI": 3, "CIN": 4,
    "CLE": 5, "DAL": 6, "DEN": 7, "DET": 8, "GB": 9, "HOU": 34, "IND": 11,
    "JAX": 30, "KC": 12, "LAC": 24, "LAR": 14, "LV": 13, "MIA": 15, "MIN": 16,
    "NE": 17, "NO": 18, "NYG": 19, "NYJ": 20, "PHI": 21, "PIT": 23, "SEA": 26,
    "SF": 25, "TB": 27, "TEN": 10, "WSH": 28,
}

ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{id}/roster"

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}
_HEADERS = {"Accept": "application/json", "User-Agent": "earl-roster-loader/1.0"}


def normalize_name(name) -> str:
    """Lowercased, accent-folded, punctuation-stripped string (no suffix removal)."""
    if not name:
        return ""
    n = unicodedata.normalize("NFD", str(name))
    n = "".join(c for c in n if not unicodedata.combining(c)).lower()
    return _NON_ALNUM.sub("", n)


def roster_name_key(display_name) -> str:
    """ESPN roster displayName -> normalized key with Jr./II/III suffix tokens dropped."""
    parts = str(display_name).split()
    parts = [p for p in parts if normalize_name(p) not in _SUFFIX]
    return normalize_name("".join(parts))


def fetch_roster(espn_team_id: int) -> list[dict]:
    url = ROSTER_URL.format(id=espn_team_id)
    resp = requests.get(url, headers=_HEADERS, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    out = []
    for group in data.get("athletes", []):
        for item in group.get("items", []):
            pos = (item.get("position") or {}).get("abbreviation") or ""
            out.append({
                "espn_id": item.get("id"),
                "name": item.get("displayName") or item.get("fullName") or "",
                "position": pos,
            })
    return out


def main() -> None:
    eng = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"),
                        pool_pre_ping=True)

    with eng.connect() as conn:
        teams = {r[0]: r[1] for r in conn.execute(
            text("SELECT abbreviation, id FROM nfl.teams")).fetchall()}
        player_rows = conn.execute(text(
            "SELECT id, name, espn_id, team_id FROM nfl.players")).fetchall()
    # normalized name -> (player_id, espn_id, team_id); prefer row w/ existing team
    players = {}
    for pid, name, espn_id, team_id in player_rows:
        key = normalize_name(name)
        if key not in players or (team_id is not None and players[key][2] is None):
            players[key] = (pid, espn_id, team_id)
    logger.info(f"Loaded {len(players)} normalized nfl.players entries; teams={len(teams)}")

    updated_team = 0
    updated_espn = 0
    matched = 0
    unmatched = []

    with eng.begin() as conn:
        for our_abbr, team_id in teams.items():
            espn_abbr = ESPN_TEAM_ABBR.get(our_abbr)
            espn_id = ESPN_TEAM_IDS.get(espn_abbr)
            if not espn_id:
                logger.warning(f"  {our_abbr}: no espn id; skip")
                continue
            try:
                roster = fetch_roster(espn_id)
            except Exception as e:
                logger.error(f"  {espn_abbr}: roster fetch failed: {e}")
                continue
            tm = 0
            inserted = 0
            for p in roster:
                entry = players.get(roster_name_key(p["name"]))
                if entry:
                    pid, cur_espn, cur_team = entry
                    matched += 1
                    tm += 1
                    if cur_team is None or cur_team != team_id:
                        conn.execute(text("UPDATE nfl.players SET team_id=:t WHERE id=:i"),
                                     {"t": team_id, "i": pid})
                        updated_team += 1
                    if not cur_espn or str(cur_espn) != str(p["espn_id"]):
                        conn.execute(text("UPDATE nfl.players SET espn_id=:e WHERE id=:i"),
                                     {"e": p["espn_id"], "i": pid})
                        updated_espn += 1
                else:
                    # missing player: insert a new row (name + espn_id + team_id + position)
                    ins = conn.execute(text(
                        "INSERT INTO nfl.players (name, position, espn_id, team_id, status) "
                        "VALUES (:n, :p, :e, :t, 'ACT')"
                        " ON CONFLICT (espn_id) DO UPDATE SET team_id = EXCLUDED.team_id "
                        " RETURNING id"),
                        {"n": p["name"], "p": p["position"] or "UNK",
                         "e": p["espn_id"], "t": team_id})
                    new_pid = ins.scalar()
                    players[roster_name_key(p["name"])] = (new_pid, p["espn_id"], team_id)
                    inserted += 1
                    matched += 1
                    tm += 1
            logger.info(f"  {espn_abbr}: resolved {tm}/{len(roster)} (inserted {inserted})")
            time.sleep(0.2)

    logger.info("=" * 60)
    logger.info(f"Roster load complete: {matched} roster players matched to nfl.players")
    logger.info(f"  team_id updated: {updated_team}")
    logger.info(f"  espn_id updated: {updated_espn}")
    logger.info(f"  unmatched: {len(unmatched)}")
    seen = set()
    for abbr, nm in unmatched[:25]:
        key = (abbr, nm)
        if key in seen:
            continue
        seen.add(key)
        logger.info(f"    no-match {abbr}: {nm}")


if __name__ == "__main__":
    main()
