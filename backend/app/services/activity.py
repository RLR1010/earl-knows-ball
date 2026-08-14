"""Fire-and-forget user IP + daily usage activity logging.

Called on every authenticated request. Records/refreshes a single row per
(user, calendar day, ip_address). The DB write is dispatched to a background
task with its OWN AsyncSession so it never blocks or ties up the request's
session/txn. Errors are swallowed and logged — activity logging must never
break a request.
"""

import logging
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.database import async_session
from app.models.user_activity import UserActivity

logger = logging.getLogger(__name__)

# Trusted header used to recover the real client IP behind Caddy.
# Caddy reverse-proxies /api/* to the API (8001) / compute (8002) boxes, so
# without this every IP would read as 127.0.0.1. We only trust this header
# because Caddy (not the client) sets/overwrites it.
CLIENT_IP_HEADER = "X-Forwarded-For"


def client_ip(request: Request) -> str | None:
    """Resolve the client IP, preferring the real one behind the Caddy proxy."""
    fwd = request.headers.get(CLIENT_IP_HEADER)
    if fwd:
        # Take the left-most entry (closest to the client). Caddy appends.
        first = fwd.split(",")[0].strip()
        if first:
            return first
    # Fall back to the immediate peer (127.0.0.1 when proxied).
    if request.client:
        return request.client.host
    return None


async def _write_activity(user_id: str, ip: str) -> None:
    """Upsert today's row for (user_id, ip). Runs in its own session/task."""
    today = datetime.now(timezone.utc).date()
    try:
        async with async_session() as db:
            result = await db.execute(
                select(UserActivity).where(
                    UserActivity.user_id == user_id,
                    UserActivity.activity_date == today,
                    UserActivity.ip_address == ip,
                )
            )
            row = result.scalar_one_or_none()
            now = datetime.now(timezone.utc)
            if row:
                row.hit_count += 1
                row.last_seen = now
            else:
                db.add(UserActivity(
                    user_id=user_id,
                    activity_date=today,
                    ip_address=ip,
                    first_seen=now,
                    last_seen=now,
                    hit_count=1,
                ))
            await db.commit()
    except Exception:  # pragma: no cover - activity logging must never fail a request
        logger.exception("Failed to record activity for user %s", user_id)


def record_activity(request: Request, user_id: str) -> None:
    """Fire-and-forget activity logging. Never raises, never blocks the request."""
    if not user_id:
        return
    ip = client_ip(request)
    if not ip:
        return
    # Python 3.11+ semantics: the task keeps a strong ref while running, and
    # the reference is dropped when done. We hold `_task` to avoid GC issues.
    _task = _spawn_activity(user_id, ip)


def _spawn_activity(user_id: str, ip: str):
    """Spawn the background write task."""
    import asyncio
    try:
        task = asyncio.get_running_loop().create_task(_write_activity(user_id, ip))
        return task
    except Exception:  # pragma: no cover
        logger.exception("Could not schedule activity write for user %s", user_id)
        return None
