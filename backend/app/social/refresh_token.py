"""Scheduled maintenance task: keep the X OAuth2 access token fresh.

X access tokens live 2 hours and are rotated via the refresh token (offline.access).
The app also refreshes lazily inside get_live_token when a request arrives inside the
600s expiry window, but if no X call happens around then the token just ages out and the
next call has to refresh mid-flight. This task proactively refreshes on a schedule so a
fresh 2h token is always in place, well before expiry.

Run from the backend dir:
    PYTHONPATH=/home/rich/earl-knows-football/backend \
      /home/rich/earl-knows-football/venv/bin/python -m app.social.refresh_token

The scheduler invokes it via a task_config row (type=subprocess). Refresh is only
performed if the stored access token is within REFRESH_IF_WITHIN_SECONDS of expiring
(or already expired / malformed), so most runs are cheap no-ops unless a refresh is due.
"""

import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

# Token refresh only fires when within N seconds of expiry. With a 2h token lifetime
# this keeps a proactive run well ahead of the in-call lazy refresh (600s slack).
REFRESH_IF_WITHIN_SECONDS = int(os.getenv("X_TOKEN_REFRESH_AHEAD_SECONDS", "3600"))


def _session_from_env():
    dsn = os.getenv("ADMIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    for pre in ("postgresql+asyncpg://", "postgresql+psycopg2://", "postgresql://"):
        if dsn.startswith(pre):
            dsn = "postgresql+asyncpg://" + dsn[len(pre):]
            break
    if not dsn.startswith("postgresql+asyncpg://"):
        dsn = "postgresql+asyncpg://" + dsn
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    eng = create_async_engine(dsn, echo=False)
    return async_sessionmaker(eng, expire_on_commit=False)


def _main() -> int:
    from app.core.config import settings
    from app.social import x_oauth as OA

    if not (settings.x_client_id and settings.x_client_secret):
        print("SKIP: x_client_id / x_client_secret not configured")
        return 0

    Session = _session_from_env()

    async def run() -> int:
        async with Session() as db:
            tok = await OA.load_token(db)
            if not tok or not tok.get("refresh_token"):
                print("SKIP: no refresh token stored — run the X Authorize flow once")
                return 0
            exp = tok.get("expires_at")
            if exp is None:
                due = True
            else:
                expires_at = exp if exp.tzinfo else exp.replace(tzinfo=dt.timezone.utc)
                now = dt.datetime.now(dt.timezone.utc)
                due = (expires_at - now).total_seconds() <= REFRESH_IF_WITHIN_SECONDS
            if not due:
                remaining = int((expires_at - now).total_seconds() // 60)
                print(f"SKIP: access token fresh ({remaining} min left); no refresh needed")
                return 0
            try:
                fresh = OA.refresh_access_token(
                    settings.x_client_id, settings.x_client_secret, tok["refresh_token"]
                )
            except Exception as e:  # noqa: BLE001 - surfaced to the compute log
                import traceback
                print("FAIL: refresh_access_token ->", type(e).__name__, str(e)[:400])
                traceback.print_exc()
                return 1
            await OA.save_tokens(db, fresh)
            saved = await OA.load_token(db)
            exp_saved = saved.get("expires_at")
            print(f"OK: access token refreshed; stored expires_at={exp_saved}")
            return 0

    return asyncio.run(run())


if __name__ == "__main__":
    code = _main()
    sys.exit(code)
