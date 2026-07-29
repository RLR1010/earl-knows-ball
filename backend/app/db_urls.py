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
# Only this one line has a hardcoded fallback with a password.
# Every other module in the codebase should get their URL from here.
ASYNC_DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://earl:earl_dev_pass@localhost:5432/earl_knows_football",
)

# ---- Derived formats ----
# For SQLAlchemy sync engine (uses psycopg2 driver)
SYNC_DATABASE_URL = ASYNC_DATABASE_URL.replace("+asyncpg", "+psycopg2")

# For raw psycopg2.connect() — no driver suffix
PSYCOPG2_DATABASE_URL = ASYNC_DATABASE_URL.replace("+asyncpg", "")
