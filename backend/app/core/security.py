from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.core.config import settings

bearer_scheme = HTTPBearer(auto_error=False)

# Session cookie name. MUST match app.routers.auth.COOKIE_NAME ("earl_token").
# The cookie is the durable session: /auth/me re-issues it on every visit (30-day
# sliding window), so an active user is never dropped just because the JS-side
# localStorage token lapsed.
COOKIE_NAME = "earl_token"


async def _user_from_token(token: str, db: AsyncSession) -> User | None:
    """Decode a JWT and return the matching user, or None if invalid/unknown."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError:
        return None
    user_id = payload.get("sub")
    if user_id is None:
        return None
    return (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Authenticate the logged-in user.

    The httpOnly `earl_token` COOKIE is the source of truth for login status and
    access (the durable session, renewed by /auth/me on every visit). The
    Authorization header (localStorage token) is only a FALLBACK for clients
    that don't carry the cookie. This guarantees a stale/lapsed JS token — or a
    cleared localStorage — can never downgrade or override an active cookie
    session.
    """
    header_token = credentials.credentials if (credentials and credentials.credentials) else None
    cookie_token = request.cookies.get(COOKIE_NAME)

    candidates: list[str] = []
    if cookie_token:
        candidates.append(cookie_token)
    if header_token and header_token != cookie_token:
        candidates.append(header_token)

    for tok in candidates:
        user = await _user_from_token(tok, db)
        if user is not None:
            return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid token" if candidates else "Not authenticated",
    )


async def get_optional_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Like get_current_user but returns None instead of raising when the request
    is unauthenticated or the token is invalid.

    Mirrors get_current_user EXACTLY: the httpOnly `earl_token` cookie is the
    source of truth for login/access, and the Authorization header (localStorage
    token) is only a fallback. Used by endpoints that serve public content to
    everyone (no auth required) but must resolve the caller when premium content
    is requested.
    """
    header_token: str | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        cand = auth_header.replace("Bearer ", "", 1).strip()
        if cand:
            header_token = cand
    cookie_token = request.cookies.get(COOKIE_NAME)

    candidates: list[str] = []
    if cookie_token:
        candidates.append(cookie_token)
    if header_token and header_token != cookie_token:
        candidates.append(header_token)

    for tok in candidates:
        user = await _user_from_token(tok, db)
        if user is not None:
            return user
    return None


async def require_premium(user: User = Depends(get_current_user)) -> User:
    if not user_is_premium(user):
        raise HTTPException(status_code=403, detail="Premium subscription required")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require an authenticated, active admin user.
    Admin-only mutation endpoints (writeup edits, moderation, admin content ops)
    must depend on this so anonymous/non-admin users cannot mutate data.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")
    return user


def user_is_premium(user: User) -> bool:
    return user is not None and user.subscription_tier in ("premium", "premium_yearly")
