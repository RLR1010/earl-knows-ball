"""X (Twitter) Ads server-side Conversions (measurement) API client.

Sends conversion events to:

    POST https://ads-api.x.com/12/measurement/conversions/{pixel_id}
    X-Pixel-Token: <token>
    Content-Type: application/json

Used for server-side conversion tracking (login / checkout). This module is
server-side ONLY — the token must never reach the browser.

Design notes:
- Fire-and-forget by default: a tracking failure must NEVER break a login or a
  checkout. Callers should not await raising behavior; we log and swallow.
- Each event carries at least one identifier. Preference order:
    twclid  >  hashed_email  >  hashed_phone_number  >  (ip_address + user_agent)
  We send whatever we have (multiple is allowed and improves matching).
- `conversion_id` enables deduplication against the browser pixel event.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# X conversion event IDs — MUST match the IDs defined in X Events Manager and
# the browser pixel (frontend/src/components/TwclidCapture.tsx X_EVENTS).
X_EVENT_LOGIN = "tw-rf02z-rf69g"
X_EVENT_PURCHASE = "tw-rf02z-rf6c9"

_CONVERSIONS_BASE = "https://ads-api.x.com/12/measurement/conversions"
_TIMEOUT = 8.0
_MAX_ATTEMPTS = 3


def _hash_identifier(value: str) -> str:
    """SHA-256 hex digest of a lowercased, trimmed identifier.

    X expects 64 lowercase hex chars for hashed_email / hashed_phone_number.
    Emails MUST be lowercased + stripped before hashing.
    """
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()


def hash_email(email: str) -> str:
    """SHA-256 of a lowercased, trimmed email (64 hex chars)."""
    return _hash_identifier(email)


def hash_phone(phone: str) -> str:
    """SHA-256 of a normalized phone number (digits-only recommended)."""
    return _hash_identifier(phone)


def build_identifiers(
    *,
    twclid: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> list[dict[str, str]]:
    """Build the `identifiers` array for a conversion event.

    Returns a single-element list (X accepts a list). Only non-empty fields are
    included. Returns [] when nothing usable is available — callers should skip
    sending in that case.
    """
    ident: dict[str, str] = {}
    if twclid:
        ident["twclid"] = twclid.strip()
    if email:
        ident["hashed_email"] = hash_email(email)
    if phone:
        ident["hashed_phone_number"] = hash_phone(phone)
    # ip_address + user_agent are only useful as a PAIR.
    if ip_address and user_agent:
        ident["ip_address"] = ip_address.strip()
        ident["user_agent"] = user_agent.strip()

    # X expects `identifiers` as a LIST of SEPARATE objects, one field each —
    # e.g. [{"twclid":".."},{"hashed_email":".."},{"ip_address":"..","user_agent":".."}].
    # Merging them into a single object makes X fail to parse the conversion
    # and it responds as if `event_id` were missing.
    identifiers: list[dict[str, str]] = []
    if "twclid" in ident:
        identifiers.append({"twclid": ident["twclid"]})
    if "hashed_email" in ident:
        identifiers.append({"hashed_email": ident["hashed_email"]})
    if "hashed_phone_number" in ident:
        identifiers.append({"hashed_phone_number": ident["hashed_phone_number"]})
    if "ip_address" in ident:
        identifiers.append({"ip_address": ident["ip_address"], "user_agent": ident["user_agent"]})
    return identifiers


def _endpoint() -> str:
    return f"{_CONVERSIONS_BASE}/{settings.x_pixel_id}"


def is_configured() -> bool:
    return bool(settings.x_pixel_token)


async def send_conversion(
    *,
    event_id: str,
    conversion_id: str | None = None,
    event_source_url: str | None = None,
    identifiers: list[dict[str, str]] | None = None,
    conversion_time: datetime | None = None,
) -> bool:
    """POST a single conversion event to the X measurement API.

    Returns True on 2xx, False otherwise. Never raises (tracking must not break
    the caller). Skips silently when unconfigured or when no identifiers exist.
    """
    if not is_configured():
        logger.debug("X pixel token not configured; skipping conversion %s", event_id)
        return False

    if not identifiers:
        logger.debug("X conversion %s has no identifiers; skipping", event_id)
        return False

    ts = conversion_time or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    conversion_time_str = ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    conversion: dict[str, Any] = {
        "conversion_time": conversion_time_str,
        "event_id": event_id,
        "identifiers": identifiers,
    }
    if event_source_url:
        conversion["event_source_url"] = event_source_url
    if conversion_id:
        conversion["conversion_id"] = conversion_id

    # IMPORTANT: the measurement endpoint takes the conversion object as the
    # TOP-LEVEL body — it is NOT wrapped in a `conversions` array. Sending
    # {"conversions":[{...}]} makes X fail to find `event_id` and return
    # MISSING_PARAMETER. Verified against the live endpoint 2026-09-10.
    payload = conversion
    headers = {
        "X-Pixel-Token": settings.x_pixel_token,
        "Content-Type": "application/json",
    }

    import asyncio

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(_endpoint(), json=payload, headers=headers)
            if 200 <= resp.status_code < 300:
                logger.info("X conversion %s sent (HTTP %s)", event_id, resp.status_code)
                return True
            if resp.status_code == 429 and attempt < _MAX_ATTEMPTS:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if (retry_after or "").isdigit() else 2.0 * attempt
                logger.warning("X conversion rate-limited; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                continue
            logger.warning(
                "X conversion %s failed: HTTP %s %s",
                event_id,
                resp.status_code,
                resp.text[:300],
            )
            return False
        except Exception as e:  # noqa: BLE001 — never break the caller
            if attempt < _MAX_ATTEMPTS:
                logger.warning("X conversion %s error (attempt %s): %s", event_id, attempt, e)
                await asyncio.sleep(2.0 * attempt)
                continue
            logger.warning("X conversion %s failed after %s attempts: %s", event_id, _MAX_ATTEMPTS, e)
            return False
    return False


def fire_conversion(
    *,
    event_id: str,
    conversion_id: str | None = None,
    event_source_url: str | None = None,
    twclid: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    conversion_time: datetime | None = None,
) -> None:
    """Schedule a conversion send WITHOUT blocking the caller.

    Safe to call from a sync request handler: creates a background task on the
    running event loop. If no loop is running, does nothing (tracking is
    best-effort only).
    """
    import asyncio

    identifiers = build_identifiers(
        twclid=twclid,
        email=email,
        phone=phone,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    if not identifiers:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    loop.create_task(
        send_conversion(
            event_id=event_id,
            conversion_id=conversion_id,
            event_source_url=event_source_url,
            identifiers=identifiers,
            conversion_time=conversion_time,
        )
    )
