#!/usr/bin/env python3
"""
Backfill: any feature hardcoded in a sport data_loader that is NOT in the DB
`features` table gets inserted with ALL boolean columns set to FALSE:
  pick_card, is_trainable, current_ats, current_ou, live_ou, live_ats
"""
import ast
import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football")
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = "/home/rich/.openclaw/workspace/earl-knows-football"

# catalog dict assignment names per sport (FEATURES + COMPUTED + NFL's RAW_FEATURES)
SPORT_CATALOGS = {
    "mlb": ["FEATURES_CATALOG", "COMPUTED_FEATURES_CATALOG"],
    "nfl": ["FEATURES_CATALOG", "COMPUTED_FEATURES_CATALOG", "RAW_FEATURES_CATALOG"],
    "nba": ["FEATURES_CATALOG", "COMPUTED_FEATURES_CATALOG"],
}
SPORT_DISPLAY = {"mlb": "DISPLAY_NAMES", "nfl": "DISPLAY_NAMES", "nba": "DISPLAY_NAMES"}
PATHS = {
    "mlb": "backend/app/handicapping/mlb/data_loader.py",
    "nfl": "backend/app/handicapping/nfl/data_loader.py",
    "nba": "backend/app/handicapping/nba/data_loader.py",
}
BOOLEAN_FIELDS = ["pick_card", "is_trainable", "current_ats", "current_ou", "live_ou", "live_ats"]
ALL_FALSE = {f: False for f in BOOLEAN_FIELDS}


def extract_dicts(path, names):
    """Return {name: dict} for each top-level dict assignment found in the file."""
    src = open(path).read()
    tree = ast.parse(src)
    out = {}
    for node in tree.body:
        # Handle both `X = {...}` (Assign) and `X: Type = {...}` (AnnAssign)
        if isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Dict) \
                and isinstance(node.target, ast.Name) and node.target.id in names:
            try:
                out[node.target.id] = ast.literal_eval(node.value)
            except Exception as e:
                print(f"  !! literal_eval failed for {node.target.id}: {e}")
            continue
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in names and isinstance(node.value, ast.Dict):
                    try:
                        out[tgt.id] = ast.literal_eval(node.value)
                    except Exception as e:
                        print(f"  !! literal_eval failed for {tgt.id}: {e}")
    return out


def norm(name):
    return name[:-4] if name.endswith("_raw") else name


async def backfill(eng, sport, dry=False):
    path = os.path.join(ROOT, PATHS[sport])
    dicts = extract_dicts(path, SPORT_CATALOGS[sport] + [SPORT_DISPLAY[sport]])
    catalog = {}
    for cname in SPORT_CATALOGS[sport]:
        catalog.update(dicts.get(cname, {}))
    display = dicts.get(SPORT_DISPLAY[sport], {})

    if not catalog:
        print(f"[{sport}] ERROR: no catalog dicts parsed"); return

    async with eng.connect() as c:
        r = await c.execute(text(f"SELECT name FROM {sport}.features"))
        db_names = {norm(x[0]) for x in r}

    # hardcoded names whose description value is a real string (skip empty)
    hard_normalized = {norm(k): k for k in catalog}
    db_norm_set = {norm(x) for x in db_names}

    # Names hardcoded but NOT in DB (compare normalized)
    to_insert = []
    for norm_name, orig_name in hard_normalized.items():
        if norm_name in db_norm_set:
            continue
        to_insert.append((orig_name, norm_name))

    print(f"[{sport}] hardcoded={len(catalog)} db={len(db_names)} -> missing (to insert)={len(to_insert)}")

    if not to_insert:
        return

    inserted = 0
    skipped = 0
    async with eng.connect() as c:
        # Determine the next safe id above the current max to avoid PK collisions
        # (the auto-increment sequence can lag behind manually-inserted rows).
        maxrow = await c.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {sport}.features"))
        next_id = maxrow.scalar() + 1
        if dry:
            print(f"  WOULD insert (all booleans FALSE): {[n for n, _ in to_insert]}")
        for orig_name, norm_name in to_insert:
            desc = catalog.get(orig_name) or catalog.get(norm_name) or ""
            disp = display.get(orig_name) or display.get(norm_name) or orig_name
            # use the hardcoded catalog NAME verbatim (matches get_features_catalog())
            insert_name = orig_name
            if dry:
                continue
            try:
                await c.execute(
                    text(
                        f"INSERT INTO {sport}.features (id, name, description, display_name, "
                        f"pick_card, is_trainable, current_ats, current_ou, live_ou, live_ats) "
                        f"VALUES (:id, :name, :desc, :disp, :pick_card, :is_trainable, :current_ats, :current_ou, :live_ou, :live_ats)"
                    ),
                    {
                        "id": next_id,
                        "name": insert_name,
                        "desc": desc,
                        "disp": disp,
                        **ALL_FALSE,
                    },
                )
                next_id += 1
                inserted += 1
            except Exception as e:
                print(f"  !! insert failed for {insert_name}: {e}")
                skipped += 1
        if (not dry):
            await c.commit()
            print(f"[{sport}] INSERTED {inserted} features, skipped {skipped} (all booleans FALSE)")
    if inserted and not dry:
        print(f"  sample inserted: {[n for n, _ in to_insert[:10]]}")


async def main():
    dry = "--dry-run" in sys.argv
    eng = create_async_engine(os.environ["DATABASE_URL"])
    print("DRY RUN (no writes)" if dry else "LIVE RUN (will write)")
    for sport in ("mlb", "nba", "nfl"):
        await backfill(eng, sport, dry=dry)
    await eng.dispose()


if __name__ == "__main__":
    asyncio.run(main())
