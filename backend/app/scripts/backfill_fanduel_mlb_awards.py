"""
Backfill mlb.player_season_props fanduel archive rows:
  - resolve team_id where NULL (clean names via players, managers via abbrev,
    parlays via first award token)
  - derive AL/NL from team league and rewrite generic prop_type
    (mvp -> mvp_al/mvp_nl, cy_young -> cy_young_al/nl, rookie_of_year -> rookie_al/nl)

Non-destructive: only fills NULLs and rewrites generic prop_types.
Idempotent: safe to re-run.
"""
import re
import sys

from sqlalchemy import create_engine, text

sys.path.insert(0, ".")
from app.core.config import settings
from app.scrapers.db import _resolve_player_team_id

# FanDuel abbrev -> our mlb.teams.abbreviation
ABBREV_FIX = {
    "KCR": "KC", "CHW": "CWS", "ATH": "OAK", "WSN": "WSH",
    "SDP": "SD", "SFG": "SF", "TBR": "TB",
}

# generic prop_type -> per-league template
PROP_MAP = {
    "mvp": "mvp_{league}",
    "cy_young": "cy_young_{league}",
    "rookie_of_year": "rookie_{league}",
    "rookie": "rookie_{league}",
}


def main():
    eng = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"), pool_pre_ping=True)

    with eng.begin() as conn:
        league_map = {r[0]: r[1] for r in conn.execute(text(
            "SELECT abbreviation, league FROM mlb.teams"))}
        rows = conn.execute(text(
            "SELECT id, player_name, prop_type, team_id FROM mlb.player_season_props "
            "WHERE bookmaker='fanduel'")).fetchall()

        changed = 0
        for rid, pname, ptype, cur_tid in rows:
            tid = cur_tid
            league = None

            # 1) resolve team_id if missing
            if tid is None:
                m = re.search(r"\(([A-Z]{2,4})\)", pname)
                if m:
                    ab = ABBREV_FIX.get(m.group(1), m.group(1))
                    tid = conn.execute(text(
                        "SELECT id FROM mlb.teams WHERE abbreviation=:a"), {"a": ab}).scalar()
                    if tid:
                        league = league_map.get(ab)
                if tid is None:
                    first = pname.split(" to win ")[0].strip() if " to win " in pname else pname
                    tid = _resolve_player_team_id(conn, "mlb", first)
                    if tid:
                        trow = conn.execute(text(
                            "SELECT league FROM mlb.teams WHERE id=:i"), {"i": tid}).first()
                        if trow:
                            league = trow[0]

            # 2) if team resolved (now or already), derive league
            if tid and league is None:
                trow = conn.execute(text(
                    "SELECT league FROM mlb.teams WHERE id=:i"), {"i": tid}).first()
                if trow:
                    league = trow[0]

            # 3) write team_id
            if tid is not None and (cur_tid is None or cur_tid != tid):
                conn.execute(text("UPDATE mlb.player_season_props SET team_id=:t WHERE id=:i"),
                             {"t": tid, "i": rid})

            # 4) rewrite generic prop_type -> league-specific
            if ptype in PROP_MAP and league:
                new_pt = PROP_MAP[ptype].format(league=league.lower())
                if new_pt != ptype:
                    conn.execute(text("UPDATE mlb.player_season_props SET prop_type=:p WHERE id=:i"),
                                 {"p": new_pt, "i": rid})
                    changed += 1

        print(f"prop_type rewrites: {changed}")

    with eng.connect() as conn:
        tot = conn.execute(text(
            "SELECT count(*), count(team_id) FROM mlb.player_season_props WHERE bookmaker='fanduel'")).fetchone()
        print(f"fanduel MLB after backfill: rows={tot[0]} team_id={tot[1]} NULL={tot[0]-tot[1]}")
        pts = conn.execute(text(
            "SELECT prop_type, count(*) FROM mlb.player_season_props WHERE bookmaker='fanduel' GROUP BY prop_type")).fetchall()
        print("prop_types:", sorted(r[0] for r in pts))


if __name__ == "__main__":
    main()
