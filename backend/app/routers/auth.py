import hashlib
import hmac
import logging
import random
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# ── Constants ────────────────────────────────────────────────────────
LOGIN_CODE_TIMEOUT_MINUTES = 10
COOKIE_NAME = "earl_token"
COOKIE_MAX_AGE = 30 * 24 * 60 * 60  # 30 days in seconds


# ── Schemas ──────────────────────────────────────────────────────────

class SendCodeRequest(BaseModel):
    email: EmailStr


class SendCodeResponse(BaseModel):
    message: str


class VerifyCodeRequest(BaseModel):
    email: EmailStr
    code: str


class VerifyCodeResponse(BaseModel):
    user: dict
    token: str
    message: str = "Login successful"


class LogoutResponse(BaseModel):
    message: str


from datetime import datetime
from pydantic import field_serializer


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    subscription_tier: str = "free"
    is_admin: bool = False
    is_active: bool = True
    email_verified: bool = False
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    model_config = {"from_attributes": True}

    @field_serializer("created_at", "last_login_at")
    def serialize_dt(self, v: datetime | None) -> str | None:
        return v.isoformat() if v else None


# ── Helpers ──────────────────────────────────────────────────────────

def _generate_code() -> str:
    """Generate a 6-digit numeric login code."""
    return f"{random.randint(0, 999999):06d}"


def _hash_code(code: str) -> str:
    """Hash a code for storage using HMAC-SHA256 with the JWT secret as key."""
    return hmac.new(
        settings.jwt_secret.encode(),
        code.encode(),
        hashlib.sha256,
    ).hexdigest()


def _verify_code(code: str, code_hash: str) -> bool:
    """Constant-time comparison of code against stored hash."""
    expected = _hash_code(code)
    return hmac.compare_digest(expected, code_hash)


async def _send_email(to: str, code: str) -> None:
    """Send a login code via Resend."""
    # Dev fallback: print code to log if no Resend API key configured
    if not settings.resend_api_key or settings.resend_api_key.startswith("re_xxx"):
        logger.warning("No Resend API key configured. Login code for %s: %s (expires in %d min)", to, code, LOGIN_CODE_TIMEOUT_MINUTES)
        return

    # Always log the code for dev/test convenience
    logger.info("Login code for %s: %s (expires in %d min)", to, code, LOGIN_CODE_TIMEOUT_MINUTES)

    try:
        import resend
        resend.api_key = settings.resend_api_key
        from_email = "login@users.earlknowsball.com"
        resend.Emails.send({
            "from": f"Earl Knows Ball <{from_email}>",
            "to": [to],
            "subject": "Your Earl Knows Ball login code",
            "html": (
                f"<div style='font-family: sans-serif; max-width: 480px; margin: 0 auto;'>"
                f"<h2>Earl Knows Ball</h2>"
                f"<p>Your login code is:</p>"
                f"<p style='font-size: 32px; font-weight: bold; letter-spacing: 8px; "
                f"text-align: center; padding: 16px; background: #f0f0f0; "
                f"border-radius: 8px;'>{code}</p>"
                f"<p>This code expires in {LOGIN_CODE_TIMEOUT_MINUTES} minutes.</p>"
                f"<p style='color: #666; font-size: 13px;'>If you didn't request it, just ignore this email.</p>"
                f"</div>"
            ),
        })
        logger.info(f"Email sent to {to}")
    except ImportError:
        logger.warning("Resend SDK not installed — code only in log for %s", to)
    except Exception as e:
        logger.warning("Failed to send email to %s via Resend: %s", to, e)


def _serialize_user(user: User) -> dict:
    """Serialize a User model into a dict for responses."""
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "subscription_tier": user.subscription_tier,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "email_verified": user.email_verified,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _create_jwt(user: User) -> str:
    """Create a JWT token (symmetric HMAC-SHA256)."""
    import jwt as pyjwt
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "email": user.email,
        "iat": now,
        "exp": now + timedelta(seconds=COOKIE_MAX_AGE),
    }
    return pyjwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def _decode_jwt(token: str) -> dict | None:
    """Decode and validate a JWT. Returns payload or None on any failure."""
    try:
        import jwt as pyjwt
        payload = pyjwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        return payload
    except Exception:
        return None


