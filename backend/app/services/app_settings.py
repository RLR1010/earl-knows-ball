"""Read/write access to admin-editable global settings (public.app_settings).

Thin helper layer so runtime features can read config without importing the ORM
model. Keys are plain strings; values are stored as text and coerced by callers.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# --- keys ---
FREE_CHAT_ENABLED_KEY = "free_chat_enabled"
FREE_CHAT_MONTHLY_TOKENS_KEY = "free_chat_monthly_tokens"

# --- defaults (also seeded by the migration) ---
DEFAULT_FREE_CHAT_ENABLED = True
DEFAULT_FREE_CHAT_MONTHLY_TOKENS = 200_000


def _to_bool(value, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


async def get_setting(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(text("SELECT value FROM public.app_settings WHERE key = :k"), {"k": key})).first()
    return row[0] if row else default


async def set_setting(db: AsyncSession, key: str, value) -> None:
    await db.execute(
        text(
            "INSERT INTO public.app_settings (key, value, updated_at) VALUES (:k, :v, now()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"
        ),
        {"k": key, "v": str(value)},
    )
    await db.commit()


async def get_free_chat_config(db: AsyncSession) -> dict:
    """Return {'enabled': bool, 'monthly_tokens': int} for the free chat tier."""
    enabled_raw = await get_setting(db, FREE_CHAT_ENABLED_KEY)
    tokens_raw = await get_setting(db, FREE_CHAT_MONTHLY_TOKENS_KEY)
    try:
        monthly_tokens = int(tokens_raw)
    except (TypeError, ValueError):
        monthly_tokens = DEFAULT_FREE_CHAT_MONTHLY_TOKENS
    return {
        "enabled": _to_bool(enabled_raw, DEFAULT_FREE_CHAT_ENABLED),
        "monthly_tokens": max(0, monthly_tokens),
    }


async def set_free_chat_config(db: AsyncSession, enabled: bool, monthly_tokens: int) -> dict:
    await set_setting(db, FREE_CHAT_ENABLED_KEY, "true" if enabled else "false")
    await set_setting(db, FREE_CHAT_MONTHLY_TOKENS_KEY, str(max(0, int(monthly_tokens))))
    return {"enabled": bool(enabled), "monthly_tokens": max(0, int(monthly_tokens))}
