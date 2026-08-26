"""
Single source of truth for database connection URLs.

Every module and script should import from here instead of hardcoding.
The password lives in `.env` — this module is the ONLY .py file with a fallback.
"""
import os
from pathlib import Path

# ---- Auto-load .env into os.environ when this module is imported ----
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(_env_path, override=False)
    except ImportError:
        pass
del _env_path

# ---- Canonical URL (async, for SQLAlchemy + asyncpg) ----
# We intentionally do NOT ship a password fallback here. If DATABASE_URL is
# unset we fail fast (ImportError at module load) so a misconfigured deploy can
# never silently connect with a guessable dev credential (hardened 2026-08-24).
_DATABASE_URL = os.environ.get("DATABASE_URL")
if not _DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. Set it in the environment or backend/.env "
        "(e.g. postgresql+asyncpg://earl_web:...@localhost:5432/earl_knows_football)."
    )
ASYNC_DATABASE_URL = _DATABASE_URL

# ---- Admin / DDL connection (superuser) ----
# Used ONLY by ingestion/backfill/migration scripts that legitimately run
# TRUNCATE / DROP TABLE / CREATE TABLE etc. Web/API traffic must NOT use this.
# Falls back to the database_url for local/dev setups when ADMIN_DATABASE_URL
# is unset (so DDL scripts keep working without a second URL being required).
ASYNC_ADMIN_DATABASE_URL = os.environ.get("ADMIN_DATABASE_URL", ASYNC_DATABASE_URL)

# ---- Derived formats ----
# For SQLAlchemy sync engine (uses psycopg2 driver)
SYNC_DATABASE_URL = ASYNC_DATABASE_URL.replace("+asyncpg", "+psycopg2")

# For raw psycopg2.connect() — no driver suffix
PSYCOPG2_DATABASE_URL = ASYNC_DATABASE_URL.replace("+asyncpg", "")
