"""Repair `nfl.depth_charts.player_id` using position-aware name matching.

The original scrape resolved a depth-chart player purely by name
(`_player_name_index` keyed on name only), so a same-name player at a different
position could win — e.g. BAL QB "Lamar Jackson" -> CB Lamar Jackson,
IND QB "Daniel Jones" -> OT Daniel Jones. Those rows then carry a player_id with
no quarterback stats, which blanks the QB feature block in the NFL data_loader
(and defeats the early-season prior-season blend).

This script re-resolves every depth-chart row with the fixed, position-aware
matcher from `app.ingestion.depth_charts` and updates the ones that change.

Usage:
    python -m app.scripts.fix_depth_chart_player_ids            # dry run
    python -m app.scripts.fix_depth_chart_player_ids --apply    # write changes
"""
import sys

from sqlalchemy import create_engine, text

from app.core.config import settings
from app.ingestion.depth_charts import _norm_name, _name_key, _pos_group, _match_player_id


def _build_index(rows):
    by_full, by_initial, by_surname = {}, {}, {}
    for pid, pname, ppos in rows:
        toks = _norm_name(pname).split()
        if len(toks) < 2:
            continue
        first_tok, last_tok = toks[0], toks[-1]
        grp = _pos_group(ppos)
        by_full.setdefault((first_tok, last_tok), []).append((pid, grp))
        by_initial.setdefault((first_tok[0], last_tok), []).append((pid, grp))
        by_surname.setdefault(last_tok, []).append((pid, grp))
    return by_full, by_initial, by_surname


def main():
    apply = "--apply" in sys.argv
    engine = create_engine(settings.database_url.replace("+asyncpg", "+psycopg2"))
    with engine.begin() as conn:
        players = conn.execute(
            text("SELECT id, name, position FROM nfl.players")
        ).all()
        by_full, by_initial, by_surname = _build_index(players)

        dc = conn.execute(
            text(
                "SELECT id, team_id, position, slot, player_name, player_id "
                "FROM nfl.depth_charts ORDER BY team_id, position, slot"
            )
        ).all()

        pid_pos = {r[0]: r[2] for r in players}
        teams = {r[0]: r[1] for r in conn.execute(text("SELECT id, abbreviation FROM nfl.teams")).all()}

        changes = []
        for row in dc:
            new = _match_player_id(
                by_full, by_initial, by_surname, row.player_name, _pos_group(row.position)
            )
            if new != row.player_id:
                changes.append((row, new))

        def grp_mismatch(pid, dpos):
            # Only meaningful when the depth-chart slot has a known position group
            # (KR/PR returner slots have none). A group mismatch or unresolved id counts.
            g = _pos_group(dpos)
            if g is None:
                return False
            return pid is None or _pos_group(pid_pos.get(pid)) != g

        new_by_id = {row.id: new for row, new in changes}
        before = sum(1 for r in dc if grp_mismatch(r.player_id, r.position))
        after = sum(
            1 for r in dc if grp_mismatch(new_by_id.get(r.id, r.player_id), r.position)
        )

        print(f"depth_charts rows: {len(dc)}")
        print(f"rows whose player_id changes: {len(changes)}")
        print(f"position-group mismatches: before={before} after={after}")
        print("-" * 100)
        for row, new in changes:
            old_name = conn.execute(
                text("SELECT name || ' (' || COALESCE(position,'?') || ')' FROM nfl.players WHERE id=:i"),
                {"i": row.player_id},
            ).scalar() if row.player_id else None
            new_name = conn.execute(
                text("SELECT name || ' (' || COALESCE(position,'?') || ')' FROM nfl.players WHERE id=:i"),
                {"i": new},
            ).scalar() if new else None
            print(
                f"{teams.get(row.team_id,'?'):4} {row.position:4} slot{row.slot:>2} "
                f"{row.player_name:22} {str(row.player_id):>8} {str(old_name):32} "
                f"-> {str(new):>8} {str(new_name):32}"
            )

        if apply:
            for row, new in changes:
                conn.execute(
                    text("UPDATE nfl.depth_charts SET player_id=:n WHERE id=:i"),
                    {"n": new, "i": row.id},
                )
            print(f"\nApplied {len(changes)} updates.")
        else:
            print("\nDRY RUN — pass --apply to write changes.")
    engine.dispose()


if __name__ == "__main__":
    main()