async def get_user_from_token(token: str, db: AsyncSession) -> User:
    """Get a User from a raw JWT token string. Raises 401 on failure."""
    payload = _decode_jwt(token)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    return user


async def get_token_user(auth_header: str, db: AsyncSession) -> User:
    """Backward-compat: parse token from 'Bearer <token>' and return User."""
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    token = auth_header.replace("Bearer ", "", 1).strip()
    return await get_user_from_token(token, db)


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """Dependency: extract user from the earl_token cookie or Authorization header."""
    # Check cookie first
    token = request.cookies.get(COOKIE_NAME)
    if token:
        return await get_user_from_token(token, db)

    # Fall back to Authorization header (legacy support for admin pages)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.replace("Bearer ", "", 1).strip()
        if token:
            return await get_user_from_token(token, db)

    raise HTTPException(status_code=401, detail="Not authenticated")


# ── Endpoints ────────────────────────────────────────────────────────

@router.post("/send-code", response_model=SendCodeResponse)
async def send_code(req: SendCodeRequest, db: AsyncSession = Depends(get_db)):
    """
    Step 1: Request a 6-digit login code.
    Creates a hashed code in the DB and sends it via email.
    Idempotent — sending multiple times overwrites the previous code.
    """
    email = req.email.lower().strip()
    code = _generate_code()
    code_hash = _hash_code(code)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=LOGIN_CODE_TIMEOUT_MINUTES)

    # Find or create user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        # Auto-create user on first send-code request
        user = User(
            id=str(uuid.uuid4()),
            email=email,
            display_name=email.split("@")[0],
            subscription_tier="free",
            is_active=True,
            is_admin=False,
            email_verified=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)

    # Store the code hash
    user.login_code_hash = code_hash
    user.login_code_expires_at = expires_at
    await db.commit()

    # Send email (async — fire-and-forget-ish, but we await for error handling)
    await _send_email(email, code)

    return SendCodeResponse(message="Login code sent to your email")


@router.post("/verify-code", response_model=VerifyCodeResponse)
async def verify_code(req: VerifyCodeRequest, response: Response, db: AsyncSession = Depends(get_db)):
    """
    Step 2: Submit the 6-digit code.
    If valid, logs the user in and sets a 30-day cookie.
    If the user doesn't exist yet, creates them.
    """
    email = req.email.lower().strip()
    code = req.code.strip()

    # Find user
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(status_code=401, detail="No code requested for this email")

    # Check code expiry
    if user.login_code_expires_at is None or user.login_code_hash is None:
        raise HTTPException(status_code=401, detail="No code requested")

    if datetime.now(timezone.utc) > user.login_code_expires_at:
        raise HTTPException(status_code=401, detail="Code expired. Request a new one.")

    # Check code value (constant-time compare)
    if not _verify_code(code, user.login_code_hash):
        raise HTTPException(status_code=401, detail="Invalid code")

    # Code is valid — clear it so it can't be reused
    user.login_code_hash = None
    user.login_code_expires_at = None
    user.last_login_at = datetime.now(timezone.utc)

    # Mark email as verified on first successful login
    if not user.email_verified:
        user.email_verified = True

    await db.commit()

    # Create JWT and set cookie
    token = _create_jwt(user)

    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        expires=COOKIE_MAX_AGE,
        path="/",
        secure=settings.base_url.startswith("https"),
        httponly=True,
        samesite="lax",
    )

    return VerifyCodeResponse(
        user=_serialize_user(user),
        token=token,
    )


@router.post("/logout", response_model=LogoutResponse)
async def logout(response: Response):
    """Clear the auth cookie."""
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        secure=settings.base_url.startswith("https"),
        httponly=True,
        samesite="lax",
    )
    return LogoutResponse(message="Logged out")


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Return the currently authenticated user."""
    return user
