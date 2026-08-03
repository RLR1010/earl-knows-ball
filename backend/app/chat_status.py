"""In-memory store for live chat status updates.

The chat response is delivered via SSE, but Caddy gzip-buffers text/event-stream
responses, so progress statuses don't reach the browser incrementally. To keep
the UI live without depending on proxy SSE behavior, the backend writes each
status to this store as it streams, and the frontend polls it via a lightweight
endpoint (GET /chat/status/{request_id}).

Not a durable store — entries expire after STATUS_TTL_SECONDS to avoid a leak.
Every status key is scoped to the authenticated user.
"""
from datetime import datetime, timedelta, timezone
from threading import Lock

STATUS_TTL_SECONDS = 300  # 5 min — chat research far exceeds this, so no leak


class ChatStatusStore:
    def __init__(self, ttl_seconds: int = STATUS_TTL_SECONDS):
        self._ttl = ttl_seconds
        self._lock = Lock()
        # request_id -> (expires_at, status_text)
        self._data: dict[str, tuple[datetime, str]] = {}

    def set(self, request_id: str, status: str) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self._data[request_id] = (now + timedelta(seconds=self._ttl), status)

    def get(self, request_id: str) -> str | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            row = self._data.get(request_id)
            if row is None:
                return None
            expires_at, status = row
            if expires_at < now:
                self._data.pop(request_id, None)
                return None
            return status

    def clear(self, request_id: str) -> None:
        with self._lock:
            self._data.pop(request_id, None)


# Module-level singleton shared across routers.
chat_status_store = ChatStatusStore()
