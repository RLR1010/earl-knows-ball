import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import settings
from app.db_urls import ASYNC_DATABASE_URL as DB_URLS_ASYNC

database_url = settings.database_url
logger = logging.getLogger("earl.database")

# Create async engine with search_path set for all connections.
# statement_timeout=20min: guards against orphaned/wedged backends (seen down
# stream when a client dies mid-query — backend hangs pushing results into a
# dead socket). Longest legit query is the full-history MLB load (~4 min),
# so 20 min gives large headroom without letting hangs run forever.
async_engine = create_async_engine(
    database_url,
    connect_args={"server_settings": {
        "search_path": "nfl, public",
        "statement_timeout": "1200000",
    }},
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=20,
    max_overflow=10,
)

async_session = async_sessionmaker(async_engine, expire_on_commit=False)

# Sync engine for scheduler and other non-async operations.
# IMPORTANT: psycopg2 breaks if the options value uses single quotes around the
# list (FATAL: invalid value for parameter "search_path": "'nfl,"). Use the
# unquoted comma-separated form. Consumers schema-qualify their tables, but set
# a search_path covering all sports so unqualified refs resolve too.
sync_url = database_url.replace("+asyncpg", "+psycopg2")
sync_options = "-c search_path=nfl,nba,mlb,public -c statement_timeout=1200000"
engine = create_engine(
    sync_url,
    pool_pre_ping=True,
    pool_recycle=300,
    pool_size=10,
    max_overflow=5,
    connect_args={"options": sync_options},
)

Session = async_session
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

async def get_db():
    """Dependency that provides a database session."""
    async with async_session() as session:
        yield session
