"""
Backfill NFL time of possession into `nfl.game_stats` from ESPN core team stats.

The nflverse `stats_team` feed (source of `nfl.game_stats`) does NOT carry time
of possession. ESPN's core team-statistics endpoint exposes it per team as
`possessionTimeSeconds` (integer seconds) + `possessionTime` ("MM:SS" string).
This module fills `time_of_possession_secs` /
`possession_time_of_possession_string` / `possession_data_source='espn'` for
every FINAL game that has a prediction row (nfl.game_stats) but no TOP yet.

`nfl.games.id` IS the ESPN event id, so we can hit the core API directly:

    sports.core.api.espn.com/v2/sports/football/leagues/nfl/events/{gid}/
        competitions/{gid}/competitors/{comp_id}/statistics
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
import httpx

from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger(__name__)

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
HEADERS = {"User-Agent": "Mozilla/5.0 (EarlKnowsBall)"}

# ESPN returns each team's CURRENT abbreviation, which differs from the historical
# abbreviation our nfl.teams stores (relocated/renamed franchises). Map ESPN abbr ->
# our DB abbr so possession can be matched for all eras.
ESPN_ABBR_ALIAS = {
    "WSH": "WAS",  # Washington Redskins -> Commanders
    "OAK": "LV",   # Oakland -> Las Vegas Raiders
    "SD": "LAC",   # San Diego -> LA Chargers
    "STL": "LAR",  # St. Louis -> LA Rams
}

# nfl.games rows (status FINAL) that still need time of possession. season_id is
# resolved to a calendar year via nfl.seasons (game_stats.season holds the year).
# nflverse week == nfl.games.week (verified: BUF@MIA 2024 game_week 2/9 == stats_week
# 2/9), so we match game_stats by (season=year, week, team_abbr, opponent_abbr).
SELECT_GAMES_SQL = """
    SELECT g.id AS game_id, s.year AS season, g.week,
           ht.abbreviation AS home_abbr, at.abbreviation AS away_abbr
    FROM nfl.games g
    JOIN nfl.seasons s ON s.id = g.season_id
    JOIN nfl.teams ht ON ht.id = g.home_team_id
    JOIN nfl.teams at  ON at.id = g.away_team_id
    WHERE g.status = 'FINAL'
      AND g.home_score IS NOT NULL
      AND g.away_score IS NOT NULL
      AND s.year >= 2016
      AND (
        g.home_time_of_possession_secs IS NULL
        OR NOT EXISTS (
          SELECT 1 FROM nfl.game_stats gs_home
          WHERE gs_home.season = s.year AND gs_home.week = g.week
            AND gs_home.team_abbr = ht.abbreviation
            AND gs_home.opponent_abbr = at.abbreviation
            AND gs_home.time_of_possession_secs IS NOT NULL
        )
        OR NOT EXISTS (
          SELECT 1 FROM nfl.game_stats gs_away
          WHERE gs_away.season = s.year AND gs_away.week = g.week
            AND gs_away.team_abbr = at.abbreviation
            AND gs_away.opponent_abbr = ht.abbreviation
            AND gs_away.time_of_possession_secs IS NOT NULL
        )
      )
    ORDER BY g.id
"""

UPDATE_SQL = """
    UPDATE nfl.game_stats
    SET time_of_possession_secs = $1,
        possession_time_of_possession_string = $2,
        possession_data_source = 'espn',
        loaded_at = NOW()
    WHERE season = $3 AND week = $4 AND team_abbr = $5 AND opponent_abbr = $6
"""

UPDATE_GAMES_SQL = """
    UPDATE nfl.games
    SET home_time_of_possession_secs = $1,
        away_time_of_possession_secs = $2
    WHERE id = $3
