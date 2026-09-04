"""X OAuth2 user-context flow for @earl_knows_ball.

Confidential client ("Web App, Automated App, or Bot"): the app secret lives on the
compute box and authenticates via HTTP Basic at the token endpoint (per XDK + X). We
use PKCE + client_secret; scope includes offline.access so we get a refresh token.

Why OAuth2 here: it is the SINGLE auth source for the account - both the user-context
read side (timelines/engagement of accounts we follow) AND posting (tweet.write). The
"Authorize Access on X" admin button produces these tokens, which we then use for every
thing the account does (reads, replies, likes/follows). Auto-refresh keeps it current.

Flow (stateless server except a short-lived DB attempt row for the PKCE verifier):
  GET /api/admin/x/oauth/authorize?redirect_to=...
      -> build authorize URL w/ PKCE, persist {state, code_verifier} -> 302 to X
  GET /api/admin/x/oauth/callback?state=&code=
      -> recover verifier, exchange -> write oauth2_* tokens to public.x_account -> 302 home
"""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Optional, Tuple

from sqlalchemy import text

try:
    from requests_oauthlib import OAuth2Session
    from xdk.oauth2_auth import OAuth2PKCEAuth
except Exception:  # pragma: no cover - only fails if xdk missing at cold import
    OAuth2PKCEAuth = None
    OAuth2Session = None

# Refresh when the access token is inside this window of expiring, so a call in flight
# never hands out a token that dies mid-request.
EXPIRY_SLACK = 60

# X REQUIRES each requested scope to be grantable by the App's permission level / type, or it
# refuses the ENTIRE consent with a generic "Something went wrong". So we request ONLY what the
# shipping features actually need (post replies + read the feed of accounts we follow + refresh):
#
#   tweet.write     -> POST /2/tweets ("Approve and send" reply). THE missing write privilege.
#   tweet.read      -> read posts/timeline.
#   users.read      -> own identity/profile.
#   follows.read    -> GET /2/users/:id/following (accounts we follow) -> the feed we read.
#   offline.access  -> refresh token so access keeps renewing without re-consent.
#
# NOT included (drop until a feature needs them, to keep the consent screen grantable):
# follows.write, like.read, like.write, list.read, mention.read, bookmark.read.
SCOPES = [
    "users.read",
    "tweet.read",
    "tweet.write",
    "follows.read",
    "offline.access",
]

SCOPE_STR = " ".join(SCOPES)


def _random_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Tuple[str, str, str]:
    """Returns (authorize_url, state, code_verifier). Persist the (state,verifier) pair."""
    if OAuth2PKCEAuth is None:
        raise RuntimeError("xdk.oauth2_auth is unavailable - can't start X OAuth2")
    auth = OAuth2PKCEAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
    )
    state = _random_state()
    url = auth.get_authorization_url(state=state)
    verifier = auth.get_code_verifier()
    if not verifier:
        raise RuntimeError("XDK did not produce a code_verifier")
    return url, state, verifier


def exchange_authorization_code(
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
) -> dict:
    """Exchange the auth code for an OAuth token dict (access_token, refresh_token, ...)."""
    if OAuth2PKCEAuth is None:
        raise RuntimeError("xdk.oauth2_auth is unavailable")
    auth = OAuth2PKCEAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPES,
    )
    auth.set_pkce_parameters(code_verifier)
    return auth.exchange_code(code, code_verifier=code_verifier)


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> dict:
    """Refresh an expired access token (confidential client, offline.access)."""
    if OAuth2Session is None:
        raise RuntimeError("xdk/requests_oauthlib unavailable")
    from requests.auth import HTTPBasicAuth
    sess = OAuth2Session(client_id=client_id)
    return sess.refresh_token(
        "https://api.x.com/2/oauth2/token",
        refresh_token=refresh_token,
        auth=HTTPBasicAuth(client_id, client_secret),
    )


def _tok_expiry(seconds) -> Optional[dt.datetime]:
    if not seconds:
        return None
    try:
        return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=int(seconds))
    except (TypeError, ValueError):
        return None


# ---------------- DB helpers (compute role, public schema) ----------------

