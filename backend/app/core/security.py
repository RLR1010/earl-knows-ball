from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.core.config import settings

bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    return user


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
