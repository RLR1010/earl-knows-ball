"""Chat live-status — DB-backed store shared across all Granian workers.

SSE through Caddy is gzip-buffered, so research statuses don't stream live to
the browser. Instead the backend writes each status into this table (shared
across the 4 Granian workers via PostgreSQL) and the frontend polls
GET /chat/status/{request_id} every ~650ms to show live updates.

An in-memory store can't be used: each Granian worker is a separate process, so
statuses written by one worker aren't visible to poll requests served by another.
This table replaces that. Rows expire via a TTL (expires_at) and are cleaned up
lazily on read.
"""

from sqlalchemy import (
    Column, BigInteger, DateTime, String,
)
from datetime import datetime, timezone

from app.database import Base

# After this long without a read, a status row is treated as expired (finished).
STATUS_TTL_SECONDS = 300


class ChatStatus(Base):
    """Latest live research status for a chat request (polled by the frontend)."""

    __tablename__ = "chat_status"
    __table_args__ = {"schema": "public"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, unique=True, index=True)
    status = Column(String(1024), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)


def _expires() -> datetime:
    from datetime import timedelta
    return datetime.now(timezone.utc) + timedelta(seconds=STATUS_TTL_SECONDS)