async def persist_attempt(db, state: str, code_verifier: str, redirect_to: Optional[str]) -> None:
    """Persist an authorize hop so callback can recover the code_verifier."""
    await db.execute(
        text(
            "INSERT INTO public.x_oauth_state (state, code_verifier, redirect_to, created_at) "
            "VALUES (:s, :cv, :r, now()) ON CONFLICT (state) "
            "DO UPDATE SET code_verifier=EXCLUDED.code_verifier, redirect_to=EXCLUDED.redirect_to"
        ),
        {"s": state, "cv": code_verifier, "r": redirect_to},
    )
    await db.commit()


async def load_attempt(db, state: str) -> Optional[dict]:
    row = (
        await db.execute(
            text(
                "SELECT state, code_verifier, redirect_to, created_at "
                "FROM public.x_oauth_state WHERE state=:s"
            ),
            {"s": state},
        )
    ).mappings().first()
    return dict(row) if row else None


async def clear_attempt(db, state: str) -> None:
    """Purge a used/abandoned attempt row; never let a code_verifier linger > needed."""
    await db.execute(text("DELETE FROM public.x_oauth_state WHERE state=:s"), {"s": state})
    await db.commit()


async def save_tokens(db, tok: dict) -> None:
    """Write a fresh OAuth2 token set into public.x_account (single canonical row, platform='x')."""
    exp = _tok_expiry(tok.get("expires_in"))
    scope = tok.get("scope") or SCOPE_STR
    access = tok.get("access_token")
    refresh = tok.get("refresh_token")

    # canonical row upsert
    await db.execute(
        text(
            "INSERT INTO public.x_account (platform, oauth2_access_token, oauth2_refresh_token,"
            "  oauth2_token_type, oauth2_scope, oauth2_expires_at, oauth2_connected_at)"
            " VALUES ('x', :at, :rt, 'bearer', :sc, :ex, now())"
            " ON CONFLICT (platform) DO UPDATE SET"
            "  oauth2_access_token=EXCLUDED.oauth2_access_token,"
            "  oauth2_refresh_token=EXCLUDED.oauth2_refresh_token,"
            "  oauth2_token_type='bearer',"
            "  oauth2_scope=EXCLUDED.oauth2_scope,"
            "  oauth2_expires_at=EXCLUDED.oauth2_expires_at,"
            "  oauth2_connected_at=now()"
        ),
        {"at": access, "rt": refresh, "sc": scope, "ex": exp},
    )
    await db.commit()


async def load_token(db) -> dict:
    """Return stored OAuth2 token set + expiry for @earl_knows_ball."""
    row = (
        await db.execute(
            text(
                "SELECT oauth2_access_token AS at, oauth2_refresh_token AS rt,"
                "       oauth2_expires_at AS ex, oauth2_refresh_token IS NOT NULL AS has_refresh"
                " FROM public.x_account WHERE platform='x'"
            )
        )
    ).mappings().first()
    if not row or not row["at"]:
        return {}
    return {
        "access_token": row["at"],
        "refresh_token": row["rt"],
        "expires_at": row["ex"],
        "has_refresh": bool(row["has_refresh"]),
    }


async def get_live_token(db, client_id: str, client_secret: str) -> dict:
    """Return a CURRENT, usable OAuth2 access token for @earl_knows_ball.

    This is what keeps the auth current: every read AND write resolves the token through
    here. If the stored access token is expired (or expiring within EXPIRY_SLACK) and a
    refresh token exists, we call refresh_access_token and persist the new set before
    returning. Returns {} if there is no stored token at all (needs "Authorize Access on
    X" to be run/approved).
    """
    tok = await load_token(db)
    if not tok:
        return {}
    now = dt.datetime.now(dt.timezone.utc)
    exp = tok.get("expires_at")
    needs_refresh = (exp is None) or (exp <= now)
    # Refresh a little early so we never hand out a token that dies mid-call.
    if not needs_refresh and isinstance(exp, dt.datetime):
        needs_refresh = exp <= (now + dt.timedelta(seconds=EXPIRY_SLACK))
    if needs_refresh:
        if not tok.get("has_refresh") or not tok.get("refresh_token"):
            # No refresh token -> can only be fixed by re-authorizing via the button.
            return {"access_token": tok["access_token"], "stale": True}
        try:
            fresh = refresh_access_token(client_id, client_secret, tok["refresh_token"])
        except Exception as exc:  # noqa: BLE001 - surface as stale, let caller decide
            return {"access_token": tok["access_token"], "stale": True, "refresh_error": str(exc)}
        await save_tokens(db, fresh)
        tok = await load_token(db)
    return {"access_token": tok["access_token"], "stale": False}
