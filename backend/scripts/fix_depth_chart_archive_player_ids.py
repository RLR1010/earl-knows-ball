"""Backfill depth_charts_archive.player_id from nfl.players by name.

Archive names are 'First Last' (mostly) with interleaved transaction tags
('Ryan U/Bal Jensen'), suffixes, and case noise. Matches on canonical
(FIRST, LAST); falls back to unique LAST-name match when the full name
doesn't resolve. Now that nfl.players holds the complete roster (~25k),
most archive rows should link.

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

_TAG = re.compile(r"\b(?:U|P|W|T|CF|SF|WA|IR|NFI|PUP|SUS|SSD|LoS|A|SUSP)/[A-Za-z0-9]+\b|\b\d{2}/\d{1,2}\b|\b(?:CF|SF)\d{2}\b|\bA/[A-Z][a-z]+\b", re.I)
_SUFFIX = {"JR", "SR", "II", "III", "IV", "V", "JR.", "SR."}


def _tokens(s):
    if not s:
        return []
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = _TAG.sub(" ", s)
    toks = [t.strip(" '-.") for t in re.split(r"[\s.,]+", s) if t.strip()]
    toks = [t for t in toks if t.upper() not in {x.upper() for x in _SUFFIX}]
    return toks


def _norm_first_last(s):
    """Archive 'First Last' -> (FIRST, LAST)."""
    toks = _tokens(s)
    if len(toks) < 2:
        return "".join(toks).upper(), ""
    return " ".join(toks[:-1]).upper(), toks[-1].upper()


def _lasttok(s):
    toks = _tokens(s)
    return toks[-1].upper() if toks else ""


def main():
    with _sync_engine.begin() as c:
        r = c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='nfl' "
            "AND table_name='depth_charts_archive' AND column_name='player_id'"))
        if not r.fetchone():
            c.execute(text("ALTER TABLE nfl.depth_charts_archive ADD COLUMN player_id INTEGER"))
            print("added player_id column")

    with _sync_engine.connect() as c:
        players = c.execute(text("SELECT id, name FROM nfl.players")).all()
        arch = c.execute(text(
            "SELECT id, player_name, player_id FROM nfl.depth_charts_archive "
            "WHERE player_id IS NULL")).all()

    from collections import defaultdict
    by_fl = defaultdict(list)
    by_last = defaultdict(list)
    for pid, name in players:
        f, l = _norm_first_last(name)
        if f and l:
            by_fl[(f, l)].append(pid)
        by_last[l].append(pid)

    total = 0
    use = []
    for aid, pname, _cur in arch:
        total += 1
        f, l = _norm_first_last(pname)
        # prefer full-name match
        if (f, l) in by_fl:
            if len(by_fl[(f, l)]) == 1:
                use.append((aid, by_fl[(f, l)][0]))
                continue
        # fallback: unique lastname
        cands = by_last.get(l, [])
        if len(cands) == 1:
            use.append((aid, cands[0]))

    print(f"archive unmatched: {total}; will link: {len(use)}")

    if APPLY and use:
        with _sync_engine.begin() as c:
            for aid, pid in use:
                c.execute(text("UPDATE nfl.depth_charts_archive SET player_id=:p WHERE id=:i"),
                          {"p": pid, "i": aid})
        print(f"APPLIED {len(use)} archive backfills.")
    else:
        print("(dry run — pass --apply)")

    if APPLY:
        with _sync_engine.connect() as c:
            r = c.execute(text('''
                SELECT s.year, s.total, s.linked FROM (
                  SELECT EXTRACT(YEAR FROM snapshot_date) AS year,
                         count(*) total, count(player_id) linked
                  FROM nfl.depth_charts_archive GROUP BY 1) s ORDER BY s.year''')).all()
            print("\narchive linkage by year:")
            for yr, t_, lk in r:
                print(f"  {int(yr)}: {int(lk)}/{int(t_)}")


if __name__ == "__main__":
    main()
