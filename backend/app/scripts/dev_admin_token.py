# Generate a short-lived dev admin JWT for hitting protected admin (X send) routes on
# the DEV box while testing. Prints token to stdout. Not for prod.
import asyncio, os, sys
from app.core.config import settings
from app.database import async_session_maker
from app.routers import auth
from sqlalchemy import text

async def main():
    if len(sys.argv) > 1:
        email = sys.argv[1]
    else:
        email = settings.admin_email or os.environ.get("X_TEST_ADMIN", "")
    async with async_session_maker() as db:
        # find an is_admin user by email
        row = (await db.execute(text(
            "SELECT id, email, is_admin FROM users WHERE email=:e LIMIT 1"),
            {"e": email})).mappings().first()
        if not row:
            # fall back to any admin
            row = (await db.execute(text(
                "SELECT id, email, is_admin FROM users WHERE is_admin=TRUE LIMIT 1"))).mappings().first()
        if not row:
            print("NO_ADMIN_USER", file=sys.stderr); return
        # build a small User-compatible object for _create_jwt
        user = type("U", (), {"id": row["id"], "email": row["email"], "is_admin": row["is_admin"]})
        print(auth._create_jwt(user))

asyncio.run(main())
