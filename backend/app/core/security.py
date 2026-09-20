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
    """Authenticate from the Authorization header (localStorage token), falling
    back to the httpOnly `earl_token` cookie.

    The header is preferred when present AND valid. If the header token is
    present but invalid/expired we still fall back to the cookie, so a stale
    JS token can never lock an active user out (the cookie keeps them logged in
    and /auth/me renews it on each visit).
    """
    header_token = credentials.credentials if (credentials and credentials.credentials) else None
    cookie_token = request.cookies.get(COOKIE_NAME)

    candidates: list[str] = []
    if header_token:
        candidates.append(header_token)
    if cookie_token and cookie_token != header_token:
        candidates.append(cookie_token)

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
    is unauthenticated or the token is invalid. Preferred over Authorization header
    (localStorage token), falling back to the earl_token cookie — matching the
    frontend source of truth. Used for endpoints that serve public content to
    everyone (no auth required) but need to know the caller when premium content
    is requested.
    """
    token: str | None = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        cand = auth_header.replace("Bearer ", "", 1).strip()
        if cand:
            token = cand
    if token is None:
        token = request.cookies.get("earl_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id: str | None = payload.get("sub")
        if user_id is None:
            return None
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        return user
    except Exception:
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
