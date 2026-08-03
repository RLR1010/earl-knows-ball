"""Live chat-status store (DB-backed, shared across Granian workers).

SSE through Caddy is gzip-buffered, so research statuses don't stream live to
the browser. Each backend status update is written here (shared across all
Granian workers via PostgreSQL) and the frontend polls GET /chat/status/{id}.

Why DB (not in-memory)? Granian runs 4 workers = 4 separate processes. An
in-memory dict in one worker is invisible to poll requests served by another.
PostgreSQL gives a consistent view across all workers.
"""
from datetime import datetime, timezone

from sqlalchemy import delete, select, update as sa_update

from app.database import async_session
from app.models.chat_status import ChatStatus, _expires


async def set_chat_status(request_id: str, status: str) -> None:
    """Upsert the latest status for a chat request (creates or updates row)."""
    now = datetime.now(timezone.utc)
    expires = _expires()
    async with async_session() as session:
        row = await session.execute(
            select(ChatStatus.id, ChatStatus.status).where(ChatStatus.request_id == request_id)
        )
        existing = row.first()
        if existing is None:
            session.add(ChatStatus(request_id=request_id, status=status,
                                   expires_at=expires, updated_at=now))
            await session.commit()
        else:
            await session.execute(
                sa_update(ChatStatus)
                .where(ChatStatus.request_id == request_id)
                .values(status=status, expires_at=expires, updated_at=now)
            )
            await session.commit()


async def get_chat_status(request_id: str) -> str | None:
    """Return the latest status, or None if absent/expired."""
    now = datetime.now(timezone.utc)
    async with async_session() as session:
        row = await session.execute(
            select(ChatStatus.status, ChatStatus.expires_at)
            .where(ChatStatus.request_id == request_id)
        )
        found = row.first()
        if found is None:
            return None
        status, exp = found
        if exp < now:
            await session.execute(
                delete(ChatStatus).where(ChatStatus.request_id == request_id)
            )
            await session.commit()
            return None
        return status


async def clear_chat_status(request_id: str) -> None:
    """Remove the status row once a chat request completes."""
    async with async_session() as session:
        await session.execute(delete(ChatStatus).where(ChatStatus.request_id == request_id))
        await session.commit()
