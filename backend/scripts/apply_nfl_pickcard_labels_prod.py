"""Prod runner: apply nfl_pickcard_labels to the production DB.
Reads DATABASE_URL from this box's backend/.env (asyncpg) -> converts to psycopg2
sync URL, then UPDATEs nfl.features for every pick_card=TRUE row.
"""
import os, sys, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from sqlalchemy import create_engine, text

# Derive sync URL from backend/.env DATABASE_URL
url = None
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
for line in open(env_path):
    if line.startswith("DATABASE_URL="):
        url = line.strip().split("=", 1)[1].replace("+asyncpg", "")
        break
if not url:
    print("ERROR: no DATABASE_URL"); sys.exit(1)

engine = create_engine(url, pool_pre_ping=True)

# Import mapping from the sibling script file by path (avoid package import issues on prod)
import importlib.util
spec = importlib.util.spec_from_file_location("labels", os.path.join(os.path.dirname(__file__), "nfl_pickcard_labels.py"))
labels_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(labels_mod)
LABELS = labels_mod.PICKCARD_LABELS

with engine.connect() as conn, conn.begin():
    for name, (dn, desc) in LABELS.items():
        conn.execute(text(
            "UPDATE nfl.features SET display_name=:dn, description=:desc "
            "WHERE name=:name AND pick_card=TRUE"),
            {"dn": dn, "desc": desc, "name": name})
    updated = conn.execute(text(
        "SELECT COUNT(*) FROM nfl.features WHERE pick_card=TRUE "
        "AND display_name IS NOT NULL AND display_name<>'' "
        "AND description IS NOT NULL AND description<>''")).scalar()
    total = conn.execute(text("SELECT COUNT(*) FROM nfl.features WHERE pick_card=TRUE")).scalar()
print(f"[PROD] Applied. pick_card total={total}, filled (dn+desc)={updated}")
