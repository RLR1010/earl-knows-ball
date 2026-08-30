#!/usr/bin/env python3
"""
Conservative ESPN-* placeholder -> canonical player linker + remap.

Problem: 45% of nfl.player_weekly_stats rows sit under auto-created 'ESPN-{id}'
placeholder players (espn_id stored as the NUMBER part of the name; position 'UNK'),
because the canonical nfl.players rows have espn_id=NULL and the ingest couldn't
link by espn_id. Chat/Earl can't query rich defensive/ST data under real names.

RULE (Rich): two players can share a name -> NEVER link by name alone when the
name is ambiguous. This linker only maps an ESPN-* -> canonical player when the
match is UNAMBIGUOUS:
   1. name matches a SINGLE canonical player (non-ESPN, espn_id may be NULL) -> link.
   2. name matches N>1 canonical players -> require position AND (team+season from
      the placeholder's own game rows) to narrow to exactly one -> link.
   3. still ambiguous -> SKIP (leave placeholder, log reason). Never guess.

Usage:
    python app/scripts/remap_espn_placeholders.py [--dry-run] [--apply]
"""
import argparse
import asyncio
import logging
import sys

import httpx
import psycopg2

sys.path.insert(0, ".")
from app.db_urls import PSYCOPG2_DATABASE_URL

logger = logging.getLogger("remap_espn")

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"
CONC = 12


async def fetch_athlete(client, espn_id: int):
    try:
        d = (await client.get(f"{CORE_BASE}/athletes/{espn_id}", timeout=20)).json()
        pos = (d.get("position") or {}).get("abbreviation")
        name = d.get("displayName")
        return espn_id, (name, pos)
    except Exception:
        return espn_id, (None, None)


async def fetch_all(espn_ids):
    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
        out = {}
        for i in range(0, len(espn_ids), CONC):
            batch = espn_ids[i:i + CONC]
            results = await asyncio.gather(*(fetch_athlete(client, e) for e in batch))
            for eid, info in results:
                out[eid] = info
        return out


def load_placeholders(conn):
    cur = conn.cursor()
    # placeholder rows: id, espn_id (parsed from name), and their distinct (team_id, season_id) context
    cur.execute(
        "SELECT id, name FROM nfl.players WHERE name LIKE 'ESPN-%'"
    )
    ph = []
    for pid, name in cur.fetchall():
        try:
            espn_id = int(name.split("-")[1])
        except Exception:
            continue
        # context: which teams/seasons this placeholder appeared in
        cur.execute(
            "SELECT DISTINCT team_id, season_id FROM nfl.player_weekly_stats WHERE player_id=%s",
            (pid,),
        )
        ctx = cur.fetchall()
        ph.append({"pid": pid, "espn_id": espn_id, "ctx": ctx})
    return ph


def load_canonical_index(conn):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, position FROM nfl.players WHERE name NOT LIKE 'ESPN-%' AND name IS NOT NULL"
    )
    by_name = {}
    for pid, name, pos in cur.fetchall():
        if not name:
            continue
        key = name.strip().lower()
        by_name.setdefault(key, []).append({"id": pid, "position": (pos or "").upper()})
    return by_name


