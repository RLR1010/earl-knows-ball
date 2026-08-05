"""Ingest the full NFL player roster into nfl.players from nflverse players.csv.

The players table was only ever populated with skill positions (K/QB/RB/TE/WR)
from a fantasy source. This pulls the complete nflverse player universe
(all positions, 1990s-present), inserts missing players keyed by gsis_id
(nflverse_id), and backfills empty nflverse-derived fields without clobbering
existing sleeper_id/fantasy data.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/ingest_full_nfl_roster.py [--create | --backfill]
  --create   insert missing players (default ON unless --backfill only)
  --backfill  only fill empty nflverse fields on existing players
"""
import os, sys, asyncio
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL
from app.ingestion.nflverse_data import _download_csv, NFLVERSE_BASE

_sync_engine = sa.create_engine(SYNC_DATABASE_URL)

# map nflverse team abbrev -> nfl.teams.id
_TEAM = {
    "ARI": 12, "ATL": 13, "BAL": 14, "BUF": 15, "CAR": 18, "CHI": 4, "CIN": 5,
    "CLE": 19, "DAL": 2, "DEN": 20, "DET": 21, "GB": 22, "HOU": 23, "IND": 24,
    "JAX": 3, "KC": 25, "LAC": 27, "LAR": 26, "LV": 17, "MIA": 28, "MIN": 30,
    "NE": 31, "NO": 32, "NYG": 33, "NYJ": 34, "PHI": 7, "PIT": 35, "SF": 29,
    "SEA": 36, "TB": 1, "TEN": 37, "WAS": 38,
}
# map nflverse position codes that differ from our display positions
_POS = {
    "HB": "RB", "FB": "RB", "DB": "S", "CB": "CB", "SS": "S", "FS": "S",
    "NT": "DT", "DE": "DE", "DT": "DT", "OLB": "LB", "ILB": "LB", "MLB": "LB",
    "C": "C", "OG": "OG", "OT": "OT", "G": "OG", "T": "OT", "DL": "DL",
    "LB": "LB", "S": "S", "P": "P", "K": "K", "LS": "LS", "QB": "QB",
    "RB": "RB", "WR": "WR", "TE": "TE", "PK": "K", "PN": "P", "SAF": "S", "NT": "DT",
}


def load_team_map():
    with _sync_engine.connect() as c:
        r = c.execute(text("SELECT abbreviation, id FROM nfl.teams"))
        return {ab.upper(): i for ab, i in r}


def main():
    team_map = load_team_map()
    create = "--create" in sys.argv or "--create" not in sys.argv and "--backfill" not in sys.argv
    backfill_only = "--backfill" in sys.argv
    dry = "--dry" in sys.argv

    print("Downloading nflverse players.csv...", flush=True)
    rows = asyncio.run(_download_csv(f"{NFLVERSE_BASE}/players/players.csv"))
    print(f"Downloaded {len(rows)} player records", flush=True)

    with _sync_engine.connect() as c:
        r = c.execute(text("SELECT id, nflverse_id, name, position, team_id FROM nfl.players"))
        existing = {}
        for pid, gsid, name, pos, tid in r:
            if gsid:
                existing[gsid.strip()] = (pid, name, pos, tid)

    ins = 0
    upd = 0
    ins_pos = {}
    for row in rows:
        gsis = (row.get("gsis_id") or "").strip()
        if not gsis:
            continue
        name = (row.get("full_name") or "").strip() or ((row.get("first_name") or "") + " " + (row.get("last_name") or "")).strip()
        pos = (row.get("position") or "").strip().upper()
        pos = _POS.get(pos, pos)
        if not name or not pos:
            continue
        team_ab = (row.get("team") or "").strip().upper()
        team_id = team_map.get(team_ab) if team_ab else None

        jn = row.get("jersey_number") or row.get("jersey")
        ht = row.get("height") or row.get("height_inches")
        wt = row.get("weight")
        yrs = row.get("years_exp")
        draft_round = row.get("draft_round")
        draft_pick = row.get("draft_pick")
        draft_year = row.get("draft_year")
        draft_team = (row.get("draft_team") or "").strip().upper()

        if gsis in existing:
            # backfill empty nflverse fields
            pid, ename, epos, etid = existing[gsis]
            sets = []
            vals = {}
            if team_id and not etid:
                sets.append("team_id=:team_id"); vals["team_id"] = team_id
            if pos:
                sets.append("position=:pos"); vals["pos"] = pos
            if not ename:
                sets.append("name=:name"); vals["name"] = name
            if jn and jn != "":
                try: sets.append("jersey_number=COALESCE(jersey_number,:jn)"); vals["jn"] = int(jn)
                except (TypeError, ValueError): pass
            if ht:
                try: sets.append("height=COALESCE(height,:ht)"); vals["ht"] = int(ht)
                except (TypeError, ValueError): pass
            if wt:
                try: sets.append("weight=COALESCE(weight,:wt)"); vals["wt"] = int(wt)
                except (TypeError, ValueError): pass
            if yrs:
                try: sets.append("years_exp=COALESCE(years_exp,:yrs)"); vals["yrs"] = int(yrs)
                except (TypeError, ValueError): pass
            if draft_year:
                try: sets.append("draft_year=COALESCE(draft_year,:dy)"); vals["dy"] = int(draft_year)
                except (TypeError, ValueError): pass
            if draft_round:
                try: sets.append("draft_round=COALESCE(draft_round,:dr)"); vals["dr"] = int(draft_round)
                except (TypeError, ValueError): pass
            if draft_pick:
                try: sets.append("draft_pick=COALESCE(draft_pick,:dp)"); vals["dp"] = int(draft_pick)
                except (TypeError, ValueError): pass
            if draft_team:
                sets.append("draft_team=COALESCE(draft_team,:dt)"); vals["dt"] = draft_team
            if sets:
                vals["pid"] = pid
                sql = f"UPDATE nfl.players SET {', '.join(sets)} WHERE id=:pid"
                if not dry:
                    with _sync_engine.begin() as c:
                        c.execute(text(sql), vals)
                upd += 1
        elif create:
            cn = row.get("college")
            bd = row.get("birth_date")
            hs = row.get("headshot_url")
            ins_pos[pos] = ins_pos.get(pos, 0) + 1
            if not dry:
                with _sync_engine.begin() as c:
                    c.execute(text(
                        "INSERT INTO nfl.players (nflverse_id, name, position, team_id, jersey_number, "
                        "height, weight, years_exp, college, birth_date, headshot_url, draft_year, "
                        "draft_round, draft_pick, draft_team) "
                        "VALUES (:g, :name, :pos, :tid, :jn, :ht, :wt, :yrs, :cn, :bd, :hs, :dy, :dr, :dp, :dt)"
                    ), {
                        "g": gsis, "name": name, "pos": pos, "tid": team_id,
                        "jn": _safe_int(jn), "ht": _safe_int(ht), "wt": _safe_int(wt),
                        "yrs": _safe_int(yrs), "cn": cn or None, "bd": bd or None,
                        "hs": hs or None, "dy": _safe_int(draft_year),
                        "dr": _safe_int(draft_round), "dp": _safe_int(draft_pick),
                        "dt": draft_team or None,
                    })
            ins += 1

    mode = "DRY-RUN " if dry else ""
    print(f"{mode}Inserted {ins} new players; backfilled {upd} existing.")
    if dry and ins_pos:
        print("  by position:")
        for p, n in sorted(ins_pos.items()):
            print(f"    {p}: {n}")


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    main()
