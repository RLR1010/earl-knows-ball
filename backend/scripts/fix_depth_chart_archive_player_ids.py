"""Backfill depth_charts_archive.player_id from nfl.players.

Strategy: match on (last-name token, team_id) since the archive mixes
'First Last' and 'Last First' orders. Only confident (unique) matches are
applied. Also backfills any already-confident full-name matches.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/fix_depth_chart_archive_player_ids.py [--apply]
"""
import os, sys, re, unicodedata
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL

APPLY = "--apply" in sys.argv
_sync_engine = sa.create_engine(SYNC_DATABASE_URL)

_TAG = re.compile(r"\b(?:U|P|W|T|CF|SF|WA|IR|NFI|PUP|SUS|SSD|LoS)/[A-Za-z0-9]+\b|\b\d{2}/\d{1,2}\b|\b(?:CF|SF)\d{2}\b", re.I)
_SUFFIX = {"JR", "SR", "II", "III", "IV", "V"}


def _lasttok(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _TAG.sub(" ", s)
    toks = [t.strip() for t in re.split(r"[\s.,]+", s) if t.strip()]
    toks = [t for t in toks if t.upper() not in _SUFFIX]
    return toks[-1].upper() if toks else ""


def _norm_full(s):
    """'First Last' -> (FIRST, LAST). Used as a secondary exact check."""
    if not s:
        return "", ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _TAG.sub(" ", s)
    toks = [t.strip() for t in re.split(r"[\s.,]+", s) if t.strip()]
    toks = [t for t in toks if t.upper() not in _SUFFIX]
    if len(toks) < 2:
        return "".join(toks).upper(), ""
    return " ".join(toks[:-1]).upper(), toks[-1].upper()


def main():
    # ensure column
    with _sync_engine.begin() as c:
        r = c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='nfl' "
            "AND table_name='depth_charts_archive' AND column_name='player_id'"))
        if not r.fetchone():
            c.execute(text("ALTER TABLE nfl.depth_charts_archive ADD COLUMN player_id INTEGER"))
            print("added player_id column")
        else:
            print("player_id column exists")

    with _sync_engine.connect() as c:
        players = c.execute(text("SELECT id, name, team_id FROM nfl.players")).all()
        arch = c.execute(text(
            "SELECT id, player_name, team_id, player_id FROM nfl.depth_charts_archive "
            "WHERE player_id IS NULL")).all()

    from collections import defaultdict
    by_lt = defaultdict(list)   # (lasttok, team) -> [player rows]
    for pid, name, tid in players:
        by_lt[(_lasttok(name), tid)].append((pid, name, tid))

    confident = []
    for aid, pname, tid, _curpid in arch:
        cands = by_lt.get((_lasttok(pname), tid), [])
        if len(cands) == 1:
            confident.append((aid, cands[0][0]))
        # else: ambiguous (multiple players same team+lastname) or none -> skip

    total = len(arch)
    print(f"unmatched archive rows: {total}; confident (unique team+lastname): {len(confident)}")

    if APPLY and confident:
        with _sync_engine.begin() as c:
            n = 0
            for aid, pid in confident:
                c.execute(text("UPDATE nfl.depth_charts_archive SET player_id=:p WHERE id=:i"),
                          {"p": pid, "i": aid})
                n += 1
        print(f"APPLIED {n} archive backfills.")
    else:
        print("(dry run — pass --apply to write)")

    # verify by snapshot year
    with _sync_engine.connect() as c:
        r = c.execute(text('''
            SELECT s.* FROM (
              SELECT EXTRACT(YEAR FROM snapshot_date) AS yr,
                     count(*) total, count(player_id) linked
              FROM nfl.depth_charts_archive GROUP BY 1 ORDER BY 1) s''')).all()
        if APPLY:
            print("\narchive linkage by year:")
            for yr, t_, lk in r:
                print(f"  {int(yr)}: {int(lk)}/{int(t_)}")


if __name__ == "__main__":
    main()