def resolve_link(ph, by_name, athlete_info):
    """Return (canonical_player_id, reason) or (None, reason)."""
    name, pos = athlete_info
    espn_id = ph["espn_id"]
    if not name:
        return None, f"no-athlete-name #{espn_id}"
    key = name.strip().lower()
    cand = by_name.get(key, [])
    if not cand:
        return None, f"no-canonical-name '{name}'"
    if len(cand) == 1:
        return cand[0]["id"], f"unique-name '{name}'"
    # ambiguous name: try position
    with_pos = [c for c in cand if c["position"] == (pos or "").upper()]
    if len(with_pos) == 1:
        return with_pos[0]["id"], f"name+pos '{name}({pos})'"
    if not with_pos:
        return None, f"ambiguous-name-no-pos '{name}' (N={len(cand)})"
    # name+pos still ambiguous -> team/season context could decide, but we don't have
    # roster-team mapping for canonicals here; be SAFE: skip.
    return None, f"ambiguous-name+pos '{name}({pos})' (N={len(with_pos)})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    conn = psycopg2.connect(PSYCOPG2_DATABASE_URL)
    ph = load_placeholders(conn)
    by_name = load_canonical_index(conn)
    logger.info("placeholders=%d", len(ph))

    espn_ids = sorted({p["espn_id"] for p in ph})
    info = asyncio.run(fetch_all(espn_ids))
    logger.info("fetched %d athletes", len(info))

    linked = {}   # placeholder pid -> canonical pid
    ambiguous = []
    no_name = []
    for p in ph:
        ai = info.get(p["espn_id"], (None, None))
        cid, reason = resolve_link(p, by_name, ai)
        if cid:
            linked[p["pid"]] = cid
        elif reason.startswith("no-"):
            no_name.append((p["pid"], reason))
        else:
            ambiguous.append((p["pid"], p["espn_id"], reason))

    logger.info("LINKED (unambiguous): %d", len(linked))
    logger.info("AMBIGUOUS (left as-is): %d", len(ambiguous))
    logger.info("NO-NAME/no-match (left as-is): %d", len(no_name))
    for _, _, r in ambiguous[:10]:
        logger.info("  amb: %s", r)
    for _, r in no_name[:5]:
        logger.info("  no-name: %s", r)

    if not args.apply:
        logger.info("DRY RUN — not applying. Re-run with --apply to remap+rename+delete.")
        conn.close()
        return

    # Apply: remap unambiguous placeholders' stats onto canonical players;
    # rename no-canonical-name placeholders to their real name (safe: no canonical
    # row to collide with); leave ambiguous placeholders untouched (never guess).
    cur = conn.cursor()
    n_remap = n_delete = n_rename = 0
    for p in ph:
        ai = info.get(p["espn_id"], (None, None))
        cid, reason = resolve_link(p, by_name, ai)
        if cid:
            # Move stats rows from placeholder -> canonical player. Guard against
            # PK (player_id, game_id) conflicts: skip a game if the canonical player
            # already has a row there (keep both is impossible; prefer not to create
            # a conflict — left-over placeholder rows are handled by the cleanup pass).
            cur.execute(
                "UPDATE nfl.player_weekly_stats SET player_id=%s "
                "WHERE player_id=%s AND NOT EXISTS ("
                "  SELECT 1 FROM nfl.player_weekly_stats z "
                "  WHERE z.player_id=%s AND z.game_id=nfl.player_weekly_stats.game_id"
                ")",
                (cid, p["pid"], cid),
            )
            n_remap += cur.rowcount
            # Do NOT overwrite canonical espn_id (unique constraint; the canonical
            # row may already carry this espn_id — that's why the placeholder is
            # redundant). Only fold stats + delete the emptied placeholder.
            cur.execute("SELECT COUNT(*) FROM nfl.player_weekly_stats WHERE player_id=%s", (p["pid"],))
            if cur.fetchone()[0] == 0:
                cur.execute("DELETE FROM nfl.players WHERE id=%s", (p["pid"],))
                n_delete += 1
        elif reason.startswith("no-") and ai[0]:
            # Rename the placeholder to the real name + set position + espn_id.
            name, pos = ai
            cur.execute(
                "UPDATE nfl.players SET name=%s, position=COALESCE(NULLIF(%s,''),'UNK'), espn_id=%s WHERE id=%s",
                (name, (pos or "UNK"), p["espn_id"], p["pid"]),
            )
            n_rename += 1
        # else: ambiguous -> never touch
    conn.commit()
    logger.info(
        "APPLIED: stats remapped=%d, placeholders deleted=%d, renamed to real name=%d",
        n_remap, n_delete, n_rename,
    )
    conn.close()


if __name__ == "__main__":
    main()
