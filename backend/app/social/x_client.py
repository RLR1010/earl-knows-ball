"""X (@earl_knows_ball) client — thin wrapper over the official `xdk` (X Developer Kit).

Auth model: "acting as ourselves" — @earl_knows_ball owns the app, so we use OAuth 1.0a
user-context with the app's own Access Token/Secret. No end-user consent dance needed.

Phase 0/1 scope: connect/verify + create a post (text, optionally with one/more uploadead
images). Scheduler + engagement polling (Phase 3/4) build on the same client.

Credentials come from Settings (config.py) which reads .env: X_CONSUMER_KEY,
X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET. If unset, the account is
"not connected" and the admin x-router reports that instead of erroring.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Imported lazily so a cold module import never hard-fails if `xdk` is absent
# (the sysimage provider/environment may not have it on every box until deployed).
def _client_for(
    *,
    api_key: Optional[str],
    api_secret: Optional[str],
    access_token: Optional[str],
    access_secret: Optional[str],
):
    from xdk import Client
    from xdk.oauth1_auth import OAuth1

    if not (api_key and api_secret and access_token and access_secret):
        raise XNotConnectedError("X credentials not configured (empty). Connect in admin first.")

    # OAuth1 `callback` is a required positional in xdk; "oob" = out-of-band / self app.
    # (An app posting as its OWN account does not go through a user-grant callback redirect.)
    auth = OAuth1(api_key, api_secret, "oob", access_token, access_secret)
    return Client(auth=auth)


def _client_for_oauth2(
    *,
    client_id: Optional[str],
    client_secret: Optional[str],
    access_token: Optional[str],
):
    """Build an xdk Client authenticated as @earl_knows_ball via OAuth 2.0 User Context.

    X requires OAuth 2.0 (PKCE) for WRITE scopes (tweet.write) on user-context calls - OAuth
    1.0a cannot carry tweet.write, which is the root of the 403 we hit on POST /2/tweets.
    The access token here is the live token minted by (and refreshed from) the "Authorize
    Access on X" flow; it must include tweet.write to post.
    """
    from xdk import Client

    if not (client_id and client_secret and access_token):
        raise XNotConnectedError("X OAuth2 credentials not configured (client_id/secret/token).")
    # xdk Client(access_token=...) picks up the OAuth2 user-context bearer automatically.
    # Providing client_id/client_secret lets it (re)authenticate; the token itself is carried
    # as the OAuth2 access token.
    return Client(
        base_url="https://api.x.com",
        access_token=access_token,
        client_id=client_id,
        client_secret=client_secret,
    )


class XError(Exception):
    """Raised for a non-2xx / structured API error from X."""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 api_errors: Optional[list] = None, raw: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.errors = api_errors or []
        self.raw = raw


class XNotConnectedError(XError):
    pass


def _friendly_x_error(exc: Exception) -> XError:
    """Normalize arbitrary xdk / httpx exceptions into XError with useful detail."""
    if isinstance(exc, XError):
        return exc
    raw = getattr(exc, "response", None)
    status = getattr(raw, "status_code", None) if raw is not None else None
    body = ""
    try:
        body = raw.text[:800] if raw is not None else str(exc)
    except Exception:
        body = str(exc)
    # xdk raises with structured errors attached to client response errors
    errs = getattr(exc, "errors", None)
    if not errs and raw is not None:
        try:
            errs = raw.json().get("errors") if callable(getattr(raw, "json", None)) else None
        except Exception:
            errs = None
    msg = f"X API request failed: {exc}"
    if errs:
        detail = "; ".join(
            str(e.get("detail") or e.get("message") or e) if isinstance(e, dict) else str(e)
            for e in errs[:3]
        )
        if detail:
            msg = detail
    return XError(msg, status=status, api_errors=errs, raw=body)


def connect_health(
    *,
    api_key: str,
    api_secret: str,
    access_token: str,
    access_secret: str,
) -> dict:
    """Verify the OAuth1 "self" credentials work and return who we are + budget-ish info.

    Calls GET /2/users/me (a read) to prove auth + discover our numeric user id/handle.
    Used by the admin 'Connect/Test' screen. Does NOT spend a write credit.
    """
    try:
        client = _client_for(
            api_key=api_key, api_secret=api_secret,
            access_token=access_token, access_secret=access_secret,
        )
        me = client.users.get_me()
        data = (me.data or {}) if hasattr(me, "data") else (me or {})
        if not data:
            raise XError("GET /users/me returned no account data (check scopes/keys).")
        # xdk dataclass -> plain dict traits
        d = getattr(data, "model_dump", None)
        info = d() if callable(d) else (dict(data) if isinstance(data, dict) else {})
        return {
            "ok": True,
            "user_id": info.get("id"),
            "username": info.get("username"),
            "name": info.get("name", ""),
            "verified": bool(info.get("verified")),
        }
    except Exception as exc:  # noqa: BLE001 - normalize
        e = _friendly_x_error(exc)
        return {"ok": False, "error": str(e), "status": e.status}


def upload_image(media_bytes: bytes, *, client=None, mime: Optional[str] = None) -> str:
    """Upload a still image (PNG/JPEG) and return its X media_id string.

    media_category defaults to 'tweet_image' (works for a single image attached to a post).
    If max size/tightness ever bites (image >5MB), we switch to chunked
    initialize/append/finalize — not needed for our card PNGs.
    """
    try:
        c = client if client is not None else _client_for_from_settings()
        from xdk.schemas import MediaUploadRequest
        resp = c.media.upload(MediaUploadRequest(media=media_bytes, media_category="tweet_image"))
        d = resp.data if hasattr(resp, "data") else resp
        if not d:
            raise XError("POST /2/media/upload returned no data.", raw=str(resp))
        mid = getattr(d, "model_dump", None)
        info = mid() if callable(mid) else (dict(d) if isinstance(d, dict) else {})
        media_id = info.get("media_id")
        if not media_id:
            raise XError("media upload succeeded but media_id missing.", raw=str(info))
        return str(media_id)
    except Exception as exc:  # noqa: BLE001
        raise _friendly_x_error(exc) from exc


def create_post(
    text: str,
    *,
    media_ids: Optional[list[str]] = None,
    in_reply_to_tweet_id: Optional[str] = None,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    access_token: Optional[str] = None,
    access_secret: Optional[str] = None,
    oauth2_client_id: Optional[str] = None,
    oauth2_client_secret: Optional[str] = None,
    oauth2_access_token: Optional[str] = None,
) -> dict:
    """POST /2/tweets and return {ok, tweet_id, text}.

    media_ids come from a prior upload_image() call (store the returned id on the
    candidate as media_id). Pass in_reply_to_tweet_id to post this as a REPLY to that
    tweet (the "reply" block on the create-posts request). Caller is responsible for
    grounding (traceable source_ref).

    Auth: prefer OAuth 2.0 user-context (oauth2_client_id/secret/access_token) because X
    only allows WRITE (tweet.write) via OAuth2 - that fixes the 403 POST /2/tweets. Falls
    back to OAuth 1.0a creds only if no OAuth2 token is supplied.
    """
    try:
        if oauth2_client_id and oauth2_access_token:
            client = _client_for_oauth2(
                client_id=oauth2_client_id,
                client_secret=oauth2_client_secret,
                access_token=oauth2_access_token,
            )
        else:
            kwargs = {}
            if api_key is not None or api_secret is not None or access_token is not None or access_secret is not None:
                kwargs = {
                    "api_key": api_key, "api_secret": api_secret,
                    "access_token": access_token, "access_secret": access_secret,
                }
            client = _client_for(**kwargs) if kwargs else _client_for_from_settings()

        # Newer xdk exposes Pydantic reply/media models; older versions accept plain
        # dicts for the sub-block, so build the payload dict first and only wrap the
        # nested media/reply in the typed schema when the class is actually available.
        from xdk.schemas import CreatePostsRequest  # noqa: PLC0415
        body: dict = {"text": text}
        if media_ids:
            try:
                from xdk.schemas import CreatePostsMedia  # noqa: PLC0415
                body["media"] = CreatePostsMedia(media_ids=list(media_ids))
            except ImportError:  # pragma: no cover - xdk version fallback
                body["media"] = {"media_ids": list(media_ids)}
        if in_reply_to_tweet_id:
            try:
                from xdk.schemas import CreatePostsReply  # noqa: PLC0415
                body["reply"] = CreatePostsReply(in_reply_to_tweet_id=in_reply_to_tweet_id)
            except ImportError:  # pragma: no cover - xdk version fallback
                body["reply"] = {"in_reply_to_tweet_id": in_reply_to_tweet_id}
        resp = client.posts.create(CreatePostsRequest(**body))
        d = resp.data if hasattr(resp, "data") else resp
        if not d:
            raise XError("POST /2/tweets returned no data (may be a read-only/credit gate).", raw=str(resp))
        di = d.model_dump() if callable(getattr(d, "model_dump", None)) else dict(d)
        return {
            "ok": True,
            "tweet_id": di.get("id"),
            "text": di.get("text", text),
            "raw": str(di),
        }
    except Exception as exc:  # noqa: BLE001
        raise _friendly_x_error(exc) from exc


def fetch_post(tweet_id: str, *, api_key=None, api_secret=None,
               access_token=None, access_secret=None) -> dict:
    """GET /2/tweets/:id — read back a created post (verification + receipt/engagement seed)."""
    try:
        kwargs = {}
        if any(v is not None for v in (api_key, api_secret, access_token, access_secret)):
            kwargs = {
                "api_key": api_key, "api_secret": api_secret,
                "access_token": access_token, "access_secret": access_secret,
            }
        client = _client_for(**kwargs) if kwargs else _client_for_from_settings()
        resp = client.posts.get_by_id(tweet_id)
        d = resp.data if hasattr(resp, "data") else resp
        if not d:
            raise XError(f"GET /2/tweets/{tweet_id} returned no data.")
        di = d.model_dump() if callable(getattr(d, "model_dump", None)) else dict(d)
        return {"ok": True, "tweet_id": str(tweet_id), "data": di}
    except Exception as exc:  # noqa: BLE001
        raise _friendly_x_error(exc) from exc


def _client_for_from_settings():
    try:
        from app.core.config import settings
    except Exception:  # pragma: no cover
        raise XNotConnectedError("Could not import app settings.") from None
    return _client_for(
        api_key=getattr(settings, "x_consumer_key", "") or None,
        api_secret=getattr(settings, "x_consumer_secret", "") or None,
        access_token=getattr(settings, "x_access_token", "") or None,
        access_secret=getattr(settings, "x_access_token_secret", "") or None,
    )