"""


async def fetch_team_stats(client: httpx.AsyncClient, game_id: int, comp_id: int):
    url = (
        f"{CORE_BASE}/events/{game_id}/competitions/{game_id}"
        f"/competitors/{comp_id}/statistics"
    )
    resp = await client.get(url, timeout=15)
    resp.raise_for_status()
    d = resp.json()
    merged: dict[str, str] = {}
    for cat in d.get("splits", {}).get("categories", []):
        for s in cat.get("stats", []):
            name = s.get("name")
            if name:
                merged[name] = s.get("displayValue") or ""
    return merged


async def get_competitor_stats(client: httpx.AsyncClient, game_id: int) -> dict[str, dict]:
    """Return {team_abbr: {'possessionTimeSeconds': ..., 'possessionTime': ...}}."""
    comp_url = (
        f"{CORE_BASE}/events/{game_id}/competitions/{game_id}/competitors"
    )
    resp = await client.get(comp_url, timeout=15)
    resp.raise_for_status()
    comp_data = resp.json()

    result: dict[str, dict] = {}
    for item in comp_data.get("items", []):
        ref = item.get("$ref", "")
        if not ref:
            continue
        try:
            comp_resp = await client.get(ref, timeout=12)
            if comp_resp.status_code != 200:
                continue
            comp = comp_resp.json()
            comp_id = int(ref.split("/")[-1].split("?")[0])
            team_ref = comp.get("team", {}).get("$ref", "")
            if not team_ref:
                continue
            team_resp = await client.get(team_ref, timeout=12)
            if team_resp.status_code != 200:
                continue
            abbr = (team_resp.json().get("abbreviation") or "").strip().upper()
            if not abbr:
                continue
            stats = await fetch_team_stats(client, game_id, comp_id)
            result[abbr] = {
                "possessionTimeSeconds": stats.get("possessionTimeSeconds"),
                "possessionTime": stats.get("possessionTime"),
            }
        except Exception:  # noqa: BLE001
            continue
    return result


async def backfill_time_of_possession(max_games: int | None = None) -> dict:
    conn = await asyncpg.connect(PSYCOPG2_DATABASE_URL)
    updated = 0
    errors = 0
    fetched = 0
    async with httpx.AsyncClient(headers=HEADERS, timeout=20) as client:
        games = await conn.fetch(SELECT_GAMES_SQL)
        if max_games:
            games = games[:max_games]
        logger.info("FOUND %d FINAL games needing time-of-possession", len(games))

        # Batch: reuse the same events across a season where possible by
        # deduping game ids.
        for g in games:
            gid = g["game_id"]
            try:
                top = await get_competitor_stats(client, gid)
                fetched += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch failed game %s: %s", gid, exc)
                errors += 1
                continue

            # Update the two game_stats rows (home + away) and the nfl.games row.
            home_secs = None
            away_secs = None
            for abbr, val in top.items():
                db_abbr = ESPN_ABBR_ALIAS.get(abbr, abbr)  # normalize era relabels
                if db_abbr not in (g["home_abbr"], g["away_abbr"]):
                    continue
                secs_raw = val.get("possessionTimeSeconds")
                secs = None
                try:
                    secs = int(secs_raw) if secs_raw is not None else None
                except (TypeError, ValueError):
                    secs = None
                disp = val.get("possessionTime") or None

                # opponent is the OTHER team in this row
                opp = (g["away_abbr"] if db_abbr == g["home_abbr"]
                       else g["home_abbr"])
                if secs is not None or disp:
                    try:
                        res = await conn.execute(
                            UPDATE_SQL,
                            secs, disp, g["season"], g["week"], db_abbr, opp,
                        )
                        updated += res if isinstance(res, int) else 1
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("update failed %s %s: %s", gid, db_abbr, exc)
                        errors += 1

                if db_abbr == g["home_abbr"]:
                    home_secs = secs
                else:
                    away_secs = secs

            if home_secs is not None or away_secs is not None:
                try:
                    await conn.execute(
                        UPDATE_GAMES_SQL, home_secs, away_secs, gid,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("games update failed %s: %s", gid, exc)
            await asyncio.sleep(0.2)  # gentle rate limiting on the core API

    remaining = await conn.fetchval(
        "SELECT count(*) FROM nfl.game_stats WHERE time_of_possession_secs IS NULL"
    )
    await conn.close()
    return {
        "needed": len(games),
        "fetched": fetched,
        "updated_rows": updated,
        "errors": errors,
        "remaining_missing": int(remaining),
    }


async def _run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = await backfill_time_of_possession()
    print(result)


if __name__ == "__main__":
    asyncio.run(_run())
