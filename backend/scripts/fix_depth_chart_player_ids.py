"""Analyze depth_charts NULL player_id rows and match to nfl.players by name.

Dry-run: prints a breakdown of confident / ambiguous / unmatched NULL rows.
Confident = exactly one players.row matches normalized name (+ team tiebreak).
Does NOT write anything. Run with --apply to actually backfill.

Run: PYTHONPATH=backend venv/bin/python backend/scripts/fix_depth_chart_player_ids.py [--apply]
"""
import os, sys, re, unicodedata
sys.path.insert(0, os.path.abspath("backend"))
os.environ.setdefault("PYTHONPATH", os.path.abspath("backend"))

import sqlalchemy as sa
from sqlalchemy import text
from app.db_urls import SYNC_DATABASE_URL

APPLY = "--apply" in sys.argv
_sync_engine = sa.create_engine(SYNC_DATABASE_URL)


def _norm(s):
    """Normalize a name to a comparable canonical form.

    Handles 'Last, First' depth-chart format, all-caps, suffixes
    (Jr/Sr/II/III/IV/V) and transaction tags ('U/Phi',' P/Det',' W/Car').
    """
    if not s:
        return "", ""
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")
    # split transaction/source tag: ' U/Phi', ' P/Det', ' W/NO', ' CF26'
    # tags look like: [ A-Z]/([A-Za-z0-9]+) OR trailing draft code like ' 26/3'
    s = re.sub(r"\s+(U|P|W|CF|SF|IR|PUP|NFI)/[A-Za-z0-9]+$", "", s.strip())
    s = re.sub(r"\s+(U|P|W|CF|SF|IR|PUP|NFI)/[A-Za-z0-9]+(\s|$)", " ", s.strip())
    # drop trailing draft acquisition shorthand like ' 26/3', ' CF25', ' 17/3'
    s = re.sub(r"\s+(?:[0-9]{2}/[0-9]|CF[0-9]{2}|W/[A-Z][a-z]+)\s*$", "", s.strip())
    # split last/first on comma
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 2:
        last, first = parts
    elif len(parts) > 2:  # "Cobb, Randall G U/Ten"
        last, first = parts[0], " ".join(parts[1:])
    else:
        # 'First Last' style
        toks = s.split()
        last = toks[-1] if toks else ""
        first = " ".join(toks[:-1]) if len(toks) > 1 else ""
    # suffixes may live on the LAST name: "Milton III", "Penix Jr.", "Minshew II"
    last = re.sub(r"\b(Jr|Sr)\.?$", "", last, flags=re.I).strip()
    last = re.sub(r"\b(I{1,3}|IV|V)$", "", last, flags=re.I).strip()
    # strip suffixes from FIRST part after comma (e.g. 'Jr., Chris' -> 'Jr.')
    first = re.sub(r"\b(Jr|Sr)\.?$", "", first, flags=re.I).strip()
    first = re.sub(r"\b(I{1,3}|IV|V)$", "", first, flags=re.I).strip()
    last = last.strip().upper()
    first = first.strip().upper()
    first = re.sub(r"\b(JR|SR|II|III|IV|V)\.?$", "", first).strip()
    last = re.sub(r"\b(JR|SR|II|III|IV|V)\.?$", "", last).strip()
    if not first:
        # 'First Last' style: we may have put firstname in last when no comma. Re-handle.
        toks = [t for t in re.split(r"[\s.,]+", s.upper()) if t]
        # suffixes to strip from the end tokens
        while toks and toks[-1] in ("JR", "SR", "II", "III", "IV", "V"):
            toks.pop()
        last = toks[-1] if toks else ""
        first = " ".join(toks[:-1]) if len(toks) > 1 else ""
    return first, last


def load_players():
    with _sync_engine.connect() as c:
        r = c.execute(text("SELECT id, name, team_id, position FROM nfl.players"))
        return [(int(i), n, t, p) for i, n, t, p in r]


def main():
    players = load_players()
    # index players by normalized (first, last)
    from collections import defaultdict
    by_name = defaultdict(list)
    for pid, name, teamid, pos in players:
        f, l = _norm(name)
        by_name[(f, l)].append((pid, teamid, pos, name))

    with _sync_engine.connect() as c:
        r = c.execute(text(
            "SELECT id, team_id, player_name FROM nfl.depth_charts WHERE player_id IS NULL"))
        rows = list(r)

    confident, ambiguous, none = [], [], []
    for dc_id, teamid, raw in rows:
        f, l = _norm(raw)
        cands = by_name.get((f, l), [])
        if not cands:
            none.append((dc_id, teamid, raw, None))
            continue
        # team tiebreak
        onteam = [x for x in cands if x[1] == teamid]
        pool = onteam if len(onteam) == 1 else cands
        if len(pool) == 1:
            confident.append((dc_id, teamid, raw, pool[0]))
        else:
            ambiguous.append((dc_id, teamid, raw, pool))

    print(f"NULL-pid depth_charts rows: {len(rows)}")
    print(f"  confident unique match : {len(confident)}")
    print(f"  ambiguous (multi)      : {len(ambiguous)}")
    print(f"  no match               : {len(none)}")

    if APPLY and confident:
        with _sync_engine.begin() as c:
            n = 0
            for dc_id, teamid, raw, (pid, pteam, ppos, pname) in confident:
                c.execute(text("UPDATE nfl.depth_charts SET player_id=:p WHERE id=:i"),
                          {"p": pid, "i": dc_id})
                n += 1
        print(f"\nAPPLIED {n} player_id backfills.")
    elif APPLY:
        print("\nNothing to apply.")

    if ambiguous:
        print("\n--- ambiguous (need review) sample ---")
        for dc_id, teamid, raw, pool in ambiguous[:15]:
            names = [f"{x[3]}(id{x[0]})" for x in pool]
            print(f"  {raw} team{teamid}: {names}")


if __name__ == "__main__":
    main()
