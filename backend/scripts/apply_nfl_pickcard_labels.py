"""Apply pick-card display_name + description to nfl.features.
Usage: python scripts/apply_nfl_pickcard_labels.py [--db dev|prod]
Loads PICKCARD_LABELS and UPDATEs nfl.features for every pick_card=TRUE row.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import create_engine, text

DB = "dev"
if "--db" in sys.argv:
    DB = sys.argv[sys.argv.index("--db") + 1]

if DB == "prod":
    # prod DSN from env file
    url = None
    for p in ["/home/rich/.openclaw/workspace/earl-knows-football/backend/.env.prod",
              "/home/rich/earl-knows-football/backend/.env.prod"]:
        if os.path.exists(p):
            for line in open(p):
                if line.startswith("SYNC_DATABASE_URL="):
                    url = line.strip().split("=", 1)[1]
                elif line.startswith("DATABASE_URL=") and not url:
                    url = line.strip().split("=", 1)[1].replace("+asyncpg", "")
            if url:
                break
    if not url:
        print("ERROR: no prod URL found"); sys.exit(1)
    import sys
    # support for sqlalchemy
    engine = create_engine(url, pool_pre_ping=True)
else:
    from app.db_urls import SYNC_DATABASE_URL
    engine = create_engine(SYNC_DATABASE_URL, pool_pre_ping=True)
    DB = "dev"

from scripts.nfl_pickcard_labels import PICKCARD_LABELS

with engine.connect() as conn, conn.begin():
    for name, (dn, desc) in PICKCARD_LABELS.items():
        conn.execute(text(
            "UPDATE nfl.features SET display_name=:dn, description=:desc "
            "WHERE name=:name AND pick_card=TRUE"),
            {"dn": dn, "desc": desc, "name": name})
    # verify
    updated = conn.execute(text(
        "SELECT COUNT(*) FROM nfl.features WHERE pick_card=TRUE "
        "AND display_name IS NOT NULL AND display_name<>'' "
        "AND description IS NOT NULL AND description<>''")).scalar()
    total = conn.execute(text(
        "SELECT COUNT(*) FROM nfl.features WHERE pick_card=TRUE")).scalar()
print(f"[{DB}] Applied. pick_card total={total}, filled (dn+desc)={updated}")
