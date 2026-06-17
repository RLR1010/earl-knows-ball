import logging
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.core.config import settings

database_url = settings.database_url
logger = logging.getLogger("uvicorn")

# Create async engine with search_path set for all connections
async_engine = create_async_engine(
    database_url,
    connect_args={"server_settings": {"search_path": "nfl, public"}},
    pool_pre_ping=True,
)

async_session = async_sessionmaker(async_engine, expire_on_commit=False)

# Sync engine for scheduler and other non-async operations
# Must use the same search_path (quoted properly for psycopg2)
sync_url = database_url.replace("+asyncpg", "+psycopg2")
sync_options = "-c search_path='nfl, public'"
engine = create_engine(
    sync_url,
    pool_pre_ping=True,
    connect_args={"options": sync_options},
)

Session = async_session
SessionLocal = sessionmaker()
Base = declarative_base()

async def get_db():
    """Dependency that provides a database session."""
    async with async_session() as session:
        yield session
